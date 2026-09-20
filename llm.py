"""Thin async OpenAI chat client with JSON extraction."""
import json, os, re, asyncio, pathlib, httpx
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

API_KEY = os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
paused = False

_client = httpx.AsyncClient(timeout=45)
# Bound concurrent agent requests.
_gate = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "3")))


async def _once(model: str, system: str, user: str, max_tokens: int) -> str:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.9,
    }
    # several free models are reasoners; their thinking would eat the whole budget.
    # OpenAI rejects the param outright, so only send it to OpenRouter.
    if "openrouter" in BASE_URL:
        body["reasoning"] = {"enabled": False}
    r = await _client.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=body,
    )
    if r.status_code == 429:
        raise RuntimeError("rate-limited")
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


async def chat(system: str, user: str, max_tokens: int = 400) -> str:
    """Retry once; return '' on failure so callers can fall back to instinct."""
    if paused or not API_KEY:
        return ""
    async with _gate:
        for attempt in range(2):
            if paused:
                return ""
            try:
                out = await _once(MODEL, system, user, max_tokens)
                if out:
                    return out
            except Exception as e:
                if attempt:
                    print(f"[llm] {MODEL}: {e}")
            if attempt == 0:
                await asyncio.sleep(1.0)
    print("[llm] request failed; agent fell back to instinct")
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
