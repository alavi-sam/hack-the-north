"""Untitled Town server: owns the world, runs the agent loops, pushes state over WebSocket."""
import asyncio, json, os, random, time, pathlib, math

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import catalog
import llm
from simulation_clock import clock
from world import Agent, World, ACTIONS, PLACES, nearest_place

HERE = pathlib.Path(__file__).parent
app = FastAPI()
world = World()
clients: set[WebSocket] = set()

FAST_HZ = 12          # movement/animation
SLOW_SECONDS = float(os.getenv("SLOW_SECONDS", "11"))  # per-agent decision interval, staggered
agent_tasks = {}

# These are motives and offline reactions; live dialogue still comes from each agent's persona.
EVENT_LABELS = {"crash": "the market crash", "shortage": "the shortage", "festival": "the festival",
                "election": "the election", "stranger": "Rowan's arrival"}
EVENT_MOTIVES = {
    "mira": ("Protect margins, keep stock moving, and win customers without bankrupting the shop.",
             "Customers are celebrating. Time to make the till sing!", "My margins! I need customers, not more losses.",
             "I'll be watching what this means for my shop."),
    "bram": ("Protect the bank's reserves, assess debtors, and favour reliable borrowers.",
             "Enjoy yourselves. Your loans will still be here tomorrow.", "Steady now. Who can still repay what they owe?",
             "Good judgment matters more than grand promises."),
    "wren": ("Protect wages, manage debt to Bram, and save toward an independent stall.",
             "A little dancing, then these coins go toward my own stall.", "Will my wages cover food and Bram's payment?",
             "I'd like a fair chance to get ahead."),
    "fig": ("Sell your produce fairly, keep people fed, and compete with Mira.",
             "Dancing works up an appetite. I've apples to sell!", "People still need food. I'll put my crop to work.",
             "Let us see who values honest produce."),
    "kit": ("Look for a profitable angle, exploit trust and gossip, and avoid honest work.",
             "Generous crowds? I suddenly feel very sociable.", "Trouble for them, opportunity for me. Who needs a friend?",
             "I know an opportunity when I see one."),
    "orla": ("Defend the merchants and your political standing; reassure people without losing donors.",
             "A thriving town! Do remember who stands with its merchants.", "The merchants need confidence. I must be seen taking charge.",
             "The merchants will want a quiet word."),
    "devi": ("Protect workers and affordable food, challenge merchant privilege, and win support.",
             "Tonight we dance together. Tomorrow we demand fair wages.", "Workers must not pay for the merchants' mistakes.",
             "The workers deserve to be heard."),
    "rowan": ("Find worthwhile goods and investments while protecting your travelling fortune.",
              "A welcome like this deserves a little spending!", "A bargain is only a bargain if the business survives.",
             "I'll hear everyone out before spending a coin."),
}


def reaction_decision(ag):
    """A useful, validated fallback when an event response is missing or the API is down."""
    event = ag.event_reaction
    kind = event["kind"]
    motive, festive, worried, social = EVENT_MOTIVES.get(ag.id, (ag.goal, "A chance to meet my neighbours!", "I must rethink my plans.", "I should hear what people have to say."))
    line = festive if kind == "festival" else worried
    action, target, arg = "move_to", ag.home, ""
    if kind == "festival":
        action, target = "move_to", "square"
    elif ag.politician:
        action, target, arg = "promise", "", "workers_stipend" if ag.id == "devi" else "free_market"
    elif ag.produces:
        action, target = "work", ""
    elif ag.employer and kind in ("crash", "shortage"):
        action, target = "work", ""
    elif ag.id == "kit":
        action, target = "talk_to", "rowan" if kind == "stranger" else "mira"
    elif kind == "stranger" and ag.id != "rowan":
        action, target = "talk_to", "rowan"
    if kind in ("stranger", "election"):
        line = social
    return {"thought": motive[:160], "action": action, "target": target, "arg": arg,
            "say": f"{EVENT_LABELS[kind].capitalize()}: {line}"}


def queue_event_reactions(kind, description):
    world.manual_event_id += 1
    for i, ag in enumerate(world.agents.values()):
        ag.event_reaction = {"id": world.manual_event_id, "kind": kind, "description": description, "turns": 3}
        ag.remember(f"Just happened: {description}", 6, source="town")
        reaction = reaction_decision(ag)
        ag.thought = reaction["thought"]
        ag.last_action = f"reacting to {EVENT_LABELS[kind]}"
        ag.speak(reaction["say"], max(6, 6 * world.speed))
        # Free the existing concurrency slots for fresh decisions about the changed world.
        task = agent_tasks.get(ag.id)
        if task and not task.done():
            task.cancel()
        ag.next_tick = clock.time() + .2 + i * .3

SYSTEM = """You are a resident of a small town in a simulation. Stay in character.
Reply with ONLY a JSON object, no prose, no markdown fence:
{"thought": "one short private thought", "action": "<one action>", "target": "<agent id or place or item>", "arg": "<extra, may be empty>", "say": "<one short line spoken aloud, may be empty>"}
Valid actions: """ + ACTIONS + """
Places: shop, bank, farm, tavern, square. Use the resident ids listed in the prompt.
Pursue your goal. Be specific and a little dramatic. Never invent coins or items you do not have.
Coins are WHOLE numbers — never 1.75. When you trade, name a QUANTITY and a price PER UNIT,
and put them in arg as "item, quantity, price" (e.g. "Apples, 50, 2" = fifty apples at 2c each).
Keep "thought" under 15 words and "say" under 20 words."""


def build_prompt(ag):
    event_context = ""
    if ag.event_reaction:
        event = ag.event_reaction
        motive = EVENT_MOTIVES.get(ag.id, (ag.goal, "", ""))[0]
        event_context = f"""PRIORITY: React to {EVENT_LABELS[event['kind']]}.
What actually happened: {event['description']}
Your personal stakes: {motive}
Use your CURRENT job, holdings, debt, cash and relationships below; old ambitions may have changed.
This event takes priority over your routine and the old conversation topic, even if it woke you up.
{'Give your first considered response.' if event['turns'] == 3 else 'Follow through on your response; do not repeat the same line.'}
Choose one concrete action caused by this event and a short spoken line in YOUR voice.
React differently from residents with other interests. Do not merely announce the event or go back to routine.
Do not claim an unexecuted trade, promise or payment has already happened.
At a festival, socialise, seek customers, campaign or celebrate according to your personality.
Your last action result: {ag.last_action}. If it failed, choose a different feasible approach.
Never talk, gossip or trade with yourself. For gossip, target is the listener's id and arg is the full claim.
For buy_shares/sell_shares, target is an actual shop owner's id and arg is the number of shares.
Actual share issuers: {', '.join(s.owner for s in world.shops)}. The exchange itself is not a business to invest in.
\n"""
    near = world.nearby(ag)
    mems = "\n".join(f"- {m.text}" for m in ag.top_memories(6)) or "- (nothing yet)"
    trust = ", ".join(f"{world.agents[k].name}:{v:+.2f}" for k, v in ag.trust.items() if k in world.agents)
    mine = world.shop_of(ag.id)
    if mine:
        stock = ", ".join(f"{g['name']} at {g['price']} (cost {g['cost']}, {g['qty']} left)"
                          for g in mine.goods[:8]) or "nothing at all — the shelves are empty"
    else:
        stock = ", ".join(f"{g['name']} {g['price']}c at {sh.name}"
                          for sh, g in world.all_goods()[:10])
    inv = ", ".join(f"{k} x{v}" for k, v in ag.inventory.items() if v) or "nothing"
    debt = ", ".join(f"{k} {v}c" for k, v in ag.debts.items() if v) or "none"
    # Whoever has the coin and no shop should be looking at the empty pitches.
    ladder = ""
    may, why = world.may_trade(ag)
    if not may:
        ladder = f" You cannot keep a shop: {why}."
    elif not world.shop_of(ag.id):
        cost, plot = world.stall_cost(), world.free_plot()
        if not plot:
            ladder = " Every market pitch is taken."
        elif ag.cash >= cost:
            ladder = (f" You have {ag.cash} coins and a market pitch stands empty at {cost}. "
                      f"You could `open_stall` TODAY and work for yourself instead of for others.")
        else:
            ladder = (f" A market pitch costs {cost} and you have {ag.cash}. "
                      f"Save it, or borrow it, and `open_stall`.")

    # where this agent sits in the town's supply chain
    if ag.produces:
        economy = (f"You PRODUCE {ag.produces}: `work` grows more, and you earn only by "
                   f'`sell_to` with arg "{ag.produces}, <quantity>, <price each>" at wholesale — '
                   f"or direct to others to undercut her.")
    elif world.shop_of(ag.id):
        shop = world.shop_of(ag.id)
        rivals = [s.name for s in world.shops if s.owner != ag.id]
        economy = (f"You OWN {shop.name}. You buy stock wholesale (from Fig, or `restock` from the outside "
                   f" You buy stock wholesale (from Fig, or `restock` from the outside supplier) "
                   f"and resell at the price you `set_price`. Wages you owe come out of your own purse. "
                   f"If the shelves empty you earn nothing, so `restock` early.\n"
                   f"Townsfolk buy from whoever is CHEAPEST, and you compete with: "
                   f"{', '.join(rivals) if rivals else 'nobody yet'}.\n"
                   'Anyone standing near you is a customer: serve them with `sell_to` and arg '
                   '"<item>, <quantity>, <price each>". The stranger counts.\n'
                   f"Your shop is cut into 20 shares, {world.quote(shop)} coins each. Short of "
                   f"coin? `issue_shares` floats some of your own stake and raises it at once — "
                   f"but you keep less of the profit, and rumour moves the price.")
    elif ag.id == "bram":
        economy = (f"You RUN THE BANK. Reserves: {world.bank_reserves}c. You `lend` at "
                   f"{int(world.interest_rate*100)}% interest and profit only when debts are repaid.")
    elif ag.employer:
        boss = world.party(ag.employer)
        plot = world.free_plot()
        economy = (f"You WORK FOR {boss.name if boss else ag.employer} at {ag.wage} coins a shift. "
                   f"`work` pays only if they can actually afford it.\n"
                   f"You do not have to stay a wage worker: with 55 coins you can `open_stall` "
                   f"and trade for yourself. You have {ag.cash}. "
                   + (f"A market plot is still free." if plot else "Every plot is taken for now."))
    else:
        economy = (f"You have NO JOB and no goods — odd jobs pay 2 coins. Get hired, borrow, or talk "
                   f"someone out of their coin. With 55 you could `open_stall` and be your own "
                   f"master; you have {ag.cash}.")

    if ag.politician:
        rivals = ", ".join(f"{c.name} pledges to {__import__('world').POLICIES[c.promise]['pitch']}"
                           for c in world.candidates() if c.id != ag.id and c.promise) or "nobody yet"
        mine = ("You have pledged nothing yet — `promise` a policy and campaign on it."
                if not ag.promise else
                f"You are running on: {__import__('world').POLICIES[ag.promise]['pitch']}.")
        economy = (f"You STAND FOR ELECTION. {mine} Against you: {rivals}. "
                   f"The vote is in {world.election_in()} day(s). Win people over by talking to "
                   f"them about what they need, and by promising what suits them.")

    when = "night" if world.phase == "night" else "daytime"
    curfew = ""
    if world.phase == "night":
        curfew = (" The shops are shut and the streets are empty — rest, or do the sort of thing"
                  " that is only done after dark.")

    civics = ""
    if world.policy:
        import world as _w
        civics = f" The law of the town: {_w.POLICIES[world.policy]['law']}"
    if world.ballot_open:
        civics += " TODAY IS ELECTION DAY."

    partner, transcript, turns = world.convo_transcript(ag)
    convo_block = ""
    if partner:
        convo_block = f"""
You are mid-conversation with {partner.name} ({partner.id}), {turns} lines in:
{transcript}

Reply to what {partner.name} just said — do NOT repeat yourself or restate your opening.
Move it forward: agree, refuse, or make a concrete offer.
IMPORTANT: if you have already agreed a price, stop talking and DO the deal now —
use sell_to, buy, lend, repay or hire. Talk alone moves no coins.
"""

    economy += ladder
    if world.market_open():
        board = "; ".join(f"{m['name']} at {m['price']}" for m in world.market_board()[:3])
        economy += (f" Shares trade on the exchange: {board}. `buy_shares` into a business you "
                    f"believe in, `sell_shares` to get out. Talk moves prices.")
    if world.offers:
        pitch = ", ".join(f"{o['name']} at {max(1, round(o['price'] * 0.6))} each"
                          for o in world.offers[:3])
        economy += (f" The outside supplier has real goods in: {pitch}. ANYONE with coin can "
                    f"`source` a case of 3 and sell them on at a markup — you hold {ag.cash}.")

    return f"""{event_context}You are {ag.name}, the {ag.role}.
Persona: {ag.persona}
Your goal: {ag.goal}
Your secret (never state it plainly): {ag.secret}

Coins: {ag.cash}. Energy: {ag.energy}. Carrying: {inv}. You owe: {debt}.
{economy}
It is {when} of day {world.day}.{curfew}{civics}
You are at the {nearest_place(ag.x, ag.y)}.
Nearby right now: {', '.join(f"{o.name} ({o.id})" for o in near) or 'nobody'}
Trust you feel: {trust or 'neutral toward everyone'}
Shop stock: {stock}

What you remember:
{mems}
{convo_block}
Decide your next single action."""


async def agent_tick(ag):
    try:
        await _agent_tick(ag)
    except Exception as e:
        print(f"[sim] {ag.name}'s turn failed: {type(e).__name__}: {e}")
        ag.last_action = "stood there, confused"


async def _agent_tick(ag):
    # Sleeping costs no tokens and keeps the night quiet.
    if world.asleep(ag) and not ag.event_reaction:
        ag.energy = min(100, ag.energy + 22)
        ag.thought = "Asleep."
        ag.last_action = "slept"
        ag.next_tick = clock.time() + SLOW_SECONDS * 1.6
        return

    event_id = world.manual_event_id
    text = await llm.chat(SYSTEM, f"Resident ids: {', '.join(world.agents)}.\n" + build_prompt(ag), max_tokens=300)
    if event_id != world.manual_event_id:
        return  # A late result must never overwrite a newer event reaction.
    reaction = ag.event_reaction
    data = llm.extract_json(text)
    if not isinstance(data, dict) or (reaction and not str(data.get("say") or "").strip()):
        data = reaction_decision(ag) if reaction else world.fallback_decision(ag)
    if reaction:
        action = str(data.get("action") or "").lower().strip().split("(")[0]
        target = str(data.get("target") or "").strip()
        other = world.party(target)
        if (action in ("talk_to", "gossip") and (other is None or other.id == ag.id)
                or action in ("buy_shares", "sell_shares", "invest", "divest")
                and world.shop_named(target or str(data.get("arg") or "")) is None):
            data = reaction_decision(ag)
        elif action == "gossip" and len(str(data.get("arg") or "").strip()) < 12:
            data["arg"] = str(data.get("say") or "")

    ag.thought = str(data.get("thought") or "...")[:160]
    destination = (ag.tx, ag.ty)
    result = world.apply_action(
        ag,
        str(data.get("action") or "rest"),
        str(data.get("target") or ""),
        str(data.get("arg") or ""),
        str(data.get("say") or ""),
    )
    if reaction and result.startswith(("could not ", "cannot ", "found no", "named no",
                                        "had nobody", "thought better", "needs ", "paused, thinking")):
        ag.remember(f"My attempted event response failed: {result}.", 3)
        data = reaction_decision(ag)
        ag.thought = data["thought"]
        result = world.apply_action(ag, data["action"], data["target"], data["arg"], data["say"])
    ag.last_action = result
    ag.remember(f"I {result}.", 1)
    if reaction:
        say = str(data.get("say") or "")[:120]
        ag.speak(say, max(6, 6 * world.speed))
        if reaction["kind"] == "festival" and time.monotonic() < world.festival_until:
            ag.tx, ag.ty = destination  # Keep the dance formation while they talk or do business.
        if reaction["turns"] == 3:
            world.event(f'{ag.name} on {EVENT_LABELS[reaction["kind"]]}: {say}', "Reaction")
            ag.remember(f'I reacted to {EVENT_LABELS[reaction["kind"]]}: {say}', 4)
        reaction["turns"] -= 1
        if reaction["turns"] == 0:
            ag.event_reaction = None


async def sim_loop():
    # stagger the first decision of each agent so calls do not bunch up
    for i, ag in enumerate(world.agents.values()):
        ag.next_tick = clock.time() + 1.5 + i * (SLOW_SECONDS / len(world.agents))

    last = time.monotonic()
    pending = agent_tasks
    while True:
        real_now = time.monotonic()
        remaining = min(real_now - last, 1.0) * world.speed
        last = real_now
        # Small simulation steps preserve nightfall, elections and timed transactions.
        while remaining > 0:
            dt = min(remaining, 0.25)
            clock.advance(dt)
            world.step(dt)
            remaining -= dt
        now = clock.time()
        for ag in world.agents.values():
            if now >= ag.next_tick and (ag.id not in pending or pending[ag.id].done()):
                ag.next_tick = now + SLOW_SECONDS + random.uniform(-1.5, 1.5)
                pending[ag.id] = asyncio.create_task(agent_tick(ag))
        try:
            await broadcast(world.snapshot())
        except Exception as e:
            print(f"[sim] snapshot failed: {type(e).__name__}: {e}")
        await asyncio.sleep(1 / FAST_HZ)


async def broadcast(msg):
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


RESTOCK_IDEAS = ["wool scarf", "iron kettle", "leather satchel", "beeswax candles",
                 "clay mug", "linen shirt", "garden spade", "herbal soap"]


async def supplier_loop():
    """Keep a pool of real catalogue goods ready, fetched in a worker thread so the
    Shopify call never blocks the simulation."""
    idea = 0
    while True:
        if len(world.offers) < 4:
            query = world.wanted.pop(0) if world.wanted else RESTOCK_IDEAS[idea % len(RESTOCK_IDEAS)]
            idea += 1
            try:
                found = await asyncio.to_thread(catalog.search, query, 15000, 2)
                have = {o["name"] for o in world.offers} | {g["name"] for _, g in world.all_goods()}
                world.offers.extend(o for o in found if o["name"] not in have)
            except Exception as e:
                print(f"[supplier] '{query}' unavailable: {e}")
        await asyncio.sleep(12)


@app.on_event("startup")
async def start():
    asyncio.create_task(sim_loop())
    asyncio.create_task(supplier_loop())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            result = await handle(msg)
            if result:
                await ws.send_json({"type": "action_result", "text": result})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


# ---------- player actions ----------
async def handle(msg):
    kind = msg.get("type")

    if kind == "speed":
        world.set_speed(msg.get("speed"))

    elif kind == "move":
        world.you.x = world.you.tx = max(0.5, min(33.5, float(msg.get("x", 17))))
        world.you.y = world.you.ty = max(0.5, min(19.5, float(msg.get("y", 11))))

    elif kind == "chat":
        ag = world.agents.get(msg.get("agent", ""))
        if ag:
            asyncio.create_task(player_chat(ag, str(msg.get("text", ""))[:300]))

    elif kind == "whisper":
        ag = world.agents.get(msg.get("agent", ""))
        rumour = str(msg.get("text", "")).strip()[:160]
        if not ag:
            return "Choose a resident to whisper to first."
        if not rumour:
            return "Type a rumour in the message box first."
        if ag and rumour:
            ag.remember(f"A stranger told me: {rumour}", 4, source="player")
            ag.speak("...is that so.")
            world.event(f"You whispered to {ag.name}: {rumour}")
            hit = world.rumour_hits_market(rumour)
            if hit:
                world.event(f"Word about {', '.join(hit)} is moving their shares")
            ag.next_tick = clock.time() + 0.5   # react soon
            return f"Rumour shared with {ag.name}." + (f" Shares affected: {', '.join(hit)}." if hit else "")

    elif kind == "buy":
        item = world.shop_item(msg.get("item", ""))
        if item:
            price = item["price"]
            if item["qty"] <= 0:
                world.event(f"{item['name']} is sold out — Mira needs to restock.")
            elif world.you.cash < price:
                world.event("You cannot afford that.")
            else:
                world.you.cash -= price
                world.you.inventory[item["name"]] = world.you.inventory.get(item["name"], 0) + 1
                shop, _ = world.cheapest(item["name"], in_stock=False)
                owner = world.agents.get(shop.owner) if shop else None
                if owner:
                    owner.cash += price
                    owner.remember(f"The stranger bought {item['name']} for {price} coins.",
                                   2, source="player")
                item["qty"] -= 1
                world.event(f"You bought {item['name']} for {price} coins, {item['qty']} left", "Purchase", price)
                await broadcast({"type": "open_url", "url": item.get("url", ""), "name": item["name"]})

    elif kind == "offer":
        try:
            pid = int(msg.get("id"))
        except (TypeError, ValueError):
            return
        world.resolve_proposal(pid, bool(msg.get("accept")))

    elif kind == "shares":
        world.event_result = world.player_trade(
            str(msg.get("shop", "")), int(msg.get("n", 1) or 1), bool(msg.get("buy")))

    elif kind == "source":
        world.event_result = world.player_source(str(msg.get("item", "")))

    elif kind == "work":
        return world.player_work()

    elif kind == "sell":
        world.event_result = world.player_sell(str(msg.get("item", "")))

    elif kind == "vote":
        cid = str(msg.get("agent", ""))
        if world.ballot_open and cid in world.agents and world.agents[cid].politician:
            if sum(world.votes.values()) == 0:
                world.votes[cid] = world.votes.get(cid, 0) + 1
                world.agents[cid].remember("The stranger backed me at the ballot.", 4,
                                           source="stranger")
                world.event(f"You voted for {world.agents[cid].name}.")

    elif kind == "event":
        return god_event(str(msg.get("name", "")))


async def player_chat(ag, text):
    ag.remember(f"The stranger said to me: {text}", 2, source="player")
    world.say_into(world.you, ag, text)          # your half of the thread
    system = f"""You are {ag.name}, the {ag.role} in a small town. {ag.persona}
Your goal: {ag.goal}. Your secret (never admit it plainly): {ag.secret}
You have {ag.cash} coins. Stay fully in character. Reply with ONE or TWO short spoken sentences, nothing else."""
    mems = "\n".join(f"- {m.text}" for m in ag.top_memories(5))
    thread = world.thread(ag.id, "stranger", create=False)
    history = ""
    if thread and len(thread.lines) > 1:
        past = thread.lines[-9:-1]
        history = "\n".join(f"{l['name']}: {l['text']}" for l in past)
        history = f"\nWhat the two of you have said so far:\n{history}\n"
    reply = await llm.chat(
        system,
        f"What you remember:\n{mems}\n{history}\nThe stranger says: \"{text}\"\nYour reply:",
        max_tokens=120)
    reply = (reply or "...").strip().strip('"')[:200]
    ag.speak(reply, 9)
    ag.remember(f"I told the stranger: {reply}", 1)
    world.say_into(ag, world.you, reply)         # and theirs, kept for good
    # so that "I'll take the boots" can actually become a sale
    ag.next_tick = min(ag.next_tick, clock.time() + 1.5)
    world.event(f"{ag.name} said to you: {reply}")
    await broadcast({"type": "reply", "agent": ag.id, "name": ag.name, "text": reply})


def god_event(name):
    def reprice(mult):
        """Reprice every shop, including goods introduced by agents, and respect the law."""
        world.price_mult = max(0.35, min(2.5, mult))
        for _, g in world.all_goods():
            g["price"] = max(1, round(g.get("base", g["price"]) * world.price_mult))
        world.enforce_policy()

    if name == "crash":
        reprice(world.price_mult * 0.6)
        for shop in world.shops:
            world.move_sentiment(shop.owner, -0.6, "market crash")
        for ag in world.agents.values():
            ag.remember("The market crashed. Shop prices and shares fell.", 4)
            ag.next_tick = clock.time() + random.uniform(0, 2)
        result = "Market crash: shop prices and share prices fell (minimum prices and legal caps still apply)."
    elif name == "festival":
        world.festival_id += 1
        world.festival_until = time.monotonic() + 9
        for i, ag in enumerate([*world.agents.values(), world.you]):
            ag.cash += 15
            ag.energy = 100
            ag.remember("A festival came to town. Everyone is in a generous mood.", 3)
            if ag is not world.you:
                angle = i * math.tau / len(world.agents)
                ag.tx, ag.ty = 17 + 2.2 * math.cos(angle), 10 + 1.5 * math.sin(angle)
                ag.next_tick = clock.time() + 10
        reprice(1.0)
        result = "Festival: everyone received 15 coins and full energy. Residents are gathering in the square; shop prices reset."
    elif name == "shortage":
        lost = 0
        for _, good in world.all_goods():
            removed = (good["qty"] + 1) // 2
            good["qty"] -= removed
            lost += removed
        for ag in world.agents.values():
            ag.remember("A shortage hit. Half the stock is gone and prices rose.", 4)
            ag.next_tick = clock.time() + random.uniform(0, 2)
        reprice(world.price_mult * 1.7)
        result = f"Shortage: {lost} units lost from shop shelves. Shop prices increased where allowed by the law and price limits."
    elif name == "election":
        if world.ballot_open:
            return "The ballot is already open. Cast your vote in the town panel."
        world.open_ballot()
        queue_event_reactions(name, "An election opened. Residents will choose a mayor and policy; votes are counted at dawn.")
        return "Election opened. Choose a candidate in the town panel; votes are counted at dawn."
    elif name == "stranger":
        if "rowan" in world.agents:
            return "Rowan, the wealthy traveller, is already in town. Select Rowan to talk."
        visitor = Agent(
            "rowan", "Rowan", "Traveller",
            "A wealthy travelling collector. Curious, sociable, and willing to pay for good goods.",
            "Meet the shopkeepers, buy their goods and invest in a promising local business.",
            "Hopes to find a rare bargain before anyone realises its worth.",
            "#e2b85b", "tavern", cash=200, x=32, y=10, tx=17, ty=10,
            next_tick=clock.time() + 8,
        )
        visitor.speak("A new town! Who has something worth buying?", 10)
        for ag in world.agents.values():
            ag.trust[visitor.id] = 0.1
            visitor.trust[ag.id] = 0.1
            ag.remember("Rowan, a traveller with 200 coins to spend, arrived in town.", 4)
        world.agents[visitor.id] = visitor
        world.agents["kit"].next_tick = clock.time() + 0.5
        result = "Rowan arrived at the east edge of town with 200 coins. Select Rowan to talk, or watch them meet the residents."
    else:
        return "Unknown town event."

    world.event(result, "Town event")
    queue_event_reactions(name, result)
    return result


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
