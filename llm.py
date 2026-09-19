"""Thin async OpenAI-compatible chat client (OpenRouter by default) with JSON extraction."""
import json, os, re, asyncio, pathlib, httpx
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

API_KEY = os.getenv("LLM_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "inclusionai/ling-3.0-flash-vl:free")
# Free models get rate-limited hard, so fall through a chain rather than going silent.
FALLBACKS = [m.strip() for m in os.getenv(
    "LLM_FALLBACK_MODELS",
    "inclusionai/ling-3.0-flash-vl:free,deepseek/deepseek-v4-flash-0731:free",
).split(",") if m.strip()]
MODELS = list(dict.fromkeys([MODEL] + FALLBACKS))

_client = httpx.AsyncClient(timeout=45)
# Free tiers share a small upstream pool, so never keep many calls in flight.
_gate = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "3")))


async def _once(model: str, system: str, user: str, max_tokens: int) -> str:
    r = await _client.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.9,
            # several free models are reasoners; their thinking would eat the whole budget
            "reasoning": {"enabled": False},
        },
    )
    if r.status_code == 429:
        raise RuntimeError("rate-limited")
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


# The chain is reordered as models prove themselves, so a rate-limited primary
# is not retried from scratch on every single decision.
_order = list(MODELS)


async def chat(system: str, user: str, max_tokens: int = 400) -> str:
    """Try the chain, preferring whatever answered last. '' if all fail (callers must cope)."""
    if not API_KEY:
        return ""
    async with _gate:
        for model in list(_order):
            for attempt in range(2):
                try:
                    out = await _once(model, system, user, max_tokens)
                    if out:
                        if _order[0] != model:      # promote the model that works
                            _order.remove(model)
                            _order.insert(0, model)
                            print(f"[llm] switched to {model}")
                        return out
                except Exception as e:
                    if attempt:
                        print(f"[llm] {model}: {e}")
                if attempt == 0:
                    await asyncio.sleep(1.0)
    print("[llm] every model failed; agent fell back to instinct")
    return ""


def extract_json(text: str):
    """Pull the first JSON object out of a model reply (fences, prose, <think>, truncation)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    if start < 0:
        return None
    blob = text[start:]
    try:
        return json.loads(blob)
    except Exception:
        pass
    # trailing junk after the object: cut at the balanced closing brace
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(blob):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                try:
                    return json.loads(blob[: i + 1])
                except Exception:
                    break
    # truncated mid-object (hit max_tokens): close what is open and salvage it
    patched = blob
    if in_str:
        patched += '"'
    patched += "}" * max(0, depth)
    try:
        return json.loads(patched)
    except Exception:
        return None
