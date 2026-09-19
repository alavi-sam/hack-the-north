"""Untitled Town: world state, agents, economy. The server is the single source of truth."""
import random, time, json
from dataclasses import dataclass, field

import catalog

TILE = 24
MAP_W, MAP_H = 34, 20

# name -> (tile x, tile y, w, h, colour) — the buildings agents walk between
PLACES = {
    "shop":   (3, 3, 7, 5, "#8b5a2b"),
    "bank":   (24, 3, 7, 5, "#4a6fa5"),
    "farm":   (3, 13, 8, 5, "#4a7c3f"),
    "tavern": (24, 13, 7, 5, "#7a4a6a"),
    "square": (14, 8, 6, 5, "#6b6b52"),
}


def place_center(name):
    x, y, w, h, _ = PLACES[name]
    return (x + w / 2, y + h / 2)


def parse_offer(arg, target):
    """Read 'Apples, 50, 2' — or 'Apples, 2' — into (item, quantity, unit price).

    Models write offers loosely, so pull the words out for the item and the numbers
    out for the figures: two numbers means quantity then unit price, one means price.
    """
    parts = [x.strip() for x in str(arg or "").split(",") if x.strip()]
    words, numbers = [], []
    for part in parts:
        try:
            numbers.append(float(part.replace("c", "").replace("coins", "").strip()))
        except ValueError:
            words.append(part)
    name = words[0] if words else (target or "")
    if len(numbers) >= 2:
        qty, unit = int(numbers[0]), numbers[1]
    elif len(numbers) == 1:
        qty, unit = 1, numbers[0]
    else:
        qty, unit = 1, None
    return name, max(1, min(99, qty)), unit


def nearest_place(x, y):
    return min(PLACES, key=lambda p: (place_center(p)[0] - x) ** 2 + (place_center(p)[1] - y) ** 2)


@dataclass
class Memory:
    text: str
    t: float
    importance: int = 1
    source: str = "self"


MAX_CONVO_LINES = 6      # a conversation ends after this many lines
CONVO_COOLDOWN = 50.0    # seconds before the same pair may start talking again


@dataclass
class Conversation:
    id: int
    a: str
    b: str
    lines: list = field(default_factory=list)   # {"speaker", "name", "text"}
    started: float = 0.0
    closed: bool = False

    @property
    def pair(self):
        return frozenset((self.a, self.b))


@dataclass
class Agent:
    id: str
    name: str
    role: str
    persona: str
    goal: str
    secret: str
    color: str
    home: str
    cash: int = 50
    x: float = 17.0
    y: float = 10.0
    tx: float = 17.0
    ty: float = 10.0
    energy: int = 100
    inventory: dict = field(default_factory=dict)
    memories: list = field(default_factory=list)
    trust: dict = field(default_factory=dict)
    debts: dict = field(default_factory=dict)   # who they owe -> amount
    thought: str = "..."
    last_action: str = "idle"
    say: str = ""
    say_until: float = 0.0
    next_tick: float = 0.0
    last_restock: float = 0.0
    employer: str = ""      # who pays this agent's wage
    wage: int = 0           # coins per work cycle, paid FROM the employer's purse
    produces: str = ""      # a producer makes this good instead of earning a wage

    def remember(self, text, importance=1, source="self"):
        self.memories.append(Memory(text, time.time(), importance, source))
        if len(self.memories) > 60:
            self.memories = self.memories[-60:]

    def top_memories(self, n=6):
        """Recent + important. Cheap stand-in for embedding retrieval."""
        now = time.time()
        scored = sorted(
            self.memories,
            key=lambda m: m.importance * 2 - (now - m.t) / 60,
            reverse=True,
        )
        return scored[:n]

    def speak(self, text, seconds=6.0):
        self.say = text[:120]
        self.say_until = time.time() + seconds


def make_agents():
    a = [
        Agent("mira", "Mira", "Merchant", "Runs the town shop. Brisk, proud, talks in short clipped sentences.",
              "Grow profit and never run out of stock.", "Overprices when she thinks nobody is comparing.",
              "#e0a44a", "shop", cash=120),
        Agent("bram", "Bram", "Banker", "Runs the bank. Formal, slow-spoken, fond of being flattered.",
              "Keep the bank solvent and lend at a profit.", "Quietly favours people who flatter him.",
              "#6fa8dc", "bank", cash=300),
        Agent("wren", "Wren", "Worker", "Hard-working labourer. Earnest, tired, speaks plainly.",
              "Save enough coin to open her own stall.", "Deep in debt to Bram and hides it.",
              "#9fd18a", "farm", cash=18, employer="mira", wage=6),
        Agent("fig", "Fig", "Supplier", "Grows produce and supplies the shop. Warm on the surface, sharp underneath.",
              "Sell produce to the shop at a good wholesale price.",
              "Secretly undercuts Mira by selling direct, to hurt her business.",
              "#c78a5a", "farm", cash=60, produces="Apples"),
        Agent("kit", "Kit", "Drifter", "A drifter and con artist. Charming, evasive, never answers straight.",
              "Get rich without ever working.", "Lies about who they are and spreads false rumours.",
              "#c06fc0", "tavern", cash=8),
    ]
    for ag in a:
        ag.x, ag.y = ag.tx, ag.ty = place_center(ag.home)
        for other in a:
            if other.id != ag.id:
                ag.trust[other.id] = round(random.uniform(-0.1, 0.35), 2)
    # seeded drama
    byid = {x.id: x for x in a}
    byid["wren"].debts["bram"] = 40
    byid["wren"].remember("I owe Bram 40 coins. Nobody can know.", 3)
    byid["bram"].remember("Wren owes me 40 coins and is behind on it.", 3)
    byid["fig"].remember("Mira's prices are robbery. I can undercut her.", 3)
    byid["kit"].remember("Wren looks like she is hiding money trouble. Useful.", 3)
    byid["mira"].remember("Fig has been selling produce cheap behind my back.", 2)
    byid["mira"].remember("Wren works for me at 6 coins a shift. I pay her from the till.", 2)
    byid["wren"].remember("I work for Mira at 6 coins a shift.", 2)
    byid["fig"].remember("I grow apples and sell them wholesale to Mira's shop.", 2)
    byid["fig"].trust["mira"] = -0.4
    byid["mira"].trust["fig"] = -0.3
    byid["kit"].trust["wren"] = 0.3
    return {x.id: x for x in a}


ACTIONS = """move_to(place) | talk_to(agent, intent) | work | rest | gossip(agent, about, claim) |
buy(item)                      — buy one unit from Mira's shop at the retail price
sell_to(agent, item, qty, price)  — sell goods you own (arg = "item, quantity, price EACH")
buy_from(agent, item, qty, price) — buy goods off another agent (arg = "item, quantity, price EACH")
pay(agent, amount)             — hand over coin: a bribe, hush money, a gift, a deal you struck
set_price(item, price)         — shopkeeper only; between cost and 3x cost (arg = "item, price")
restock(query)                 — Mira only; buy new stock from the outside supplier, costs coin
hire(agent, wage)              — offer someone a job you pay for (arg = wage)
lend(agent, amount)            — bank only; charges interest
repay(agent)                   — pay down what you owe, interest included"""


class World:
    def __init__(self):
        self.agents = make_agents()
        self.stock = catalog.load_stock()
        # local produce sits in the same shop list, just without a real product link
        for name, price in (("Bread", 4), ("Apples", 3)):
            self.stock.append({"name": name, "real_title": f"Local {name}",
                               "price": price, "url": "", "image": ""})
        # every item now has a cost basis and a finite quantity, so the shop can sell out
        for it in self.stock:
            it["cost"] = max(1, round(it["price"] * 0.6))
            it["qty"] = 6
        self.prices = {it["name"]: it["price"] for it in self.stock}
        self.base_prices = dict(self.prices)   # crash/shortage scale off this, never compound
        self.price_mult = 1.0
        self.log = []
        self.conversations = []
        self.convo_seq = 0
        self.convo_cooldown = {}               # frozenset(pair) -> time it may restart
        self.bank_reserves = 300
        self.interest_rate = 0.2      # what Bram charges on a loan
        self.townsfolk_next = time.time() + 6
        self.flows = []               # recent coin movements, for the Economy panel
        self.last_paid = {}           # (payer, payee) -> time, so handouts cannot loop
        self.day = 1
        self.started = time.time()
        self.player = {"x": 17.0, "y": 11.0, "cash": 100, "inventory": {}, "name": "Stranger"}
        self.event("The town wakes up.")

    # ---------- helpers ----------
    def pay(self, payer, payee, amount, why):
        """Move coin between two purses. Returns the amount actually paid (0 if broke)."""
        amount = int(max(0, amount))
        if amount <= 0 or payer.cash < amount:
            return 0
        payer.cash -= amount
        payee.cash += amount
        self.flows.append({"t": time.strftime("%H:%M:%S"), "from": payer.name,
                           "to": payee.name, "amount": amount, "why": why})
        if len(self.flows) > 40:
            self.flows = self.flows[-40:]
        return amount

    def stock_of(self, name):
        for it in self.stock:
            if it["name"].lower() == (name or "").lower():
                return it
        return None

    def event(self, text):
        self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
        if len(self.log) > 120:
            self.log = self.log[-120:]

    def shop_item(self, name):
        name = (name or "").strip().lower()
        for it in self.stock:
            if it["name"].lower() == name:
                return it
        for it in self.stock:
            if name and name in it["name"].lower():
                return it
        return None

    def nearby(self, agent, radius=4.0):
        return [o for o in self.agents.values()
                if o.id != agent.id and (o.x - agent.x) ** 2 + (o.y - agent.y) ** 2 < radius ** 2]

    def active_convo(self, a_id, b_id):
        pair = frozenset((a_id, b_id))
        for c in reversed(self.conversations):
            if not c.closed and c.pair == pair:
                return c
        return None

    def open_convo(self, a_id, b_id):
        self.convo_seq += 1
        c = Conversation(self.convo_seq, a_id, b_id, started=time.time())
        self.conversations.append(c)
        if len(self.conversations) > 24:
            self.conversations = self.conversations[-24:]
        return c

    def close_convo(self, c):
        """End a thread: each side keeps one summarising memory and nudges trust."""
        c.closed = True
        self.convo_cooldown[c.pair] = time.time() + CONVO_COOLDOWN
        a, b = self.agents.get(c.a), self.agents.get(c.b)
        if not (a and b):
            return
        for me, them in ((a, b), (b, a)):
            said = [l["text"] for l in c.lines if l["speaker"] == them.id]
            gist = said[-1] if said else "not much"
            me.remember(f"I spoke with {them.name}. They said: {gist}", 2, source=them.id)
            self.adjust_trust(me, them.id, 0.04)

    def convo_transcript(self, ag, limit=6):
        """The open conversation this agent is in, rendered for their prompt."""
        for c in reversed(self.conversations):
            if not c.closed and ag.id in c.pair:
                other_id = c.b if c.a == ag.id else c.a
                other = self.agents.get(other_id)
                lines = "\n".join(f"{l['name']}: {l['text']}" for l in c.lines[-limit:])
                return other, lines, len(c.lines)
        return None, "", 0

    def adjust_trust(self, agent, other_id, delta):
        cur = agent.trust.get(other_id, 0.0)
        agent.trust[other_id] = round(max(-1.0, min(1.0, cur + delta)), 2)

    def townsfolk_tick(self):
        """The five residents are not the whole town. Ordinary townsfolk shop at Mira's,
        which is the only coin entering the economy — everything else is a transfer."""
        if time.time() < self.townsfolk_next:
            return
        self.townsfolk_next = time.time() + random.uniform(7, 13)
        mira = self.agents["mira"]
        in_stock = [it for it in self.stock if it["qty"] > 0]
        if not in_stock:
            mira.remember("The shelves are bare and customers left empty-handed.", 4)
            self.event("Townsfolk found the shelves bare and went away")
            return
        # cheaper goods sell more often, so Mira's markup is a real trade-off
        weights = [1.0 / max(1, self.prices.get(it["name"], it["price"])) for it in in_stock]
        item = random.choices(in_stock, weights=weights)[0]
        price = self.prices.get(item["name"], item["price"])
        item["qty"] -= 1
        mira.cash += price
        self.flows.append({"t": time.strftime("%H:%M:%S"), "from": "Townsfolk",
                           "to": mira.name, "amount": price, "why": f"bought {item['name']}"})
        if len(self.flows) > 40:
            self.flows = self.flows[-40:]
        if item["qty"] == 0:
            mira.remember(f"I have sold out of {item['name']}.", 3)
        self.event(f"A townsfolk bought {item['name']} for {price} coins, {item['qty']} left")

    # ---------- fast tick: movement only, never waits on the LLM ----------
    def step(self, dt):
        self.townsfolk_tick()
        for ag in self.agents.values():
            dx, dy = ag.tx - ag.x, ag.ty - ag.y
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > 0.08:
                speed = 1.8 * dt
                ag.x += dx / dist * min(speed, dist)
                ag.y += dy / dist * min(speed, dist)
            if ag.say and time.time() > ag.say_until:
                ag.say = ""

    # ---------- action validation: the LLM proposes, the world decides ----------
    def apply_action(self, ag, action, target, arg, say):
        """Returns a short result string recorded into memories."""
        # models sometimes echo the signature, e.g. 'talk_to(agent, intent)'
        action = (action or "rest").lower().strip().split("(")[0].strip()
        target = (target or "").strip().strip("()")

        if say:
            ag.speak(say)

        if action == "move_to":
            dest = target.lower()
            if dest not in PLACES:
                dest = nearest_place(ag.x, ag.y) if dest else ag.home
            ag.tx, ag.ty = place_center(dest)
            return f"walked to the {dest}"

        if action == "work":
            if ag.energy < 15:
                return "was too tired to work"
            ag.energy -= 15

            # a producer turns effort into goods, not coin — they earn by selling
            if ag.produces:
                ag.inventory[ag.produces] = ag.inventory.get(ag.produces, 0) + 6
                return f"worked the farm and now holds {ag.inventory[ag.produces]} {ag.produces}"

            # an employee is paid out of their employer's actual purse
            boss = self.agents.get(ag.employer)
            if boss:
                paid = self.pay(boss, ag, ag.wage, "wages")
                if paid:
                    return f"worked a shift and {boss.name} paid {paid} coins"
                # an employer who cannot make payroll is a relationship event
                self.adjust_trust(ag, boss.id, -0.15)
                ag.remember(f"{boss.name} could not pay my wages.", 4, source=boss.id)
                self.event(f"{boss.name} could not make payroll; {ag.name} went unpaid")
                return f"worked a shift but {boss.name} could not pay"

            # nobody employs them: odd jobs around town, barely a living
            ag.cash += 2
            return "scraped together 2 coins doing odd jobs"

        if action == "rest":
            ag.energy = min(100, ag.energy + 25)
            return "rested"

        other = self.agents.get(target.lower())

        if action == "talk_to":
            if not other:
                return "found nobody to talk to"
            line = (say or arg or "").strip()
            if not line:
                return f"approached {other.name} but said nothing"

            convo = self.active_convo(ag.id, other.id)
            if convo is None:
                until = self.convo_cooldown.get(frozenset((ag.id, other.id)), 0)
                if time.time() < until:
                    return f"had nothing further to say to {other.name} just yet"
                convo = self.open_convo(ag.id, other.id)

            # one speaker at a time: never talk over a reply you have not had yet
            if convo.lines and convo.lines[-1]["speaker"] == ag.id:
                other.next_tick = min(other.next_tick, time.time() + 1.0)
                return f"waited for {other.name} to answer"

            convo.lines.append({"speaker": ag.id, "name": ag.name, "text": line})
            ag.tx, ag.ty = other.x + 1, other.y
            other.remember(f"{ag.name} said: {line}", 2, source=ag.id)
            self.adjust_trust(other, ag.id, 0.05)

            if len(convo.lines) >= MAX_CONVO_LINES:
                self.close_convo(convo)
                return f"finished talking with {other.name}"
            # let them answer while it is still their turn to care
            other.next_tick = min(other.next_tick, time.time() + 2.5)
            return f"said to {other.name}: {line[:40]}"

        if action == "gossip":
            claim = (arg or say or "").strip()
            if not other:
                return "had nobody to gossip to"
            if len(claim) < 12:
                return "thought better of spreading a half-formed rumour"
            other.remember(f"{ag.name} told me: {claim}", 3, source=ag.id)
            # trust in the subject shifts by how much the listener trusts the teller
            subject = None
            for aid in self.agents:
                if aid in claim.lower() and aid != ag.id and aid != other.id:
                    subject = aid
            if subject:
                weight = max(0.0, other.trust.get(ag.id, 0.0))
                self.adjust_trust(other, subject, -0.25 * (0.4 + weight))
            ag.speak(claim)
            self.event(f"{ag.name} whispered to {other.name}: {claim}")
            return f"spread a rumour to {other.name}"

        if action == "buy":
            item = self.shop_item(target) or self.shop_item(arg)
            if not item:
                return f"could not find '{target}' in the shop"
            mira = self.agents["mira"]
            if ag.id == mira.id:
                return "already owns the shop's stock"
            price = self.prices.get(item["name"], item["price"])
            if item["qty"] <= 0:
                mira.remember(f"I sold out of {item['name']}. I need to restock.", 3)
                return f"found {item['name']} sold out"
            if not self.pay(ag, mira, price, f"bought {item['name']}"):
                return f"could not afford {item['name']} ({price} coins)"
            item["qty"] -= 1
            ag.inventory[item["name"]] = ag.inventory.get(item["name"], 0) + 1
            self.event(f"{ag.name} bought {item['name']} for {price} coins, {item['qty']} left")
            return f"bought {item['name']} for {price} coins"

        if action in ("pay", "give", "bribe"):
            # raw coin, no goods: bribes, hush money, gifts, settling a handshake deal
            if not other:
                return "had nobody to pay"
            if other.id == ag.id:
                return "cannot pay themselves"
            try:
                amount = max(1, int(float(str(arg).split(",")[0].strip() or 0)))
            except (TypeError, ValueError):
                return "did not name an amount to hand over"
            recent = self.last_paid.get((ag.id, other.id), 0)
            if time.time() - recent < 30:
                return f"had already handed {other.name} coin a moment ago"
            if not self.pay(ag, other, amount, "handed over coin"):
                return f"could not find {amount} coins to give {other.name}"
            self.last_paid[(ag.id, other.id)] = time.time()
            other.remember(f"{ag.name} paid me {amount} coins.", 3, source=ag.id)
            ag.remember(f"I paid {other.name} {amount} coins.", 3)
            self.adjust_trust(other, ag.id, 0.12)
            self.event(f"{ag.name} handed {other.name} {amount} coins")
            return f"paid {other.name} {amount} coins"

        if action in ("buy_from", "sell_to", "sell"):
            # arg is "item, qty, price-per-unit" — a deal for 50 apples must move 50 apples
            name, qty, unit = parse_offer(arg, target)
            if not other:
                return "named no one to trade with"
            if other.id == ag.id:
                return "cannot trade with themselves"
            seller, buyer = (other, ag) if action == "buy_from" else (ag, other)

            have = next((k for k in seller.inventory if k.lower() == (name or "").lower()), None)
            stockpile = seller.inventory.get(have, 0) if have else 0
            if not have or stockpile <= 0:
                who = "had no" if seller is ag else f"{seller.name} had no"
                return f"{who} {name or 'goods'} to sell"

            if unit is None:
                unit = max(1, round(self.prices.get(have, 5) * 0.6))
            unit = max(1, int(unit))

            # fill as much of the order as the seller holds and the buyer can pay for
            wanted = qty
            qty = min(qty, stockpile, buyer.cash // unit)
            if qty <= 0:
                buyer.remember(f"I could not afford {seller.name}'s {have} at {unit}c each.",
                               2, source=seller.id)
                return (f"{buyer.name} could not afford even one {have} at {unit} coins "
                        f"(they hold {buyer.cash}c)")

            total = qty * unit
            self.pay(buyer, seller, total, f"{qty}x {have} @ {unit}c")
            seller.inventory[have] = stockpile - qty

            shortfall = ""
            if qty < wanted:
                reason = "that was all they had" if stockpile < wanted else "that was all they could pay for"
                shortfall = f" — only {qty} of the {wanted} agreed, {reason}"
                seller.remember(f"I could only fill {qty} of {buyer.name}'s order for {wanted} {have}.", 2)

            # selling to the shopkeeper puts the goods on the shelves at that wholesale cost
            if buyer.id == "mira":
                item = self.stock_of(have)
                if item:
                    item["qty"] += qty
                    item["cost"] = unit
                else:
                    retail = max(unit + 1, round(unit * 1.5))
                    self.stock.append({"name": have, "real_title": f"Local {have}", "url": "",
                                       "image": "", "price": retail, "cost": unit, "qty": qty})
                    self.prices[have] = retail
                    self.base_prices[have] = retail
                self.event(f"{seller.name} supplied the shop with {qty} {have} "
                           f"at {unit} coins each, {total} in all{shortfall}")
            else:
                buyer.inventory[have] = buyer.inventory.get(have, 0) + qty
                self.event(f"{seller.name} sold {buyer.name} {qty} {have} "
                           f"at {unit} coins each, {total} in all{shortfall}")
            self.adjust_trust(buyer, seller.id, 0.08)
            self.adjust_trust(seller, buyer.id, 0.08)
            verb = "bought" if action == "buy_from" else "sold"
            counterpart = seller.name if action == "buy_from" else buyer.name
            return (f"{verb} {qty}x {have} {'from' if verb == 'bought' else 'to'} {counterpart} "
                    f"at {unit} coins each, {total} coins in total{shortfall}")

        if action == "set_price":
            if ag.id != "mira":
                return "does not set the shop's prices"
            parts = [x.strip() for x in (arg or "").split(",")]
            name = parts[0] if parts and parts[0] else target
            item = self.shop_item(name)
            if not item:
                return f"could not find '{name}' on the shelves"
            try:
                want = int(float(parts[1]))
            except (IndexError, ValueError):
                return "did not name a price"
            cost = item["cost"]
            # the rule from the plan: never below cost, never above 3x cost
            price = max(cost, min(cost * 3, want))
            old_price = self.prices.get(item["name"], item["price"])
            self.prices[item["name"]] = price
            self.base_prices[item["name"]] = price
            verb = "marked up" if price > old_price else "cut"
            self.event(f"Mira {verb} {item['name']} from {old_price} to {price} coins, having paid {cost}")
            if want != price:
                return f"{verb} {item['name']} to {price} coins — {want} was outside the allowed range"
            return f"{verb} {item['name']} from {old_price} to {price} coins"

        if action == "hire":
            if not other:
                return "had nobody to hire"
            try:
                wage = max(1, min(20, int(float(arg))))
            except (TypeError, ValueError):
                wage = 6
            if ag.cash < wage:
                return f"could not afford to take {other.name} on"
            if other.employer == ag.id and other.wage == wage:
                return f"already employs {other.name} at {wage} coins a shift"
            if other.id == ag.id:
                return "cannot hire themselves"
            poached = self.agents.get(other.employer) if other.employer else None
            other.employer, other.wage = ag.id, wage
            other.remember(f"{ag.name} hired me at {wage} coins a shift.", 3, source=ag.id)
            self.adjust_trust(other, ag.id, 0.15)
            if poached and poached.id != ag.id:
                poached.remember(f"{ag.name} poached {other.name} off me.", 4, source=ag.id)
                self.adjust_trust(poached, ag.id, -0.3)
                self.event(f"{ag.name} poached {other.name} away from {poached.name} for {wage} coins a shift")
                return f"poached {other.name} away from {poached.name} at {wage} coins a shift"
            self.event(f"{ag.name} hired {other.name} for {wage} coins a shift")
            return f"hired {other.name} at {wage} coins a shift"

        if action == "lend":
            if ag.id != "bram":
                return "is not the bank and cannot lend"
            if not other:
                return "had nobody to lend to"
            try:
                amount = max(1, min(80, int(float(arg))))
            except (TypeError, ValueError):
                amount = 20
            outstanding = other.debts.get("bram", 0)
            if outstanding + amount > 120:
                return f"refused — {other.name} is already {outstanding}c deep"
            # keep a working float so the player can always still get a loan
            if self.bank_reserves - amount < 80:
                return "the bank did not have the reserves"
            owed = round(amount * (1 + self.interest_rate))
            self.bank_reserves -= amount
            other.cash += amount
            other.debts["bram"] = other.debts.get("bram", 0) + owed
            other.remember(f"Bram lent me {amount} coins; I owe {owed} back.", 3, source="bram")
            ag.remember(f"I lent {other.name} {amount} coins at {int(self.interest_rate*100)}%.", 3)
            self.flows.append({"t": time.strftime("%H:%M:%S"), "from": "Bank",
                               "to": other.name, "amount": amount, "why": "loan"})
            self.event(f"Bram lent {other.name} {amount} coins, {owed} due back")
            return f"lent {other.name} {amount} coins, {owed} due back at {int(self.interest_rate*100)}% interest"

        if action == "repay":
            creditor = target.lower() or "bram"
            owed = ag.debts.get(creditor, 0)
            if owed <= 0:
                return "owed nothing"
            pay = min(ag.cash, owed)
            if pay <= 0:
                return "could not afford to repay anything"
            ag.cash -= pay
            ag.debts[creditor] = owed - pay
            if creditor == "bram":
                self.bank_reserves += pay
            elif creditor in self.agents:
                self.agents[creditor].cash += pay
            if creditor in self.agents:
                self.adjust_trust(self.agents[creditor], ag.id, 0.25)
            self.flows.append({"t": time.strftime("%H:%M:%S"), "from": ag.name,
                               "to": creditor.title(), "amount": pay, "why": "repayment"})
            left = ag.debts[creditor]
            self.event(f"{ag.name} repaid {pay}c to {creditor.title()}"
                       + (f" ({left}c still owing)" if left else " — debt cleared"))
            return f"repaid {pay} coins to {creditor.title()}, {left} still owing"

        if action == "restock":
            if ag.id != "mira":
                return "does not run the shop"
            if time.time() - ag.last_restock < 90:
                return "had already restocked recently"
            if len(self.stock) >= 14 and not self.stock_of((arg or target or "").strip()):
                return "had no shelf space for anything new"
            query = (arg or target or "wool scarf").strip()
            if len(query) < 3:
                return "could not think what to restock"
            ag.last_restock = time.time()
            try:
                found = catalog.search(query, limit=1)
            except Exception as e:
                return f"the supplier catalogue was unreachable ({e})"
            if not found:
                return f"found nothing for '{query}'"
            item = found[0]
            unit_cost = max(1, round(item["price"] * 0.6))
            units = 3
            bill = unit_cost * units
            if ag.cash < bill:
                return (f"found {item['name']} at {unit_cost}c wholesale but needed {bill}c "
                        f"for a case and only had {ag.cash}c")
            ag.cash -= bill
            self.flows.append({"t": time.strftime("%H:%M:%S"), "from": ag.name,
                               "to": "Supplier", "amount": bill, "why": f"wholesale {item['name']}"})
            existing = self.stock_of(item["name"])
            if existing:
                existing["qty"] += units
                existing["cost"] = unit_cost
            else:
                item["cost"] = unit_cost
                item["qty"] = units
                self.stock.append(item)
                self.prices[item["name"]] = item["price"]
                self.base_prices[item["name"]] = item["price"]
            self.event(f"Mira took delivery of {units} {item['name']} at {unit_cost} coins "
                       f"each, {bill} in all: {item['real_title'][:34]}")
            return (f"bought {units} {item['name']} from the supplier at {unit_cost}c each "
                    f"({bill}c total), shelved at {self.prices[item['name']]}c")

        # anything the model invented that the world does not implement
        ag.energy = min(100, ag.energy + 5)
        return f"paused, thinking about how to {action.replace('_', ' ')}"

    # ---------- fallback when the LLM is unavailable ----------
    def fallback_decision(self, ag):
        choices = [("work", "", ""), ("rest", "", ""),
                   ("move_to", random.choice(list(PLACES)), "")]
        near = self.nearby(ag)
        if near:
            choices.append(("talk_to", random.choice(near).id, "passing the time"))
        action, target, arg = random.choice(choices)
        return {"thought": "(no model — acting on instinct)", "action": action,
                "target": target, "arg": arg, "say": ""}

    # ---------- serialisation for the client ----------
    def snapshot(self):
        return {
            "type": "state",
            "day": self.day,
            "tile": TILE,
            "map": {"w": MAP_W, "h": MAP_H, "places": PLACES},
            "agents": [{
                "id": a.id, "name": a.name, "role": a.role, "color": a.color,
                "x": round(a.x, 2), "y": round(a.y, 2), "cash": a.cash,
                "energy": a.energy, "thought": a.thought, "say": a.say,
                "action": a.last_action, "goal": a.goal,
                "inventory": a.inventory, "trust": a.trust,
                "debt": sum(a.debts.values()),
                "employer": a.employer, "wage": a.wage, "produces": a.produces,
            } for a in self.agents.values()],
            "player": self.player,
            "shop": [{**it, "price": self.prices.get(it["name"], it["price"])} for it in self.stock],
            "bank_reserves": self.bank_reserves,
            "interest_rate": self.interest_rate,
            "flows": self.flows[-14:],
            "price_mult": round(self.price_mult, 2),
            "conversations": [{
                "id": c.id, "a": c.a, "b": c.b, "closed": c.closed,
                "names": [self.agents[c.a].name, self.agents[c.b].name],
                "colors": [self.agents[c.a].color, self.agents[c.b].color],
                "lines": c.lines,
            } for c in self.conversations[-8:]],
            "log": self.log[-40:],
        }
