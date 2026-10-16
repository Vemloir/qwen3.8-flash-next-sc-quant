"""
Agent evaluation trace (qbench test_trace with score_spans) from agent sessions that calibration v2
did not use: per session, the latest logged request whose rendered prompt fits --max_ctx tokens,
with the response the model gave there and the whole row within --max_row tokens, scored on the
assistant turns inside the row (the earlier turns of the session are in the prompt): the first
--span_scored tokens of each turn (where the agent decides what to call or say), latest turns first,
up to --row_scored tokens per row.

    python agent_eval_trace.py --manifest calib_v2/trace_manifest.json -o eval/agent_trace.json
"""
import argparse, glob, json, os, sys
from transformers import AutoTokenizer

Q = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(Q, "agent-env"))
from build_rows import reassemble, normalize  # noqa: E402


def latest_fitting(tok, log, max_ctx, max_row):
    """Prompt and response ids of the latest request with a prompt within max_ctx tokens and the
    whole row within max_row tokens."""
    best = None
    for line in open(log):
        rec = json.loads(line)
        req = rec.get("request")
        if rec.get("status") != 200 or not req or not rec["path"].rstrip("/").endswith("chat/completions"):
            continue
        reply = reassemble(rec["response_raw"])
        if not (reply["content"] or reply["reasoning_content"] or reply["tool_calls"]):
            continue
        tvars = {"preserve_thinking": True}
        for k in ("reasoning_effort", "enable_thinking"):
            if req.get(k) is not None:
                tvars[k] = req[k]
        msgs = normalize(req["messages"])
        p = tok.apply_chat_template(msgs, tools = req.get("tools"), add_generation_prompt = True, tokenize = True, **tvars)
        p = p["input_ids"] if hasattr(p, "input_ids") else p
        if len(p) > max_ctx:
            break
        f = tok.apply_chat_template(msgs + normalize([reply]), tools = req.get("tools"),
                                    add_generation_prompt = False, tokenize = True, **tvars)
        f = f["input_ids"] if hasattr(f, "input_ids") else f
        if len(p) < len(f) <= max_row and f[:len(p)] == p:
            best = (list(p), list(f[len(p):]))
    return best


def assistant_spans(tok, ids):
    """[s, e) token spans of every assistant turn body (after the role header, through <|im_end|>)."""
    start, end = tok.convert_tokens_to_ids("<|im_start|>"), tok.convert_tokens_to_ids("<|im_end|>")
    header = tok.encode("assistant\n", add_special_tokens = False)
    spans, i = [], 0
    while i < len(ids):
        if ids[i] == start and ids[i + 1:i + 1 + len(header)] == header:
            s = i + 1 + len(header)
            e = s
            while e < len(ids) and ids[e] != end:
                e += 1
            spans.append((s, min(e + 1, len(ids))))
            i = e
        i += 1
    return spans


def main(args):
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    man = json.load(open(args.manifest))
    excluded = set(man.get("excluded", []))
    sessions = {"xhigh": sorted(glob.glob(f"{Q}/agent-runs/logs/trace-*.jsonl"))}
    for mode, held in man["held_out"].items():
        for k in held["agent"]:
            name = k.split(":", 1)[1]
            if name in excluded:
                continue
            log = f"{Q}/agent-runs/logs/{name}.jsonl" if mode == "xhigh" else f"{Q}/agent-runs-mixed/logs/{name}.jsonl"
            sessions.setdefault(mode, []).append(log)

    rows = []
    for mode, logs in sessions.items():
        for log in logs:
            got = latest_fitting(tok, log, args.max_ctx, args.max_row)
            if not got:
                continue
            p, r = got
            ids = p + r
            spans = assistant_spans(tok, ids)
            keep, n = [], 0
            for s, e in reversed(spans):
                if n >= args.row_scored:
                    break
                e = min(e, s + args.span_scored, s + args.row_scored - n)
                keep.insert(0, (s, e))
                n += e - s
            if not keep:
                continue
            rows.append({"conversation": len(rows), "turn": 0, "tools": True, "mode": mode,
                         "key": "agent:" + os.path.basename(log)[:-6], "input_ids": p, "response_ids": r,
                         "score_spans": [list(x) for x in keep]})

    scored = sum(sum(e - s for s, e in r["score_spans"]) for r in rows)
    total = sum(len(r["input_ids"]) + len(r["response_ids"]) for r in rows)
    json.dump({"model": "agent sessions held out from calibration v2", "vocab_size": len(tok),
               "meta": {"rows": len(rows), "tokens": total, "scored_tokens": scored}, "rows": rows},
              open(args.output, "w"))
    for mode in ("xhigh", "medium", "low", "off"):
        rs = [r for r in rows if r["mode"] == mode]
        print(f"    {mode:6}: {len(rs):2} sessions, {sum(sum(e - s for s, e in r['score_spans']) for r in rs):6,} scored tokens")
    print(f" -- {len(rows)} rows, {total:,} tokens, {scored:,} scored -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required = True)
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--max_ctx", type = int, default = 10240)
    ap.add_argument("--max_row", type = int, default = 10240)
    ap.add_argument("--span_scored", type = int, default = 250)
    ap.add_argument("--row_scored", type = int, default = 1500)
    main(ap.parse_args())
