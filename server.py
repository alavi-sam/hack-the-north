"""Untitled Town server: owns the world, runs the agent loops, pushes state over WebSocket."""
import asyncio, json, os, random, time, pathlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import catalog
import llm
from world import World, ACTIONS, PLACES, nearest_place

HERE = pathlib.Path(__file__).parent
app = FastAPI()
world = World()
clients: set[WebSocket] = set()

FAST_HZ = 12          # movement/animation
SLOW_SECONDS = float(os.getenv("SLOW_SECONDS", "11"))  # per-agent decision interval, staggered

SYSTEM = """You are a resident of a small town in a simulation. Stay in character.
Reply with ONLY a JSON object, no prose, no markdown fence:
{"thought": "one short private thought", "action": "<one action>", "target": "<agent id or place or item>", "arg": "<extra, may be empty>", "say": "<one short line spoken aloud, may be empty>"}
Valid actions: """ + ACTIONS + """
Places: shop, bank, farm, tavern, square. Agent ids: mira, bram, wren, fig, kit.
Pursue your goal. Be specific and a little dramatic. Never invent coins or items you do not have.
Coins are WHOLE numbers — never 1.75. When you trade, name a QUANTITY and a price PER UNIT,
and put them in arg as "item, quantity, price" (e.g. "Apples, 50, 2" = fifty apples at 2c each).
Keep "thought" under 15 words and "say" under 20 words."""


def build_prompt(ag):
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
    if not world.shop_of(ag.id):
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
                   '"<item>, <quantity>, <price each>". The stranger counts.')
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

    return f"""You are {ag.name}, the {ag.role}.
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
    if world.asleep(ag):
        ag.energy = min(100, ag.energy + 22)
        ag.thought = "Asleep."
        ag.last_action = "slept"
        ag.next_tick = time.time() + SLOW_SECONDS * 1.6
        return

    text = await llm.chat(SYSTEM, build_prompt(ag), max_tokens=300)
    data = llm.extract_json(text) or world.fallback_decision(ag)

    ag.thought = str(data.get("thought") or "...")[:160]
    result = world.apply_action(
        ag,
        str(data.get("action") or "rest"),
        str(data.get("target") or ""),
        str(data.get("arg") or ""),
        str(data.get("say") or ""),
    )
    ag.last_action = result
    ag.remember(f"I {result}.", 1)


async def sim_loop():
    # stagger the first decision of each agent so calls do not bunch up
    for i, ag in enumerate(world.agents.values()):
        ag.next_tick = time.time() + 1.5 + i * (SLOW_SECONDS / len(world.agents))

    last = time.time()
    while True:
        now = time.time()
        world.step(now - last)
        last = now
        for ag in world.agents.values():
            if now >= ag.next_tick:
                ag.next_tick = now + SLOW_SECONDS + random.uniform(-1.5, 1.5)
                asyncio.create_task(agent_tick(ag))
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
            await handle(msg)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


# ---------- player actions ----------
async def handle(msg):
    kind = msg.get("type")

    if kind == "move":
        world.you.x = world.you.tx = max(0.5, min(33.5, float(msg.get("x", 17))))
        world.you.y = world.you.ty = max(0.5, min(19.5, float(msg.get("y", 11))))

    elif kind == "chat":
        ag = world.agents.get(msg.get("agent", ""))
        if ag:
            asyncio.create_task(player_chat(ag, str(msg.get("text", ""))[:300]))

    elif kind == "whisper":
        ag = world.agents.get(msg.get("agent", ""))
        rumour = str(msg.get("text", ""))[:160]
        if ag and rumour:
            ag.remember(f"A stranger told me: {rumour}", 4, source="player")
            ag.speak("...is that so.")
            world.event(f"You whispered to {ag.name}: {rumour}")
            ag.next_tick = time.time() + 0.5   # react soon

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
                world.event(f"You bought {item['name']} for {price} coins, {item['qty']} left")
                await broadcast({"type": "open_url", "url": item.get("url", ""), "name": item["name"]})

    elif kind == "offer":
        try:
            pid = int(msg.get("id"))
        except (TypeError, ValueError):
            return
        world.resolve_proposal(pid, bool(msg.get("accept")))

    elif kind == "work":
        world.event_result = world.player_work()

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
        god_event(str(msg.get("name", "")))


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
    ag.next_tick = min(ag.next_tick, time.time() + 1.5)
    world.event(f"{ag.name} said to you: {reply}")
    await broadcast({"type": "reply", "agent": ag.id, "name": ag.name, "text": reply})


def god_event(name):
    def reprice(mult):
        """Prices always derive from base × a clamped multiplier — never a one-way ratchet."""
        world.price_mult = max(0.35, min(2.5, mult))
        for _, g in world.all_goods():
            g["price"] = max(1, round(g["base"] * world.price_mult))

    if name == "crash":
        reprice(world.price_mult * 0.6)
        for ag in world.agents.values():
            ag.remember("The market crashed. Prices collapsed overnight.", 4)
            ag.next_tick = time.time() + random.uniform(0, 2)
        world.event("The market crashed; prices collapsed overnight.")
    elif name == "festival":
        for ag in world.agents.values():
            ag.cash += 15
            ag.energy = 100
            ag.remember("A festival came to town. Everyone is in a generous mood.", 3)
            ag.tx, ag.ty = 17, 10
        reprice(1.0)
        world.event("A festival came to town; everyone drifted to the square and prices settled.")
    elif name == "shortage":
        for ag in world.agents.values():
            ag.remember("Word is there is a shortage coming. Stock will run out.", 4)
            ag.next_tick = time.time() + random.uniform(0, 2)
        reprice(world.price_mult * 1.7)
        world.event("Word of a shortage spread; prices spiked.")
    elif name == "stranger":
        world.agents["kit"].remember("A wealthy stranger arrived in town. An opportunity.", 4)
        world.agents["kit"].next_tick = time.time() + 0.5
        world.event("A wealthy stranger arrived in town.")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
