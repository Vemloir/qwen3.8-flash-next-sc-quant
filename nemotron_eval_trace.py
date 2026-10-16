"""
Evaluation trace (qbench test_trace with score_spans) from NVIDIA's Nemotron post-training datasets released with
Nemotron 3.5 Super: data with no overlap with the MiMo task pool that calibration and the earlier evaluation traces
came from. The assistant turns of these conversations were written by Qwen3.5-397B (plus GLM-5.2 / Kimi-K2.6), a
close relative of the evaluated model, so the trace is teacher-forced rather than self-generated: neutral to every
quant compared, with no generation step that could favour one of them.

Rows are sampled from several byte offsets of each JSONL file (HTTP range requests, nothing is downloaded whole),
rendered with the model's chat template (thinking on when the turns carry reasoning, earlier reasoning kept), cut at
the last assistant turn that fits --max_row tokens (or cropped mid-turn if the first one does not fit), and scored on
the assistant turn bodies inside the row, latest turns first, up to --row_scored tokens per row.

    python nemotron_eval_trace.py -o eval/nemotron_trace.json
"""
import argparse, gzip, io, json, os, random, sys, urllib.request
from transformers import AutoTokenizer

Q = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(Q, "agent-env"))
from build_rows import normalize  # noqa: E402
from agent_eval_trace import assistant_spans  # noqa: E402

B = "https://huggingface.co/datasets/nvidia/"
SOURCES = [
    # name, path, rows, kind
    ("tool_calling", "Nemotron-SFT-Agentic-v4/resolve/main/data/tool_calling.jsonl", 10, "jsonl"),
    ("knowledge_agent", "Nemotron-SFT-Agentic-v4/resolve/main/data/knowledge_agent.jsonl", 8, "jsonl"),
    ("swe_opencode", "Nemotron-SFT-SWE-v3.5/resolve/main/data/train-0000{i}-of-00010.jsonl.gz", 8, "gz"),  # long rows, see LIMITS
    ("chat", "Nemotron-SFT-Instruction-Following-Chat-v4/resolve/main/data/chat.jsonl", 8, "jsonl"),
    ("multi_turn_if", "Nemotron-SFT-Instruction-Following-Chat-v4/resolve/main/data/multi_turn_instruction_following.jsonl", 8, "jsonl"),
    ("math_proofs", "Nemotron-Math-Proofs-v3-SFT/resolve/main/data/train-0000{i}-of-00010.jsonl", 6, "jsonl_multi"),
]
CHUNK = 4_000_000
# (max_row, max_prompt) overrides: the OpenCode system prompt with its tools alone is 10-12k tokens
LIMITS = {"swe_opencode": (16384, 14336)}


def fetch(url, start = None, size = None):
    h = {"Range": f"bytes={start}-{start + size - 1}"} if start is not None else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers = h), timeout = 120).read()


def file_size(url):
    req = urllib.request.Request(url, method = "HEAD")
    with urllib.request.urlopen(req, timeout = 60) as r:
        return int(r.headers["Content-Length"])


def lines_at(url, offset, size):
    """Complete JSON lines inside [offset, offset + size)"""
    data = fetch(url, offset, size)
    parts = data.split(b"\n")
    if offset > 0:
        parts = parts[1:]
    parts = parts[:-1]
    out = []
    for p in parts:
        try:
            out.append(json.loads(p))
        except Exception:
            pass
    return out


def sample(name, path, n, kind, rng):
    pool = []
    if kind == "gz":
        for i in rng.sample(range(10), 2):
            raw = gzip.decompress(fetch(B + path.format(i = i)))
            pool += [json.loads(l) for l in raw.splitlines() if l.strip()]
    elif kind == "jsonl_multi":
        # very long rows (whole proof traces): one chunk from the start of several files
        for i in rng.sample(range(9), 3):
            pool += lines_at(B + path.format(i = i), 0, 3 * CHUNK)
    else:
        url = B + path
        size = file_size(url)
        for frac in (0.0, 0.3, 0.55, 0.8):
            pool += lines_at(url, int(size * frac), CHUNK)
    rng.shuffle(pool)
    print(f" -- {name}: {len(pool)} candidate rows", flush = True)
    return pool


def render(tok, msgs, tools):
    thinking = any(m.get("reasoning_content") for m in msgs if m.get("role") == "assistant")
    kw = {"preserve_thinking": True, "enable_thinking": thinking}
    ids = tok.apply_chat_template(msgs, tools = tools or None, add_generation_prompt = False, tokenize = True, **kw)
    return ids["input_ids"] if hasattr(ids, "input_ids") else list(ids)


def fit_row(tok, rec, max_row, max_prompt):
    """(input_ids, response_ids) for the longest assistant-terminated prefix within max_row tokens; the prompt is
    everything up to the last assistant turn of the row. Falls back to cropping a single long first turn."""
    msgs = normalize([m for m in rec["messages"] if m.get("role") in ("system", "user", "assistant", "tool")])
    if msgs and msgs[0].get("role") == "system" and not (msgs[0].get("content") or "").strip():
        msgs = msgs[1:]
    tools = rec.get("tools") or None
    ends = [i + 1 for i, m in enumerate(msgs) if m["role"] == "assistant"]
    if not ends:
        return None
    best = None
    for e in ends:
        ids = render(tok, msgs[:e], tools)
        if len(ids) > max_row:
            break
        best = (e, ids)
    if best is None:
        # first assistant turn alone is too long: crop it
        e = ends[0]
        ids = render(tok, msgs[:e], tools)
        prompt = render(tok, msgs[:e - 1], tools) if e > 1 else []
        if len(prompt) > max_prompt:
            return None
        ids = ids[:max_row]
        return ids, len(prompt)
    e, ids = best
    return ids, None


def main(args):
    rng = random.Random(args.seed)
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    rows, report = [], {}
    for name, path, n, kind in SOURCES:
        taken = 0
        for rec in sample(name, path, n, kind, rng):
            if taken >= n:
                break
            got = fit_row(tok, rec, *LIMITS.get(name, (args.max_row, args.max_prompt)))
            if not got:
                continue
            ids, crop_at = got
            spans = assistant_spans(tok, ids)
            if not spans:
                continue
            keep, scored = [], 0
            for s, e in reversed(spans):
                if scored >= args.row_scored:
                    break
                e = min(e, s + args.row_scored - scored)
                keep.insert(0, (s, e))
                scored += e - s
            if scored < 32:
                continue
            # prompt = everything before the first scored span; response = the rest of the row
            cut = keep[0][0]
            rows.append({"conversation": len(rows), "turn": 0, "source": name, "uuid": rec.get("uuid"),
                         "input_ids": ids[:cut], "response_ids": ids[cut:],
                         "score_spans": [[s, e] for s, e in keep]})
            taken += 1
        report[name] = taken
        print(f"    {name}: {taken} rows", flush = True)

    scored = sum(sum(e - s for s, e in r["score_spans"]) for r in rows)
    total = sum(len(r["input_ids"]) + len(r["response_ids"]) for r in rows)
    json.dump({"model": "Nemotron 3.5 Super post-training data (teacher-forced)", "vocab_size": len(tok),
               "meta": {"rows": len(rows), "tokens": total, "scored_tokens": scored, "sources": report},
               "rows": rows}, open(args.output, "w"))
    print(f" -- {len(rows)} rows, {total:,} tokens, {scored:,} scored -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--max_row", type = int, default = 8192)
    ap.add_argument("--max_prompt", type = int, default = 6144)
    ap.add_argument("--row_scored", type = int, default = 500)
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
