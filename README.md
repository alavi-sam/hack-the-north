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

`.env` keys: `LLM_API_KEY`, `LLM_MODEL`, optional `LLM_BASE_URL`, `LLM_FALLBACK_MODELS`,
`LLM_CONCURRENCY`, `SLOW_SECONDS`.

> **Model note:** free OpenRouter models are rate-limited hard and several are reasoners
> that return empty completions. `llm.py` disables reasoning tokens and falls through a
> model chain; if every model fails an agent acts on instinct instead of freezing. For the
> demo, use a paid model — `inclusionai/ling-3.0-flash-vl:free` was the most reliable free one.

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
| LLM | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) for ticks/dialogue; a stronger model for reflection/player chat | Cheap + fast where it's frequent, smart where it's rare |
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
