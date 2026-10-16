"""
Held-out evaluation trace (qbench test_trace format) from the parts of the trace pool that calibration
v2 did not use, per reasoning mode:

  chat   held-out conversations (Russian ones included), one (context, response) turn each
  agent  up to --steps agent steps per held-out session, spread over the requests whose prompt fits
         --agent_ctx tokens, each with the response the model gave there (xhigh: the capped trace-*
         sessions, never used for calibration; other modes: the mixed-mode sessions left out of v2)

KLD is scored on the response tokens only, as in qbench. Rows longer than --max_row are skipped.

    python eval_trace.py --manifest calib_v2/trace_manifest.json -o eval/heldout_trace.json
"""
import argparse, glob, json, os, random, re, sys
from transformers import AutoTokenizer

Q = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(Q, "agent-env"))
from build_rows import reassemble, normalize  # noqa: E402

MODES = ["xhigh", "medium", "low", "off"]


def chat_rows(keys):
    src = {"xhigh": f"{Q}/trace/chat_trace_full.json"}
    for m in ("medium", "low", "off"):
        src[m] = f"{Q}/trace/mixed/chat_{m}.json"
        src[f"mt_{m}"] = f"{Q}/trace/mixed/chat_mt_{m}.json"
    split = json.load(open(f"{Q}/trace/mixed/multiturn_split.json"))
    cache, out = {}, []
    for k in keys:
        _, s, conv, turn = k.split(":")
        rows = cache.setdefault(s, json.load(open(src[s]))["rows"])
        for r in rows:
            c = split[s[3:]][r["conversation"]] if s.startswith("mt_") else r["conversation"]
            if c == int(conv) and r["turn"] == int(turn):
                out.append({"key": k, "input_ids": r["input_ids"], "response_ids": r["response_ids"]})
                break
    return out


def agent_steps(tok, log, agent_ctx, steps):
    """Up to `steps` logged requests (evenly spread) with a prompt within agent_ctx tokens and a non-empty response."""
    found = []
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
        f = tok.apply_chat_template(msgs + normalize([reply]), tools = req.get("tools"), add_generation_prompt = False,
                                    tokenize = True, **tvars)
        if hasattr(p, "input_ids"):
            p, f = p["input_ids"], f["input_ids"]
        if len(p) > agent_ctx:
            break
        if len(f) > len(p) and f[:len(p)] == p:
            found.append({"input_ids": list(p), "response_ids": list(f[len(p):])})
    if len(found) <= steps:
        return found
    return [found[round(i * (len(found) - 1) / (steps - 1))] for i in range(steps)]


def main(args):
    rng = random.Random(args.seed)
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    man = json.load(open(args.manifest))
    rows = []
    for m in MODES:
        held = man["held_out"][m]
        chat = chat_rows(held["chat"])
        chat = [r for r in chat if len(r["input_ids"]) + len(r["response_ids"]) <= args.max_row]
        ru = [r for r in chat if re.search("[а-яА-ЯёЁ]", tok.decode(r["input_ids"][:300]))]
        rng.shuffle(ru)
        rest = [r for r in chat if r not in ru]
        rng.shuffle(rest)
        pick = ru[:args.chat_ru] + rest[:args.chat - min(args.chat_ru, len(ru))]
        rows += [dict(r, mode = m, src = "chat", lang = "ru" if r in ru else "en") for r in pick]

        if m == "xhigh":
            logs = sorted(glob.glob(f"{Q}/agent-runs/logs/trace-*.jsonl"))
        else:
            logs = [f"{Q}/agent-runs-mixed/logs/{k.split(':', 1)[1]}.jsonl" for k in held["agent"]]
        rng.shuffle(logs)
        n = 0
        for log in logs:
            if n >= args.agent:
                break
            got = [s for s in agent_steps(tok, log, args.agent_ctx, args.steps)
                   if len(s["input_ids"]) + len(s["response_ids"]) <= args.max_row]
            rows += [dict(s, key = f"agent:{os.path.basename(log)[:-6]}:{j}", mode = m, src = "agent")
                     for j, s in enumerate(got)]
            n += bool(got)

    resp = sum(len(r["response_ids"]) for r in rows)
    ctx = sum(len(r["input_ids"]) for r in rows)
    out = {"model": "trace pool (held out from calibration v2)", "vocab_size": len(tok),
           "meta": {"rows": len(rows), "input_tokens": ctx, "output_tokens": resp},
           "rows": [{"conversation": i, "turn": 0, "tools": r["src"] == "agent", "key": r["key"], "mode": r["mode"],
                     "src": r["src"], "input_ids": r["input_ids"], "response_ids": r["response_ids"]}
                    for i, r in enumerate(rows)]}
    json.dump(out, open(args.output, "w"))
    by = {}
    for r in rows:
        by.setdefault((r["mode"], r["src"]), [0, 0])
        by[(r["mode"], r["src"])][0] += 1
        by[(r["mode"], r["src"])][1] += len(r["response_ids"])
    for k in sorted(by):
        print(f"    {k[0]:6} {k[1]:5}: {by[k][0]:2} rows, {by[k][1]:6,} scored tokens")
    print(f" -- {len(rows)} rows, {ctx:,} context + {resp:,} scored tokens -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required = True)
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--chat", type = int, default = 3)
    ap.add_argument("--chat_ru", type = int, default = 1)
    ap.add_argument("--agent", type = int, default = 3, help = "sessions per mode")
    ap.add_argument("--steps", type = int, default = 3, help = "steps per session")
    ap.add_argument("--agent_ctx", type = int, default = 8192)
    ap.add_argument("--max_row", type = int, default = 8192)
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
