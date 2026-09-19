"""Shopify Global Catalog (UCP MCP) -> shop stock, with a disk cache + offline fallback."""
import json, os, pathlib, httpx

MCP_URL = "https://catalog.shopify.com/api/ucp/mcp"
AGENT_PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-08-25/valid-with-capabilities.json"
CACHE = pathlib.Path(__file__).parent / "catalog_cache.json"

# What Mira stocks the shop with at startup. (query, in-game display name)
SEED_QUERIES = [
    ("coffee beans", "Coffee Beans"),
    ("leather boots", "Leather Boots"),
    ("wool blanket", "Wool Blanket"),
    ("camping lantern", "Lantern"),
    ("pocket knife", "Pocket Knife"),
    ("straw hat", "Straw Hat"),
]

FALLBACK = [
    {"name": n, "price": p, "real_title": n, "url": "", "image": ""}
    for n, p in [("Coffee Beans", 14), ("Leather Boots", 40), ("Wool Blanket", 26),
                 ("Lantern", 18), ("Pocket Knife", 12), ("Straw Hat", 9)]
]


def _rpc(name: str, arguments: dict, timeout=30):
    r = httpx.post(
        MCP_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "mcp error"))
    return body["result"]["structuredContent"]


def search(query: str, max_cents: int = 15000, limit: int = 1):
    """Live catalog search. Returns a list of {name, price, real_title, url, image}."""
    sc = _rpc("search_catalog", {
        "meta": {"ucp-agent": {"profile": AGENT_PROFILE}},
        "catalog": {
            "query": query,
            "context": {"currency": "CAD", "address_country": "CA", "language": "en"},
            "filters": {"price": {"max": max_cents}, "available": True},
        },
    })
    out = []
    for prod in sc.get("products", []):
        if len(out) >= limit:
            break
        variants = prod.get("variants") or []
        if not variants:
            continue
        title = prod.get("title", "")
        # the catalog occasionally returns non-English listings; they read as noise in-game
        if not title or sum(c.isascii() for c in title) / len(title) < 0.8:
            continue
        v = variants[0]
        cents = (v.get("price") or {}).get("amount", 0)
        if not cents or not v.get("url"):
            continue
        media = (prod.get("media") or v.get("media") or [{}])[0]
        out.append({
            "name": title[:40],
            "real_title": title,
            # in-game coins: roughly dollars / 3, so a $200 boot is ~66 coins
            "price": max(3, round(cents / 100 / 3)),
            "url": v.get("url", ""),
            "image": media.get("url", ""),
        })
    return out


def load_stock(refresh: bool = False):
    """Shop stock: cached file -> live MCP -> hardcoded fallback."""
    if CACHE.exists() and not refresh:
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    items = []
    for query, display in SEED_QUERIES:
        try:
            got = search(query)
            if got:
                got[0]["name"] = display
                items.append(got[0])
                continue
        except Exception as e:
            print(f"[catalog] '{query}' failed: {e}")
        items.append(next(f for f in FALLBACK if f["name"] == display))
    try:
        CACHE.write_text(json.dumps(items, indent=2))
    except Exception:
        pass
    return items


if __name__ == "__main__":
    for it in load_stock(refresh=True):
        print(f"{it['price']:>4}c  {it['name']:<16} {it['real_title'][:50]}")
