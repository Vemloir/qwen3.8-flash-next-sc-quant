"""
Shared pieces of the calibration regeneration: reasoning modes, the streaming client for the
OpenAI-compatible server (vLLM with NVIDIA's FP4-Local checkpoint, reached through an SSH port
forward to the Colab VM), the loop detector and the resumable JSONL store.

There is no generation length cap. A request ends at the end of the response, at the context
limit (finish_reason "length"), or when the loop detector sees the tail of the stream repeating
(status "loop"); the last two are kept in the raw output for inspection but never become rows.
"""
import asyncio, json, os, re, time, zlib
import aiohttp

MODES = {
    "off": {"enable_thinking": False},
    "low": {"enable_thinking": True, "reasoning_effort": "low"},
    "medium": {"enable_thinking": True, "reasoning_effort": "medium"},
    "xhigh": {"enable_thinking": True, "reasoning_effort": "xhigh"},
}
# the model's generation_config.json
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
MODEL = "nvidia/Qwen3.8-Flash-Next-FP4-Local"


def api_key():
    k = os.environ.get("REGEN_API_KEY")
    if not k:
        path = os.path.expanduser("~/.config/sc-quant/regen_api_key")
        k = open(path).read().strip() if os.path.exists(path) else None
    if not k:
        raise SystemExit("set REGEN_API_KEY or write the key to ~/.config/sc-quant/regen_api_key")
    return k


class LoopDetector:
    """
    Stops only a runaway: within the last `window` characters some `span`-character block occurs
    at least `repeats` times and the tail compresses below `ratio`. On the first calibration set
    music (ABC repeats bars) and a chess loop both repeated a 160-character block 10-13 times, so
    milder repetition is left to qa.py and a human look; this only keeps one degenerate stream
    from running to the context limit.
    """
    def __init__(self, window = 16000, span = 200, repeats = 20, ratio = 0.15, every = 2000):
        self.window, self.span, self.repeats, self.ratio, self.every = window, span, repeats, ratio, every
        self.buf, self.n, self.next_check = [], 0, every

    def feed(self, text):
        self.buf.append(text)
        self.n += len(text)
        if self.n < self.next_check:
            return False
        self.next_check = self.n + self.every
        tail = "".join(self.buf)[-self.window:]
        self.buf = [tail]
        if len(tail) < self.window // 2:
            return False
        if len(zlib.compress(tail.encode(), 6)) / max(1, len(tail.encode())) > self.ratio:
            return False
        probe = tail[-self.span:]
        return tail.count(probe) >= self.repeats or max_block_repeats(tail, self.span) >= self.repeats


def max_block_repeats(text, span, step = 37):
    best = 0
    for i in range(0, max(1, len(text) - span), step):
        best = max(best, text.count(text[i:i + span]))
    return best


async def chat_stream(session, base_url, key, messages, mode, extra = None, timeout = None):
    """
    One streamed chat completion. Returns {"reasoning", "content", "tool_calls", "finish",
    "status", "usage", "seconds"}; status is "ok", "length", "loop" or "error".
    """
    body = {"model": MODEL, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": dict(MODES[mode]), **SAMPLING}
    if "reasoning_effort" in MODES[mode]:
        body["reasoning_effort"] = MODES[mode]["reasoning_effort"]
    body.update(extra or {})
    out = {"reasoning": [], "content": [], "finish": None, "usage": None}
    det = LoopDetector()
    t0 = time.time()
    status = "ok"
    try:
        async with session.post(f"{base_url}/v1/chat/completions", json = body,
                                headers = {"Authorization": f"Bearer {key}"},
                                timeout = aiohttp.ClientTimeout(total = timeout, sock_read = 1800)) as r:
            if r.status != 200:
                return {"status": "error", "error": f"HTTP {r.status}: {(await r.text())[:500]}"}
            async for raw in r.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                if ev.get("usage"):
                    out["usage"] = ev["usage"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    rs = d.get("reasoning_content") or d.get("reasoning")
                    if rs:
                        out["reasoning"].append(rs)
                    if d.get("content"):
                        out["content"].append(d["content"])
                    if ch.get("finish_reason"):
                        out["finish"] = ch["finish_reason"]
                    if det.feed((rs or "") + (d.get("content") or "")):
                        status = "loop"
                if status == "loop":
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return {"status": "error", "error": repr(e)[:500], "seconds": round(time.time() - t0, 1)}
    if status == "ok" and out["finish"] == "length":
        status = "length"
    elif status == "ok" and out["finish"] != "stop":
        status = "error"
    return {"reasoning": "".join(out["reasoning"]), "content": "".join(out["content"]),
            "finish": out["finish"], "status": status, "usage": out["usage"],
            "seconds": round(time.time() - t0, 1)}


class Store:
    """Append-only JSONL keyed by "key"; a rerun skips keys whose last record is final."""
    def __init__(self, path):
        self.path = path
        self.done = {}
        if os.path.exists(path):
            for line in open(path):
                if line.strip():
                    r = json.loads(line)
                    self.done[r["key"]] = r
        self.lock = asyncio.Lock()

    def has(self, key, retry_errors = True):
        r = self.done.get(key)
        return r is not None and not (retry_errors and r["status"] == "error")

    async def put(self, rec):
        async with self.lock:
            self.done[rec["key"]] = rec
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii = False) + "\n")


def has_cyrillic(text):
    return bool(re.search("[а-яА-ЯёЁ]", text))
