"""Untitled Town server: owns the world, runs the agent loops, pushes state over WebSocket."""
import asyncio, json, os, random, time, pathlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

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
    stock = ", ".join(
        f"{i['name']} {world.prices.get(i['name'], i['price'])}c"
        f"(cost {i['cost']}c, {i['qty']} left)" for i in world.stock[:8])
    inv = ", ".join(f"{k} x{v}" for k, v in ag.inventory.items() if v) or "nothing"
    debt = ", ".join(f"{k} {v}c" for k, v in ag.debts.items() if v) or "none"
    # where this agent sits in the town's supply chain
    if ag.produces:
        economy = (f"You PRODUCE {ag.produces}: `work` grows more, and you earn only by "
                   f'`sell_to` with arg "{ag.produces}, <quantity>, <price each>" at wholesale — '
                   f"or direct to others to undercut her.")
    elif ag.id == "mira":
        economy = ("You RUN THE SHOP. You buy stock wholesale (from Fig, or `restock` from the outside "
                   "supplier) and resell at the price you `set_price`. Wages you owe come out of your own "
                   "purse. Stock runs out — if the shelves empty you earn nothing.")
    elif ag.id == "bram":
        economy = (f"You RUN THE BANK. Reserves: {world.bank_reserves}c. You `lend` at "
                   f"{int(world.interest_rate*100)}% interest and profit only when debts are repaid.")
    elif ag.employer:
        boss = world.agents.get(ag.employer)
        economy = (f"You WORK FOR {boss.name if boss else ag.employer} at {ag.wage} coins a shift. "
                   f"`work` pays only if they can actually afford it. Spend your wages at the shop.")
    else:
        economy = ("You have NO JOB and no goods — odd jobs pay 2 coins. Get hired, borrow, or talk "
                   "someone out of their coin.")

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

    return f"""You are {ag.name}, the {ag.role}.
Persona: {ag.persona}
Your goal: {ag.goal}
Your secret (never state it plainly): {ag.secret}

Coins: {ag.cash}. Energy: {ag.energy}. Carrying: {inv}. You owe: {debt}.
{economy}
You are at the {nearest_place(ag.x, ag.y)}.
Nearby right now: {', '.join(f"{o.name} ({o.id})" for o in near) or 'nobody'}
Trust you feel: {trust or 'neutral toward everyone'}
Shop stock: {stock}

What you remember:
{mems}
{convo_block}
Decide your next single action."""


async def agent_tick(ag):
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
        await broadcast(world.snapshot())
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


@app.on_event("startup")
async def start():
    asyncio.create_task(sim_loop())


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
        world.player["x"] = max(0.5, min(33.5, float(msg.get("x", 17))))
        world.player["y"] = max(0.5, min(19.5, float(msg.get("y", 11))))

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
            price = world.prices.get(item["name"], item["price"])
            if item["qty"] <= 0:
                world.event(f"{item['name']} is sold out — Mira needs to restock.")
            elif world.player["cash"] < price:
                world.event("You cannot afford that.")
            else:
                world.player["cash"] -= price
                world.player["inventory"][item["name"]] = world.player["inventory"].get(item["name"], 0) + 1
                world.agents["mira"].cash += price
                item["qty"] -= 1
                world.agents["mira"].remember(
                    f"The stranger bought {item['name']} for {price}c.", 2, source="player")
                world.event(f"You bought {item['name']} for {price} coins, {item['qty']} left")
                await broadcast({"type": "open_url", "url": item.get("url", ""), "name": item["name"]})

    elif kind == "event":
        god_event(str(msg.get("name", "")))


async def player_chat(ag, text):
    ag.remember(f"The stranger said to me: {text}", 2, source="player")
    system = f"""You are {ag.name}, the {ag.role} in a small town. {ag.persona}
Your goal: {ag.goal}. Your secret (never admit it plainly): {ag.secret}
You have {ag.cash} coins. Stay fully in character. Reply with ONE or TWO short spoken sentences, nothing else."""
    mems = "\n".join(f"- {m.text}" for m in ag.top_memories(5))
    reply = await llm.chat(system, f"What you remember:\n{mems}\n\nA stranger says to you: \"{text}\"\nYour reply:", max_tokens=120)
    reply = (reply or "...").strip().strip('"')[:200]
    ag.speak(reply, 9)
    ag.remember(f"I told the stranger: {reply}", 1)
    world.event(f"{ag.name} → You: {reply}")
    await broadcast({"type": "reply", "agent": ag.id, "name": ag.name, "text": reply})


def god_event(name):
    def reprice(mult):
        """Prices always derive from base × a clamped multiplier — never a one-way ratchet."""
        world.price_mult = max(0.35, min(2.5, mult))
        for k, base in world.base_prices.items():
            world.prices[k] = max(1, round(base * world.price_mult))

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
