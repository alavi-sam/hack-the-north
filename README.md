# Untitled Town — Plan & Overview

A 2D pixel town where every resident is an AI agent with a personality, goals, memories, and relationships. They walk around, trade, lend, lie, and gossip with each other. The player walks in as another resident and can talk to anyone, buy things, borrow money, or plant a rumor and watch it spread. The shop's stock is **real products from Shopify's Global Catalog**, so buying in-game hands you a real product/checkout link.

---

## 0. Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # then put your key in
.venv/bin/uvicorn server:app --reload --port 8000
```

Open <http://localhost:8000>. WASD to walk, `E` to select the nearest resident, then type
in the bottom bar — **Talk** speaks to them, **Whisper rumour** plants one. The right panel
has Talk / Minds / Relations / Shop / Log; the god buttons force events.

**Talk** is the tab to watch: agent dialogue is threaded per pair, newest first, so you can
follow one negotiation instead of reading interleaved chatter. A thread runs up to 6 lines
with strict turn-taking, then closes (each side keeps a summarising memory) and that pair
goes quiet for 50s. The Log is economy and rumours only.

**Files:** `server.py` (WebSocket + two-tier loop) · `world.py` (state, economy, action
validation) · `llm.py` (model client) · `catalog.py` (Shopify Global Catalog) ·
`static/index.html` (whole frontend).

`.env` keys: `OPENAI_API_KEY`; optional `OPENAI_MODEL` (defaults to `gpt-4.1-nano`),
`LLM_CONCURRENCY`, `SLOW_SECONDS`.

> **Model note:** agents call OpenAI directly using GPT-4.1 nano, a low-cost model.
> Requests retry once; if both attempts fail, the agent acts on instinct instead of freezing.
> Old `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_FALLBACK_MODELS` settings are ignored.

Use **Time** in the bottom toolbar to run the town at **5×, 10×, or 20×**.
Use **Pause** at the bottom left to freeze the shared town and stop agent and player-chat
prompts. Pending requests are cancelled; tokens already processed upstream may still be billed.
**Resume** continues at the previous speed, without advancing through the paused time.
Choose **Normal** or **End fast-forward & recap** to return to 1× and open the
collapsible recap at the bottom right of the town. It covers that fast-forward period:
completed purchases of 20+ coins, election results, new businesses, dividends, stock
offerings, major town events, and start-to-finish share-price changes, when they occur.
The recap uses recorded simulation data and requires no AI call. Time controls apply
to the shared town for all connected players. Faster time can make more agent API calls;
each agent has at most one decision in flight, with the existing concurrency limit.

---

## 1. Overview

**One-liner:** *A tiny society run by AI agents, where the shop is a real Shopify store and you can poke the economy.*

**What judges see in 5 minutes:**
1. A town already alive: agents walking, talking, speech bubbles, no user input.
2. A side panel showing each agent's *thought* and the *relationship graph* changing live.
3. The player intervenes (spreads a rumor, takes a loan, buys something) and the town visibly reacts.
4. The player buys a real product; a real Shopify link opens.

**Why it scores (Hack the North criteria):**
| Criterion | How we hit it |
|---|---|
| WOW | Emergent behavior nobody scripted; a rumor you plant comes back changed |
| Technical ability | Memory + relationship model, two-tier decision loop, server-validated actions, live Shopify Global Catalog via MCP |
| Originality | AI society where the economy is backed by a real commerce API |
| Design | Game world, speech bubbles, readable thought panel, relationship graph |

---

## 2. The cast (5 agents)

Each agent has: **persona**, **goal**, **secret**, **starting cash/items**, and **initial relationships**. Secrets and conflicting goals are what create drama.

| Agent | Role | Goal | Secret / flaw |
|---|---|---|---|
| **Mira** | Merchant, runs the shop | Grow profit, never run out of stock | Overprices when she thinks nobody compares |
| **Bram** | Banker | Keep the bank solvent | Quietly favors people who flatter him |
| **Wren** | Worker (hard-working) | Save enough to open her own stall | Deep in debt to Bram, hides it |
| **Fig** | Farmer / supplier | Sell produce at a fair price | Secretly undercuts Mira to hurt her |
| **Kit** | Drifter / con artist | Get rich without working | Lies about who they are, spreads false rumors |

Roles map to the "merchant / banker / worker" idea; Fig and Kit exist to create conflict, rivalry, and deception. Add more only after the core loop works.

---

## 3. How an agent thinks

Every agent has this state:

```
persona        static text (who they are, how they talk)
needs/state    cash, inventory, location, energy
memory         list of observations: {text, time, importance}
relationships  {other_agent: trust (-1..1), notes}
plan           today's intentions (1-3 lines)
```

### Two-tier loop (this is what keeps it fast)

| Tier | What | Runs | LLM? |
|---|---|---|---|
| **Fast** | Walking, pathfinding, animation, proximity, working/resting timers | every frame (~10-30 Hz) | No |
| **Slow** | Deciding what to do next, conversations, reflection | every ~5-8 s per agent, staggered | Yes |

### Slow tick (per agent)
1. Build a compact prompt: persona + state + top-N relevant memories (recent + important) + nearby agents + last events.
2. LLM returns **structured JSON**: `{thought, action, target, say}`.
3. **World validates the action** (can they afford it? is the target in range?). The LLM *proposes*, the world *decides*. This prevents hallucinated money/items and keeps the sim coherent.
4. Result is written back into the memories of everyone involved.

### Action set (keep it small)
`move_to(place)`, `talk_to(agent, intent)`, `buy(item)`, `sell(item, price)`, `lend(agent, amount, rate)`, `repay(agent)`, `work`, `rest`, `gossip(agent, about, claim)`.

### Conversations
Two agents alternate turns for up to ~4-6 lines, each turn one LLM call, run in the background while the rest of the town keeps moving. Speech shows as bubbles. At the end each side stores a one-line memory and updates trust.

### Reflection (the "they feel alive" trick)
Every N events (or once per sim "day"), an agent summarizes recent memories into 1-2 higher-level beliefs ("Kit lied to me about the boots — don't trust Kit"). These get retrieved into future prompts. Use a stronger model here since it runs rarely.

### Gossip and trust
When A gossips to B about C, B stores it as a memory with a source. B's trust in C shifts by (how much B trusts A) × (claim severity). This is what makes rumors spread and distort, and it's the source of most emergent moments.

---

## 4. The economy — a closed supply chain

Every coin in the town is a **transfer between two purses**. Nothing is minted except
ordinary townsfolk shopping at Mira's (`world.townsfolk_tick`), which is the only money
entering the system. `World.pay()` is the single chokepoint every transfer goes through,
so the ledger cannot drift.

```
Townsfolk ──buy at retail──▶ Mira (merchant)
                              │  ▲
        pays wages from her   │  │ buys wholesale (from Fig, or the Shopify supplier)
        own purse             ▼  │
                            Wren ──spends wages──▶ the shop
Fig (supplier) ──grows goods with `work`, sells them──▶ Mira
Bram (bank) ──lends at 20%──▶ anyone; profits only when the debt is repaid
Kit ──no job, no goods──▶ must borrow, con, or talk coin out of people
```

- **Producers** (`Fig`) turn `work` into *goods*, not coin. They only earn by selling.
- **Employees** (`Wren`) are paid **out of their employer's actual cash**. If Mira is broke,
  payroll is missed, trust drops, and it lands in the Log. `hire` can poach someone off a
  rival, which costs the rival a chunk of trust.
- **The shop has finite stock.** Items carry a `cost` and a `qty`; they sell out. Mira's
  `set_price` is bounded to between cost and 3× cost, so her "overprice when nobody
  compares" flaw is a real, visible price change rather than flavour text.
- **Loans carry interest.** `lend` records principal × 1.2 as the debt; `repay` pays it
  down and the interest lands in the bank's reserves.
- **Deals execute.** `sell_to` / `buy_from` / `pay` let a negotiation end in a transaction —
  a bribe, hush money, or a haggled price. Without these the conversations were theatre.

### 4b. Original notes (rule-based, not LLM)

- **Items:** ~8-10 goods. Shop items come from the Global Catalog (see §5); produce (bread, apples) is local.
- **Prices:** Merchant sets prices (LLM decision), bounded by rules (can't go below cost or above 3x). Demand pressure nudges suggested price.
- **Wages:** Workers earn per work cycle from whoever employs them.
- **Bank:** Holds reserves, lends at a rate Bram picks (LLM decision, bounded). Missed repayment => default => trust collapses in memory of the town.
- **Ledger:** One authoritative server-side ledger. Agents only see what their memories say.

---

## 4c. Days, nights and elections

**The clock.** A day lasts `DAY_SECONDS` (default 240s). At dusk everyone walks home and
sleeps — sleeping costs no tokens, which also keeps the nights cheap. Kit the drifter is the
exception: he keeps his own hours and works the dark alone. Standing next to a sleeper wakes
them. The map takes a night wash and lamps come on in the windows.

**The exchange.** Every shop is cut into 20 shares, and a quarter of each is on the market
from the start, so there is something to trade on day one. A share is valued off the books you
already have — stock at cost plus a multiple of the day's takings — and the quoted price eases
toward that, leaning on what the town believes.

- `issue_shares(n)` lets an owner float part of their own stake to raise coin at once. It is a
  second route to capital beside Bram's loans, at the cost of keeping less of the profit.
- `buy_shares` / `sell_shares` deal against the exchange, which always stands ready, so there
  is never a missing counterparty. Trades move the price.
- Profitable shops pay a dividend on the day's takings, split across the holders.

**This is what finally gives gossip teeth.** Rumours spread well but moved nothing measurable
— now a rumour naming a shopkeeper drags their shares, weighted by how much the listener
trusts the teller. Whisper that Mira waters her milk and the ticker drops: her standing reads
"talked down", and anyone holding her stock is out of pocket. Buying in before you start the
talk is a strategy the rules permit, and Kit is exactly the sort to work it out.

Elections bite here too: a `cheap_bread` cap squeezes margins and shop values with it, while
`free_market` lifts them.

**The Shopify hustle.** The Global Catalog is not just the shop's opening stock — it is a
live supplier anyone can buy from. A background task keeps a pool of real listings topped up
off the MCP endpoint (in a worker thread, so the search never blocks the simulation).

- `source(query)` is open to **any** agent with coin, not just shopkeepers: buy a case of 3
  real products at wholesale into your own bag and sell them on at a markup
- shipments are scarce — taking one removes it from the pool, so agents race for the same case
- a shopkeeper pays a **premium for a line they do not carry** and little for more of what is
  already piled up, so the margin comes from finding what nobody else has
- `world.catalogue` remembers the real title, image and product URL behind every name, so a
  product keeps its Shopify identity however many hands it passes through
- the player has the same hustle: the Shop tab lists the supplier's real goods with images and
  case prices, and every good on sale in town carries a "view the real product" link

**Earning as the player.** *Work a shift* must be done **at a workplace** — the farm, the
shop, the bank or a stall — and takes seven seconds, after which you are paid: 3 coins for odd
jobs, or your wage if someone has hired you — agents can offer you a job, which arrives as an offer you accept. Anything in
your bag can be sold to the nearest shopkeeper at wholesale. Vigour limits how hard you can
work and comes back on its own.

**Social mobility.** Two market pitches sit empty on the map marked "to let". Any agent with
`STALL_COST` coins can `open_stall`: they leave their employer, become a shopkeeper with
their own goods, costs and prices, and their goal is rewritten. Buyers — agents, townsfolk
and the player — go to whichever shop is **cheapest**, so undercutting is a real strategy and
a markup really costs you custom.

**Politics.** A *Call an election* button on the god panel opens the ballot whenever you want
one, rather than waiting for the fifth day. Orla (the sitting alderman, quietly funded by shopkeepers) and Devi (an
agitator for the workers) stand for election every `ELECTION_EVERY` days. They `promise` one
of four policies and campaign by talking people round. On election day every resident votes
on **self-interest weighted by trust** — a worker gains from a stipend, an owner from a free
market, a debtor from cheap credit — and the player gets a ballot too.

The winner's promise becomes law, and the law actually binds:

| Policy | What it changes in the rules |
|---|---|
| `cheap_bread` | Food is capped at 4 coins; `set_price` cannot exceed it |
| `free_market` | Market pitches cost half as much |
| `workers_stipend` | Everyone without a shop draws 5 coins a day from the treasury |
| `cheap_credit` | The bank may only charge 10% interest |

The treasury fills from a 10% levy on townsfolk purchases, so a promise can bankrupt the town
that voted for it.

**Conversations are lasting.** Each pair of people has one thread that is never discarded —
exchanges open and close inside it, but the history stays. Your own chats persist per person
and are shown back to the agent when they reply, so you can pick a conversation up where you
left it. The Talk tab filters by person, or by "Your chats".

---

## 5. Shopify integration (real, not mocked)

The Merchant's stock is pulled from the **Shopify Global Catalog MCP** (`https://catalog.shopify.com/api/ucp/mcp`, tool `search_catalog`). We already have a working script (`catalog.py`).

- **At startup:** run 5-8 searches (e.g. "coffee beans", "leather boots", "wool blanket", "camping lantern") with a price cap, pick the top result of each, cache name/price/image/URL.
- **In-world:** each becomes a shop item with a pixel-icon (or the product image in the shop UI). Prices scaled to in-game coins.
- **Player buys:** shop UI shows the real product; "Buy" deducts coins in-game and opens the real product/checkout URL.
- **Agent behavior:** Mira can *search the catalog live* as an action ("restock: find cheaper boots") — a real MCP call triggered by an agent's decision. That's the technical showpiece for the Shopify prize.
- **Stretch:** results include offers from multiple merchants per product, so Mira can compare suppliers and Fig can undercut her with a real cheaper listing.

**Devpost reminder:** select the Shopify prize on the submission **by 2:00 PM Saturday** or it won't be judged.

---

## 6. Player interaction

- WASD / arrows to walk, `E` to talk to a nearby agent.
- **Free-text chat** with any agent (they respond in character, using their memory of you).
- **Shop:** buy real products with in-game coins.
- **Bank:** request a loan; Bram decides based on your history and how he feels about you.
- **Whisper:** tell an agent a rumor. Watch it propagate and mutate. (Best demo moment.)
- **God panel (for demos):** buttons to trigger events — *market crash, festival, stranger arrives, shortage*. Lets you steer the demo instead of waiting for drama.

---

## 7. UI

**Game canvas (center):** tilemap, sprites, speech bubbles, name tags.

**Right panel (tabs):**
- **Minds:** each agent's current thought + goal + last action (this makes the AI legible).
- **Relationships:** live graph, nodes = agents, edge color/width = trust.
- **Economy:** cash per agent, shop prices over time, bank reserves.
- **Log:** timestamped event feed ("Kit told Wren that Mira waters down the milk").

**Bottom:** chat box when talking to someone, inventory/coins, god-panel buttons.

---

## 8. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | **Phaser 3** loaded from CDN in a single HTML page | No build step, fast to start, tilemap + sprite support built in |
| Backend | **Python + FastAPI + WebSocket** | Python is already on your machine; easy async LLM calls |
| Realtime | WebSocket pushes world state + events to the client | Client only renders; server owns truth |
| LLM | OpenAI GPT-4.1 nano (`gpt-4.1-nano`) for agent decisions and player chat | Low cost and low latency |
| Structured output | JSON schema / tool-use for the `{thought, action, target, say}` response | Reliable action parsing |
| Shopify | Global Catalog MCP via plain HTTPS (already working) | Real data, no store setup |
| Art | Free tileset + sprite pack (Kenney, itch.io) | Rules allow public assets; don't draw your own |

**Key design rule:** server is the single source of truth. Frontend never decides outcomes.

---

## 9. Build plan (4 people)

| Person | Owns |
|---|---|
| **A — Game/frontend** | Phaser scene, tilemap, sprites, movement, speech bubbles, chat UI, side panels |
| **B — Agent brain** | Prompts, memory store + retrieval, slow-tick loop, structured action parsing, conversations, reflection |
| **C — World/economy** | Ledger, action validation, prices, bank/loans, gossip-trust math, god-panel events |
| **D — Shopify + glue + demo** | Global Catalog fetch/cache, shop UI, buy flow, WebSocket protocol, Devpost, demo script |

### Timeline (code must be written after 12:00 AM Sat; Devpost due 2:00 PM Sat)

**Phase 1 — Skeleton (midnight to ~4 AM)**
- Server holds world state, pushes it over WebSocket.
- Client renders map + 3 walking sprites from server state.
- One agent makes one LLM decision and the action executes.

**Phase 2 — Core loop (~4 AM to ~10 AM)**
- All 5 agents on staggered slow ticks with validated actions.
- Talk / buy / lend / work working end to end.
- Global Catalog items appear in the shop.
- Thought panel visible.

**Phase 3 — Submit (~10 AM to 2 PM)**
- Ugly but working end to end. **Submit on Devpost with the Shopify prize selected.** Screen-record a backup demo video now.

**Phase 4 — Depth (afternoon/evening Sat)**
- Memory retrieval + reflection, gossip/trust, relationship graph, whisper mechanic.
- Agent-triggered live catalog search, god panel, economy chart.

**Phase 5 — Polish + rehearse (night into Sun 8 AM)**
- Art pass, sound optional, tune agent personalities so drama actually happens.
- Rehearse the 5-minute demo 3+ times; record a fallback video; freeze code.

### Cut list (drop in this order if behind)
1. Economy chart 2. Reflection 3. Live agent catalog search (keep cached stock) 4. Relationship graph (keep thought panel) 5. Two of the five agents.
**Never cut:** agents visibly talking to each other, the thought panel, the real Shopify item purchase.

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LLM latency makes the town feel dead | Two-tier loop; movement never waits on the LLM; stagger agent ticks; use Haiku |
| API cost / rate limits | Short prompts, top-N memories only, cap conversation length, cache catalog |
| Agents loop or do nothing interesting | Strong conflicting goals + secrets, periodic god-panel events, per-agent "boredom" nudge toward their goal |
| Hallucinated actions or money | Server validates every action; LLM only proposes |
| Personalities blur together | Distinct voice notes per agent in the persona; test with side-by-side transcripts |
| Demo fails live | Pre-recorded fallback video, seeded scenario that reliably produces drama, god-panel to force events |
| Wi-Fi / catalog down | Cache catalog results to a JSON file at startup and fall back to it |
| Scope creep | Follow the cut list; MVP is 3 agents before adding 2 more |

---

## 11. Demo script (5 minutes, live)

1. **(0:00)** Open on the town already running. "Nothing is scripted. Every resident is an AI agent with its own goals and memory."
2. **(0:30)** Point at the Minds panel: Kit's thought reads "Wren owes Bram — I can use that." Show it change.
3. **(1:00)** Whisper a rumor to Kit ("Mira waters down her milk"). Open the relationship graph.
4. **(1:45)** Watch the rumor travel; Kit tells Fig, Fig confronts Mira. Trust edges shift on the graph.
5. **(2:30)** Walk to the shop, show real products from Shopify's Global Catalog. Buy one; the real product page opens.
6. **(3:15)** Ask Bram for a loan; he decides based on how he feels about you (and says why).
7. **(4:00)** Hit *market crash* on the god panel; prices and moods react.
8. **(4:30)** Close: "Real commerce data, a memory and trust system, and every decision you saw was an LLM reasoning over world state."

---

## 12. Next steps (before midnight)

- [ ] Agree on the 5 characters (or swap in your own — keep the conflicts).
- [ ] Pick and download a tileset + character sprites.
- [ ] Get an Anthropic API key working on everyone's machine; confirm Python 3.10+.
- [ ] Decide who owns which of the four roles.
- [ ] Planning and public assets are allowed beforehand; **no project code until midnight.**
