"""Untitled Town: world state, agents, economy. The server is the single source of truth."""
import math, os, random, time, json
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
    "stall_a": (12, 15, 4, 3, "#8a6b3f"),
    "stall_b": (18, 3, 4, 3, "#8a6b3f"),
}
PLOTS = ("stall_a", "stall_b")      # market plots a worker can set up on
STALL_COST = 55                     # what it takes to open for business

DAY_SECONDS = float(os.getenv("DAY_SECONDS", "240"))   # one full day and night
NIGHT_FROM = 0.72                   # the point in the day when the light goes
ELECTION_EVERY = 5                  # the town votes on every fifth day

# A promise is only worth something if winning actually changes a rule.
POLICIES = {
    "cheap_bread": {
        "pitch": "cap the price of food so nobody goes hungry",
        "law": "Food is capped at 4 coins.",
        "helps": "buyers",
    },
    "free_market": {
        "pitch": "let trade run free and make it cheap to open a stall",
        "law": f"Market plots cost {STALL_COST // 2} coins and shopkeepers price as they please.",
        "helps": "owners",
    },
    "workers_stipend": {
        "pitch": "pay every working man and woman a daily stipend from the town purse",
        "law": "Anyone without a shop draws 5 coins a day from the treasury.",
        "helps": "workers",
    },
    "cheap_credit": {
        "pitch": "force the bank to lend at half the interest",
        "law": "The bank may charge only 10% interest.",
        "helps": "debtors",
    },
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


MAX_CONVO_LINES = 8      # a conversation ends after this many lines
CONVO_COOLDOWN = 50.0    # seconds before the same pair may start talking again


@dataclass
class Shop:
    owner: str
    name: str
    place: str
    goods: list = field(default_factory=list)   # each: name, cost, price, base, qty, real_title, url, image

    def good(self, name):
        name = (name or "").strip().lower()
        for g in self.goods:
            if g["name"].lower() == name:
                return g
        for g in self.goods:
            if name and name in g["name"].lower():
                return g
        return None


@dataclass
class Conversation:
    id: int
    a: str
    b: str
    lines: list = field(default_factory=list)   # {"speaker", "name", "text"}
    started: float = 0.0
    last_t: float = 0.0
    closed: bool = True        # closed means "not mid-exchange", never "gone"
    session_from: int = 0      # where the current exchange began in lines

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
    politician: bool = False
    promise: str = ""       # the policy they are running on

    def remember(self, text, importance=1, source="self"):
        self.memories.append(Memory(text, time.time(), importance, source))
        if len(self.memories) > 60:
            self.memories = self.memories[-60:]

    def top_memories(self, n=7):
        """Important first, recent second. Decay is logarithmic, so something that
        mattered an hour ago still outranks a trivial thing from a minute ago —
        a linear decay made agents forget their own deals within minutes."""
        now = time.time()
        scored = sorted(
            self.memories,
            key=lambda m: m.importance * 5 - math.log1p(max(0, now - m.t) / 60) * 3,
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
        Agent("orla", "Orla", "Alderman",
              "The sitting alderman. Speaks in polished, careful sentences and never quite says no.",
              "Win the election and keep the merchants on side.",
              "Takes quiet gifts from shopkeepers and calls them campaign support.",
              "#d0857f", "square", cash=90, politician=True),
        Agent("devi", "Devi", "Agitator",
              "A firebrand who speaks for the working people. Blunt, warm, quick to anger.",
              "Win the election and shift the town's coin toward those who work for it.",
              "Would take the job for the power as much as the people.",
              "#7fc4c0", "tavern", cash=22, politician=True),
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
    byid["orla"].remember("Mira's coin keeps my campaign alive. She will expect a return.", 4)
    byid["devi"].remember("Wren works herself raw for six coins a shift. That is the whole argument.", 4)
    byid["orla"].trust["devi"] = -0.35
    byid["devi"].trust["orla"] = -0.4
    byid["mira"].trust["orla"] = 0.4
    return {x.id: x for x in a}


ACTIONS = """move_to(place) | talk_to(agent, intent) | work | rest | gossip(agent, about, claim) |
promise(policy)                — candidates only: pledge cheap_bread, free_market,
                                 workers_stipend or cheap_credit
buy(item)                      — buy one unit from whichever shop sells it cheapest
sell_to(agent, item, qty, price)  — sell goods you own (arg = "item, quantity, price EACH")
buy_from(agent, item, qty, price) — buy goods off another agent (arg = "item, quantity, price EACH")
pay(agent, amount)             — hand over coin: a bribe, hush money, a gift, a deal you struck
set_price(item, price)         — your own shop only; between cost and 3x cost (arg = "item, price")
restock(query)                 — shop owners; buy new stock from the outside supplier, costs coin
open_stall(name)               — if you have no shop and 55 coins, take a market plot and trade
                                 for yourself instead of for wages
hire(agent, wage)              — offer someone a job you pay for (arg = wage)
lend(agent, amount)            — front someone money. Bram lends the bank's coin at interest;
                                 anyone else lends their own, and it is owed back to them
repay(agent, amount)           — pay down what you owe that person"""


class World:
    def __init__(self):
        self.agents = make_agents()
        goods = catalog.load_stock()
        for name, price in (("Bread", 4), ("Apples", 3)):
            goods.append({"name": name, "real_title": f"Local {name}",
                          "price": price, "url": "", "image": ""})
        for g in goods:
            g["cost"] = max(1, round(g["price"] * 0.6))
            g["base"] = g["price"]
            g["qty"] = 6
        self.shops = [Shop("mira", "Mira's Shop", "shop", goods)]
        self.price_mult = 1.0
        self.log = []
        self.conversations = []
        self.convo_seq = 0
        self.convo_cooldown = {}               # frozenset(pair) -> time it may restart
        self.bank_reserves = 300
        self.interest_rate = 0.2      # what Bram charges on a loan
        self.townsfolk_next = time.time() + 10
        self.flows = []               # recent coin movements, for the Economy panel
        self.last_paid = {}           # (payer, payee) -> time, so handouts cannot loop
        self.offers = []              # supplier goods already fetched, ready to buy
        self.proposals = []           # deals put TO the player, awaiting their yes or no
        self.proposal_seq = 0
        self.last_pitch = {}          # agent id -> when they last pitched the player
        self.wanted = []              # what Mira has asked the supplier for
        self.day = 1
        self.day_started = time.time()
        self.phase = "day"
        self.treasury = 60
        self.mayor = ""
        self.policy = ""
        self.election_today = False
        self.ballot_open = False
        self.votes = {}
        self.started = time.time()
        # The player is an Agent like anyone else — just one the model never drives.
        # Without this, agents had no one to sell to when you asked them to.
        self.you = Agent("stranger", "Stranger", "Newcomer",
                         "A newcomer to town.", "See what this town is made of.", "",
                         "#efe7d6", "square", cash=100, x=17.0, y=11.0, tx=17.0, ty=11.0)
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

    def propose(self, ag, kind, text, action, arg):
        """Agents may offer the player a deal; only the player may accept it.
        Nothing leaves your purse without you saying so."""
        for old in self.proposals:
            if old["from"] == ag.id and old["action"] == action and old["arg"] == arg:
                return "had already put that to the stranger"
        # one pitch at a time per person, so the stranger is not buried in cards
        if any(o["from"] == ag.id for o in self.proposals):
            return "already has an offer waiting with the stranger"
        if time.time() - self.last_pitch.get(ag.id, 0) < 25:
            return "had only just pitched the stranger; gave them room to think"
        self.last_pitch[ag.id] = time.time()
        self.proposal_seq += 1
        self.proposals.append({"id": self.proposal_seq, "from": ag.id, "name": ag.name,
                               "color": ag.color, "kind": kind, "text": text,
                               "action": action, "arg": arg, "t": time.time()})
        self.proposals = self.proposals[-3:]
        self.event(f"{ag.name} put an offer to you: {text}")
        return f"offered the stranger: {text}"

    def resolve_proposal(self, pid, accept):
        p = next((x for x in self.proposals if x["id"] == pid), None)
        if not p:
            return "That offer is no longer on the table."
        self.proposals.remove(p)
        ag = self.agents.get(p["from"])
        if not ag:
            return "They are no longer here."
        if not accept:
            ag.remember(f"The stranger turned down my offer: {p['text']}", 3, source="stranger")
            self.adjust_trust(ag, "stranger", -0.1)
            self.event(f"You turned down {ag.name}: {p['text']}")
            return f"You turned down {ag.name}."
        result = self.apply_action(ag, p["action"], "stranger", p["arg"], "", consented=True)
        ag.remember(f"The stranger accepted: {p['text']}", 3, source="stranger")
        self.adjust_trust(ag, "stranger", 0.1)
        self.event(f"You accepted {ag.name}'s offer: {result}")
        return result

    def expire_proposals(self):
        now = time.time()
        for p in list(self.proposals):
            if now - p["t"] > 40:
                self.proposals.remove(p)

    def party(self, ident):
        """Resolve an action's target to a person — an agent, or the player."""
        ident = (ident or "").strip().lower()
        if ident in ("stranger", "player", "you", "the stranger", "newcomer"):
            return self.you
        return self.agents.get(ident)

    def everyone(self):
        return list(self.agents.values()) + [self.you]

    def shop_of(self, owner_id):
        return next((sh for sh in self.shops if sh.owner == owner_id), None)

    def shop_at(self, place):
        return next((sh for sh in self.shops if sh.place == place), None)

    def free_plot(self):
        taken = {sh.place for sh in self.shops}
        return next((p for p in PLOTS if p not in taken), None)

    def all_goods(self):
        """Every good on sale anywhere, as (shop, good) pairs."""
        return [(sh, g) for sh in self.shops for g in sh.goods]

    def cheapest(self, name, in_stock=True):
        """Where a buyer would actually go for this. Competition lives here."""
        hits = [(sh, g) for sh, g in self.all_goods()
                if g["name"].lower() == (name or "").lower() and (g["qty"] > 0 or not in_stock)]
        if not hits:
            hits = [(sh, g) for sh, g in self.all_goods()
                    if name and name.lower() in g["name"].lower() and (g["qty"] > 0 or not in_stock)]
        return min(hits, key=lambda p: p[1]["price"]) if hits else (None, None)

    def stock_of(self, name):
        return self.cheapest(name, in_stock=False)[1]

    def event(self, text):
        self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
        if len(self.log) > 120:
            self.log = self.log[-120:]

    def shop_item(self, name):
        return self.cheapest(name, in_stock=False)[1]

    def nearby(self, agent, radius=4.0):
        return [o for o in self.everyone()
                if o.id != agent.id and (o.x - agent.x) ** 2 + (o.y - agent.y) ** 2 < radius ** 2]

    def active_convo(self, a_id, b_id):
        c = self.thread(a_id, b_id, create=False)
        return c if (c and not c.closed) else None

    def thread(self, a_id, b_id, create=True):
        """The one lasting conversation between these two. It is never thrown away —
        exchanges open and close inside it, but the history stays."""
        pair = frozenset((a_id, b_id))
        for c in self.conversations:
            if c.pair == pair:
                return c
        if not create:
            return None
        self.convo_seq += 1
        c = Conversation(self.convo_seq, a_id, b_id, started=time.time(), last_t=time.time())
        self.conversations.append(c)
        return c

    def say_into(self, speaker, listener, text):
        """Append a line to the lasting thread between two people."""
        c = self.thread(speaker.id, listener.id)
        c.lines.append({"speaker": speaker.id, "name": speaker.name, "text": text})
        if len(c.lines) > 80:
            c.lines = c.lines[-80:]
            c.session_from = max(0, c.session_from - 1)
        c.last_t = time.time()
        return c

    def open_convo(self, a_id, b_id):
        c = self.thread(a_id, b_id)
        c.closed = False
        c.session_from = len(c.lines)
        return c

    def close_convo(self, c):
        """End the current exchange. The thread and its history remain."""
        c.closed = True
        self.convo_cooldown[c.pair] = time.time() + CONVO_COOLDOWN
        a, b = self.party(c.a), self.party(c.b)
        if not (a and b):
            return
        session = c.lines[c.session_from:]
        for me, them in ((a, b), (b, a)):
            theirs = [l["text"] for l in session if l["speaker"] == them.id]
            mine = [l["text"] for l in session if l["speaker"] == me.id]
            note = f"I talked with {them.name}. They said: {theirs[-1] if theirs else 'little'}"
            if mine:
                note += f" I said: {mine[-1]}"
            me.remember(note + " If we agreed anything, act on it now.", 4, source=them.id)
            self.adjust_trust(me, them.id, 0.04)
            me.next_tick = min(me.next_tick, time.time() + 2.0)

    def convo_transcript(self, ag, limit=8):
        """The open conversation this agent is in, rendered for their prompt."""
        mine = [c for c in self.conversations if not c.closed and ag.id in c.pair]
        for c in sorted(mine, key=lambda c: c.last_t, reverse=True):
            if True:
                other_id = c.b if c.a == ag.id else c.a
                other = self.party(other_id)
                recent = c.lines[max(c.session_from, len(c.lines) - limit):]
                lines = "\n".join(f"{l['name']}: {l['text']}" for l in recent)
                return other, lines, len(c.lines) - c.session_from
        return None, "", 0

    def adjust_trust(self, agent, other_id, delta):
        cur = agent.trust.get(other_id, 0.0)
        agent.trust[other_id] = round(max(-1.0, min(1.0, cur + delta)), 2)

    def election_in(self):
        return ELECTION_EVERY - (self.day % ELECTION_EVERY)

    def candidates(self):
        return [a for a in self.agents.values() if a.politician]

    def price_ceiling(self, good_name):
        """cheap_bread is a real law: it holds food down whatever a shopkeeper wants."""
        if self.policy == "cheap_bread" and good_name in ("Bread", "Apples"):
            return 4
        return None

    def stall_cost(self):
        return STALL_COST // 2 if self.policy == "free_market" else STALL_COST

    def rate(self):
        return 0.1 if self.policy == "cheap_credit" else self.interest_rate

    def enforce_policy(self):
        cap_applied = False
        for _, g in self.all_goods():
            cap = self.price_ceiling(g["name"])
            if cap is not None and g["price"] > cap:
                g["price"] = cap
                cap_applied = True
        return cap_applied

    def clock(self):
        """How far through the day we are, 0 at dawn and 1 at the end of night."""
        return ((time.time() - self.day_started) % DAY_SECONDS) / DAY_SECONDS

    def is_night(self):
        return self.clock() >= NIGHT_FROM

    def time_tick(self):
        """Roll the day over, and put the town to bed when the light goes."""
        was_night = self.phase == "night"
        now_night = self.is_night()

        if time.time() - self.day_started >= DAY_SECONDS:
            self.day_started += DAY_SECONDS
            self.day += 1
            self.phase = "day"
            for ag in self.agents.values():
                ag.energy = min(100, ag.energy + 55)      # a night's sleep
                ag.remember(f"Day {self.day} began.", 2)
                ag.next_tick = min(ag.next_tick, time.time() + random.uniform(0, 4))
            self.event(f"Day {self.day} — the town wakes.")
            self.on_new_day()
            return

        if now_night and not was_night:
            self.phase = "night"
            for ag in self.agents.values():
                # the drifter keeps his own hours; everyone else goes home
                if ag.id == "kit":
                    ag.remember("Night. The town sleeps and I am wide awake.", 3)
                    continue
                ag.tx, ag.ty = place_center(ag.home)
                ag.remember("Night fell. Time to head home and sleep.", 2)
            self.event("Night falls; the town heads home.")
        elif not now_night and was_night:
            self.phase = "day"

    def on_new_day(self):
        if self.ballot_open:
            self.count_votes()
        self.election_today = False
        self.pay_stipend()
        self.enforce_policy()
        if self.day % ELECTION_EVERY == 0:
            self.open_ballot()
        else:
            days = ELECTION_EVERY - (self.day % ELECTION_EVERY)
            if days <= 2:
                for ag in self.agents.values():
                    ag.remember(f"The election is {days} day(s) away.", 3)

    def open_ballot(self):
        self.election_today = True
        self.ballot_open = True
        self.votes = {}
        for c in self.candidates():
            if not c.promise:
                c.promise = random.choice(list(POLICIES))
        pitch = "; ".join(f"{c.name} would {POLICIES[c.promise]['pitch']}" for c in self.candidates())
        for ag in self.agents.values():
            ag.remember(f"Election day. {pitch}. I must decide who to back.", 5)
            ag.next_tick = min(ag.next_tick, time.time() + random.uniform(0, 5))
        self.event(f"ELECTION DAY — {pitch}")

    def stands_to_gain(self, voter, policy):
        """How much this voter's own circumstances favour a policy. Self-interest,
        not ideology — which is what makes the promises worth making."""
        owns = self.shop_of(voter.id) is not None
        owes = sum(voter.debts.values())
        if policy == "cheap_bread":
            return 0.0 if owns else 0.5
        if policy == "free_market":
            return 0.7 if owns else (0.3 if voter.cash >= self.stall_cost() else -0.2)
        if policy == "workers_stipend":
            return -0.3 if owns else 0.7
        if policy == "cheap_credit":
            return 0.6 if owes > 0 else 0.05
        return 0.0

    def count_votes(self):
        cands = self.candidates()
        if not cands:
            return
        tally = {c.id: 0 for c in cands}
        detail = []
        for ag in self.agents.values():
            if ag.politician:
                continue
            pick = max(cands, key=lambda c: ag.trust.get(c.id, 0) * 0.6
                       + self.stands_to_gain(ag, c.promise))
            tally[pick.id] += 1
            detail.append(f"{ag.name} backed {pick.name}")
            ag.remember(f"I voted for {pick.name}, who promised to "
                        f"{POLICIES[pick.promise]['pitch']}.", 4)
        for cid, n in self.votes.items():                       # the player's ballot
            if cid in tally:
                tally[cid] += n
                detail.append(f"You backed {self.agents[cid].name}")

        top = max(tally.values())
        winners = [c for c in cands if tally[c.id] == top]
        winner = random.choice(winners)
        self.mayor = winner.id
        self.policy = winner.promise
        self.ballot_open = False
        law = POLICIES[self.policy]["law"]
        capped = self.enforce_policy()

        score = ", ".join(f"{c.name} {tally[c.id]}" for c in cands)
        self.event(f"ELECTION RESULT — {winner.name} won ({score}). New law: {law}")
        for ag in self.agents.values():
            ag.remember(f"{winner.name} won the election. The law now says: {law}", 5)
            ag.next_tick = min(ag.next_tick, time.time() + random.uniform(0, 6))
        winner.remember(f"I won. I promised to {POLICIES[self.policy]['pitch']} "
                        f"and now I must live with it.", 5)
        for c in cands:
            if c.id != winner.id:
                c.remember(f"I lost to {winner.name}. Next time.", 5)
                c.promise = ""
        if capped:
            self.event("Shopkeepers had to drop their prices to meet the new law.")

    def pay_stipend(self):
        if self.policy != "workers_stipend":
            return
        for ag in self.agents.values():
            if self.shop_of(ag.id) or ag.politician:
                continue
            if self.treasury < 5:
                self.event("The treasury is empty; no stipend was paid today.")
                return
            self.treasury -= 5
            ag.cash += 5
            ag.remember("I drew my 5 coin stipend from the town purse.", 3)
        self.event("The workers' stipend was paid out of the treasury.")

    def asleep(self, ag):
        """Abed for the night — unless you are the drifter, or somebody is standing
        over you, in which case you can be woken."""
        if self.phase != "night" or ag.id == "kit":
            return False
        near_player = (self.you.x - ag.x) ** 2 + (self.you.y - ag.y) ** 2 < 9
        return not near_player

    def townsfolk_tick(self):
        """The five residents are not the whole town. Ordinary townsfolk shop at Mira's,
        which is the only coin entering the economy — everything else is a transfer."""
        if time.time() < self.townsfolk_next:
            return
        if self.phase == "night":
            self.townsfolk_next = time.time() + 8
            return
        self.townsfolk_next = time.time() + random.uniform(14, 22)
        on_sale = [(sh, g) for sh, g in self.all_goods() if g["qty"] > 2]
        if not on_sale:
            for sh in self.shops:
                owner = self.agents.get(sh.owner)
                if owner:
                    owner.remember("The shelves are bare and customers left empty-handed.", 4)
            self.event("Townsfolk found the shelves bare and went away")
            return
        # cheaper wins more custom — this is what makes a price war bite
        weights = [(1.0 / max(1, g["price"])) ** 1.6 for _, g in on_sale]
        shop, good = random.choices(on_sale, weights=weights)[0]
        owner = self.agents.get(shop.owner)
        price = good["price"]
        good["qty"] -= 1
        if owner:
            owner.cash += price
            if good["qty"] == 0:
                owner.remember(f"I have sold out of {good['name']}.", 3)
        levy = max(0, round(price * 0.1))
        if levy and owner:
            owner.cash -= levy
            self.treasury += levy
        self.flows.append({"t": time.strftime("%H:%M:%S"), "from": "Townsfolk",
                           "to": owner.name if owner else shop.name,
                           "amount": price, "why": f"bought {good['name']}"})
        if len(self.flows) > 40:
            self.flows = self.flows[-40:]
        self.event(f"A townsfolk bought {good['name']} at {shop.name} for {price} coins, "
                   f"{good['qty']} left")

    # ---------- fast tick: movement only, never waits on the LLM ----------
    def step(self, dt):
        self.time_tick()
        self.townsfolk_tick()
        self.expire_proposals()
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
    def apply_action(self, ag, action, target, arg, say, consented=False):
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
            boss = self.party(ag.employer)
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

        other = self.party(target)

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

            self.say_into(ag, other, line)
            ag.tx, ag.ty = other.x + 1, other.y
            other.remember(f"{ag.name} said: {line}", 2, source=ag.id)
            self.adjust_trust(other, ag.id, 0.05)

            if len(convo.lines) - convo.session_from >= MAX_CONVO_LINES:
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
            shop, good = self.cheapest(target or arg)
            if good is None:
                shop2, sold_out = self.cheapest(target or arg, in_stock=False)
                if sold_out is not None:
                    return f"found {sold_out['name']} sold out everywhere"
                return f"could not find '{target or arg}' for sale anywhere"
            owner = self.agents.get(shop.owner)
            if owner is None or owner.id == ag.id:
                return "already owns that stock"
            price = good["price"]
            if not self.pay(ag, owner, price, f"bought {good['name']}"):
                return f"could not afford {good['name']} ({price} coins)"
            good["qty"] -= 1
            ag.inventory[good["name"]] = ag.inventory.get(good["name"], 0) + 1
            self.event(f"{ag.name} bought {good['name']} from {shop.name} for {price} coins, "
                       f"{good['qty']} left")
            return f"bought {good['name']} from {shop.name} for {price} coins"

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

            # the shopkeeper sells off the shelves, not out of her pockets
            sellers_shop = self.shop_of(seller.id)
            shelf = sellers_shop.good(name) if sellers_shop else None
            if shelf is not None:
                have, stockpile = shelf["name"], shelf["qty"]
                if stockpile <= 0:
                    seller.remember(f"I am out of {have} and turned away a sale.", 3)
                    return f"{seller.name} had no {have} left on the shelf"
            else:
                have = next((k for k in seller.inventory if k.lower() == (name or "").lower()), None)
                stockpile = seller.inventory.get(have, 0) if have else 0
                if not have or stockpile <= 0:
                    who = "had no" if seller is ag else f"{seller.name} had no"
                    return f"{who} {name or 'goods'} to sell"

            if unit is None:
                ref = self.stock_of(have)
                base_price = ref["price"] if ref else 5
                unit = base_price if shelf is not None else round(base_price * 0.6)
            unit = max(1, int(unit))
            # she will haggle, but never below cost and never above 3x it —
            # the same ceiling set_price obeys, so a greedy mood cannot become a fleecing
            if shelf is not None:
                unit = max(shelf["cost"], min(shelf["cost"] * 3, unit))

            # fill as much of the order as the seller holds and the buyer can pay for
            wanted = qty
            qty = min(qty, stockpile, buyer.cash // unit)
            if qty <= 0:
                buyer.remember(f"I could not afford {seller.name}'s {have} at {unit}c each.",
                               2, source=seller.id)
                return (f"{buyer.name} could not afford even one {have} at {unit} coins "
                        f"(they hold {buyer.cash}c)")

            total = qty * unit
            if not consented and self.you in (buyer, seller):
                if buyer is self.you:
                    text = f"{qty} {have} for {unit} coins each — {total} in all"
                else:
                    text = f"to buy {qty} {have} off you at {unit} coins each — {total} in all"
                return self.propose(ag, "trade", text, action,
                                    f"{have}, {qty}, {unit}")

            self.pay(buyer, seller, total, f"{qty}x {have} @ {unit}c")
            if shelf is not None:
                shelf["qty"] = stockpile - qty
            else:
                seller.inventory[have] = stockpile - qty

            shortfall = ""
            if qty < wanted:
                reason = "that was all they had" if stockpile < wanted else "that was all they could pay for"
                shortfall = f" — only {qty} of the {wanted} agreed, {reason}"
                seller.remember(f"I could only fill {qty} of {buyer.name}'s order for {wanted} {have}.", 2)

            # selling to the shopkeeper puts the goods on the shelves at that wholesale cost
            buyers_shop = self.shop_of(buyer.id)
            if buyers_shop is not None:
                item = buyers_shop.good(have)
                if item:
                    item["qty"] += qty
                    item["cost"] = unit
                else:
                    retail = max(unit + 1, round(unit * 1.5))
                    buyers_shop.goods.append({"name": have, "real_title": f"Local {have}", "url": "",
                                              "image": "", "price": retail, "base": retail,
                                              "cost": unit, "qty": qty})
                self.event(f"{seller.name} supplied {buyers_shop.name} with {qty} {have} "
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
            shop = self.shop_of(ag.id)
            if shop is None:
                return "keeps no shop, so sets no prices"
            parts = [x.strip() for x in (arg or "").split(",")]
            name = parts[0] if parts and parts[0] else target
            item = shop.good(name)
            if not item:
                return f"has no '{name}' on the shelves"
            try:
                want = int(float(parts[1]))
            except (IndexError, ValueError):
                return "did not name a price"
            cost = item["cost"]
            price = max(cost, min(cost * 3, want))          # never under cost, never over 3x
            cap = self.price_ceiling(item["name"])
            if cap is not None:
                price = min(price, cap)
            old_price = item["price"]
            if price == old_price:
                return f"left {item['name']} at {price} coins"
            item["price"] = price
            item["base"] = price
            verb = "marked up" if price > old_price else "cut"
            # the rival is the best price among OTHER shops, not counting your own
            others = [(sh, g) for sh, g in self.all_goods()
                      if sh.owner != ag.id and g["name"].lower() == item["name"].lower()]
            rival, rg = min(others, key=lambda pr: pr[1]["price"]) if others else (None, None)
            under = ""
            if rival and rg["price"] > price:
                under = f", undercutting {rival.name}"
                rival_owner = self.agents.get(rival.owner)
                if rival_owner:
                    rival_owner.remember(f"{ag.name} is selling {item['name']} at {price}, "
                                         f"under my {rg['price']}.", 4, source=ag.id)
                    self.adjust_trust(rival_owner, ag.id, -0.15)
            self.event(f"{ag.name} {verb} {item['name']} from {old_price} to {price} coins, "
                       f"having paid {cost}{under}")
            return f"{verb} {item['name']} from {old_price} to {price} coins{under}"

        if action == "hire":
            if not other:
                return "had nobody to hire"
            try:
                wage = max(1, min(20, int(float(arg))))
            except (TypeError, ValueError):
                wage = 6
            if ag.cash < wage:
                return f"could not afford to take {other.name} on"
            if other is self.you:
                return "the stranger works for nobody"
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
            if not other:
                return "had nobody to lend to"
            if other.id == ag.id:
                return "cannot lend to themselves"
            try:
                amount = max(1, min(80, int(float(str(arg).split(",")[0].strip()))))
            except (TypeError, ValueError):
                amount = 20
            owed_already = other.debts.get(ag.id, 0)
            if not consented and other is self.you:
                rate = self.rate() if ag.id == "bram" else 0.0
                owed_preview = round(amount * (1 + rate))
                terms = f" at {int(rate * 100)}% interest" if rate else ""
                return self.propose(ag, "loan",
                                    f"a loan of {amount} coins{terms} — {owed_preview} to pay back",
                                    "lend", str(amount))

            if ag.id == "bram":
                # the bank lends out of its reserves, at the bank's rate
                if owed_already + amount > 120:
                    return f"refused — {other.name} is already {owed_already} deep with the bank"
                if self.bank_reserves - amount < 80:
                    return "the bank did not have the reserves"
                rate = self.rate()
                self.bank_reserves -= amount
                other.cash += amount
                source = "Bank"
            else:
                # anyone can front a friend money out of their own pocket
                if ag.cash < amount:
                    return f"could not spare {amount} coins"
                if owed_already + amount > 60:
                    return f"refused — {other.name} already owes them {owed_already}"
                rate = 0.0
                self.pay(ag, other, amount, "a loan between friends")
                source = ag.name

            owed = round(amount * (1 + rate))
            other.debts[ag.id] = owed_already + owed
            terms = f" at {int(rate * 100)}% interest" if rate else ""
            other.remember(f"{ag.name} lent me {amount} coins; I owe {owed} back.", 3, source=ag.id)
            ag.remember(f"I lent {other.name} {amount} coins{terms}. They owe me {owed}.", 3)
            if source == "Bank":
                self.flows.append({"t": time.strftime("%H:%M:%S"), "from": "Bank",
                                   "to": other.name, "amount": amount, "why": "loan"})
            self.event(f"{ag.name} lent {other.name} {amount} coins, {owed} due back")
            return f"lent {other.name} {amount} coins, {owed} due back{terms}"

        if action == "repay":
            creditor = self.party(target) or self.agents.get("bram")
            if creditor is None:
                return "owed nothing to anyone"
            owed = ag.debts.get(creditor.id, 0)
            if owed <= 0:
                return f"owed {creditor.name} nothing"
            try:
                offer = int(float(str(arg).split(",")[0].strip()))
            except (TypeError, ValueError):
                offer = owed
            pay = max(0, min(ag.cash, owed, offer))
            if pay <= 0:
                return f"could not afford to repay {creditor.name} anything"
            ag.cash -= pay
            ag.debts[creditor.id] = owed - pay
            if creditor.id == "bram":
                self.bank_reserves += pay          # the bank's money goes back to reserves
            else:
                creditor.cash += pay
            self.adjust_trust(creditor, ag.id, 0.25)
            self.flows.append({"t": time.strftime("%H:%M:%S"), "from": ag.name,
                               "to": creditor.name, "amount": pay, "why": "repayment"})
            left = ag.debts[creditor.id]
            self.event(f"{ag.name} repaid {creditor.name} {pay} coins"
                       + (f", {left} still owing" if left else " and cleared the debt"))
            return f"repaid {creditor.name} {pay} coins, {left} still owing"

        if action == "restock":
            shop = self.shop_of(ag.id)
            if shop is None:
                return "keeps no shop to restock"
            if time.time() - ag.last_restock < 35:
                return "had already restocked recently"
            query = (arg or target or "wool scarf").strip()
            if len(query) < 3:
                return "could not think what to restock"
            if len(shop.goods) >= 14 and not shop.good(query):
                return "had no shelf space for anything new"
            ag.last_restock = time.time()

            match = None
            for o in self.offers:
                if query.lower() in o["name"].lower() or o["name"].lower() in query.lower():
                    match = o; break
            if match is None and self.offers:
                match = self.offers[0]
            if match is None:
                if query not in self.wanted:
                    self.wanted.append(query)
                return f"sent word to the supplier about '{query}' and is waiting on a price"
            self.offers.remove(match)
            if query not in self.wanted:
                self.wanted.append(query)

            unit_cost = max(1, round(match["price"] * 0.6))
            units = 6
            bill = unit_cost * units
            if ag.cash < bill:
                return (f"found {match['name']} at {unit_cost} coins wholesale but needed {bill} "
                        f"for a case and only had {ag.cash}")
            ag.cash -= bill
            self.flows.append({"t": time.strftime("%H:%M:%S"), "from": ag.name,
                               "to": "Supplier", "amount": bill, "why": f"wholesale {match['name']}"})
            existing = shop.good(match["name"])
            if existing:
                existing["qty"] += units
                existing["cost"] = unit_cost
                shelf_price = existing["price"]
            else:
                match["cost"] = unit_cost
                match["qty"] = units
                match["base"] = match["price"]
                shop.goods.append(match)
                shelf_price = match["price"]
            self.event(f"{ag.name} took delivery of {units} {match['name']} at {unit_cost} coins "
                       f"each, {bill} in all: {match['real_title'][:34]}")
            return (f"bought {units} {match['name']} from the supplier at {unit_cost} coins each "
                    f"({bill} in all), shelved at {shelf_price}")

        if action in ("promise", "campaign", "pledge"):
            if not ag.politician:
                return "holds no office and stands for nothing"
            want = (arg or target or "").strip().lower().replace(" ", "_")
            key = next((k for k in POLICIES if k == want or want in k or k in want), None)
            if key is None:
                key = max(POLICIES, key=lambda k: self.stands_to_gain(ag, k))
            if ag.promise == key:
                return f"had already pledged to {POLICIES[key]['pitch']}"
            ag.promise = key
            pitch = POLICIES[key]["pitch"]
            ag.speak(f"Elect me and I will {pitch}.")
            for other in self.agents.values():
                if other.id != ag.id:
                    other.remember(f"{ag.name} is campaigning to {pitch}.", 4, source=ag.id)
            self.event(f"{ag.name} pledged to {pitch}")
            return f"pledged to {pitch}"

        if action in ("open_stall", "open_shop", "found_business"):
            if self.shop_of(ag.id):
                return "already keeps a shop"
            plot = self.free_plot()
            if plot is None:
                return "found no free plot in the market — every pitch is taken"
            if ag.cash < self.stall_cost():
                return (f"needs {self.stall_cost()} coins to take a market plot and has only {ag.cash}. "
                        f"Keep working, or ask Bram for the capital.")
            ag.cash -= self.stall_cost()
            name = (arg or target or f"{ag.name}'s Stall").strip()[:28] or f"{ag.name}'s Stall"
            if ag.name.lower() not in name.lower():
                name = f"{ag.name}'s {name}"
            shop = Shop(ag.id, name, plot, [])
            self.shops.append(shop)
            # they work for themselves now
            old_boss = self.party(ag.employer) if ag.employer else None
            ag.employer, ag.wage = "", 0
            ag.role = "Shopkeeper"
            ag.goal = f"Make {name} pay: stock it, price it, and take custom off the competition."
            ag.tx, ag.ty = place_center(plot)
            ag.remember(f"I opened {name} on the market plot. I work for myself now.", 5)
            if old_boss:
                old_boss.remember(f"{ag.name} left my employ to open {name}.", 4, source=ag.id)
                self.adjust_trust(old_boss, ag.id, -0.2)
            for other in self.agents.values():
                if other.id != ag.id:
                    other.remember(f"{ag.name} has opened {name} in the market.", 3, source=ag.id)
            self.event(f"{ag.name} opened {name} — working for themselves now")
            return (f"opened {name} for {self.stall_cost()} coins. The shelves are empty — "
                    f"restock, or buy stock off Fig, then set prices")

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
            "clock": round(self.clock(), 3),
            "phase": self.phase,
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
            "player": {"x": round(self.you.x, 2), "y": round(self.you.y, 2),
                       "cash": self.you.cash, "inventory": self.you.inventory,
                       "name": self.you.name},
            "shops": [{"owner": sh.owner, "name": sh.name, "place": sh.place,
                       "owner_name": self.agents[sh.owner].name if sh.owner in self.agents else sh.name,
                       "color": self.agents[sh.owner].color if sh.owner in self.agents else "#ab977c",
                       "goods": sh.goods} for sh in self.shops],
            "shop": [{**g, "shop": sh.name, "owner": sh.owner} for sh in self.shops for g in sh.goods],
            "plots": list(PLOTS),
            "bank_reserves": self.bank_reserves,
            "interest_rate": self.rate(),
            "treasury": self.treasury,
            "mayor": self.mayor,
            "mayor_name": self.agents[self.mayor].name if self.mayor in self.agents else "",
            "policy": POLICIES[self.policy]["law"] if self.policy else "",
            "ballot": [{"id": c.id, "name": c.name, "color": c.color,
                        "pitch": POLICIES[c.promise]["pitch"] if c.promise else "has pledged nothing"}
                       for c in self.candidates()] if self.ballot_open else [],
            "election_in": self.election_in(),
            "flows": self.flows[-14:],
            "price_mult": round(self.price_mult, 2),
            "proposals": [{"id": p["id"], "name": p["name"], "color": p["color"],
                           "kind": p["kind"], "text": p["text"]} for p in self.proposals],
            "conversations": [{
                "id": c.id, "a": c.a, "b": c.b, "closed": c.closed,
                "mine": "stranger" in c.pair,
                "last_t": round(c.last_t, 1),
                "names": [self.party(c.a).name, self.party(c.b).name],
                "colors": [self.party(c.a).color, self.party(c.b).color],
                "lines": c.lines[-40:],
            } for c in sorted(self.conversations, key=lambda c: c.last_t, reverse=True)[:14]],
            "log": self.log[-40:],
        }
