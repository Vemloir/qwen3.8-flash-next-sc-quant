"""
Multilingual evaluation trace (qbench test_trace with score_spans), no overlap with calibration or the MiMo pool:

  ru   RefalMachine/GrandMaster-PRO-MAX-Qwen3-synthetic: native Russian prompts (Vikhrmodels/GrandMaster-PRO-MAX),
       answers and <think> reasoning in Russian by Qwen3-235B-A22B; rendered with thinking on, reasoning and answer
       scored
  de es fr it ja zh   nvidia/Nemotron-SFT-Multilingual-v1 (math and code)
  hi ko pt ja         nvidia/Nemotron-SFT-Multilingual-v2 (math and code)
       prompts and final answers translated, reasoning left in English: the reasoning is dropped and the rows are
       rendered with thinking off, so only the translated answer is scored

Teacher-forced like nemotron_eval_trace.py (rows sampled by HTTP range requests, nothing downloaded whole).

    python multilingual_eval_trace.py -o eval/multilingual_trace.json
"""
import argparse, json, os, random, re, sys
from transformers import AutoTokenizer

Q = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, Q)
from nemotron_eval_trace import fetch, file_size, lines_at, render, assistant_spans  # noqa: E402

HF = "https://huggingface.co/datasets/"
ML1 = HF + "nvidia/Nemotron-SFT-Multilingual-v1/resolve/main/data/super-v3_{kind}_{lang}_translated_final.jsonl"
ML2 = HF + "nvidia/Nemotron-SFT-Multilingual-v2/resolve/main/ultra-v3_{kind}_{lang}_translated_final.jsonl"
GM = HF + "RefalMachine/GrandMaster-PRO-MAX-Qwen3-synthetic/resolve/main/train.jsonl"


def gm_conversations(rng, n_chunks = 4, chunk = 4_000_000):
    size = file_size(GM)
    out = []
    for frac in [rng.random() for _ in range(n_chunks)]:
        for rec in lines_at(GM, int(size * frac), chunk):
            gen = rec.get("generated") or []
            if not gen or not isinstance(rec.get("inputs"), list):
                continue
            text = gen[0]
            m = re.match(r"\s*<think>(.*?)</think>\s*(.*)", text, re.S)
            if not m or not m.group(2).strip():
                continue
            msgs = [x for x in rec["inputs"] if x.get("role") in ("system", "user", "assistant")]
            msgs.append({"role": "assistant", "reasoning_content": m.group(1).strip(), "content": m.group(2).strip()})
            out.append(msgs)
    rng.shuffle(out)
    return out


def ml_conversations(url, rng, chunk = 3_000_000):
    size = file_size(url)
    out = []
    for frac in (rng.random() * 0.5, 0.5 + rng.random() * 0.5):
        for rec in lines_at(url, int(size * frac), chunk):
            msgs = []
            for m in rec["messages"]:
                if m["role"] not in ("system", "user", "assistant"):
                    continue
                m = {k: v for k, v in m.items() if k in ("role", "content")}
                msgs.append(m)
            if msgs and msgs[-1]["role"] == "assistant" and len(msgs[-1].get("content") or "") >= 300:
                out.append(msgs)
    rng.shuffle(out)
    return out


def make_row(tok, msgs, thinking, max_row, row_scored):
    kw = {"preserve_thinking": True, "enable_thinking": thinking}
    ids = tok.apply_chat_template(msgs, add_generation_prompt = False, tokenize = True, **kw)
    ids = ids["input_ids"] if hasattr(ids, "input_ids") else list(ids)
    ids = ids[:max_row]
    spans = assistant_spans(tok, ids)
    if not spans:
        return None
    s, e = spans[-1]
    if not thinking:
        # skip the empty think block the template emits with thinking off
        body = tok.decode(ids[s:e])
        skip = re.match(r"\s*<think>\s*</think>\s*", body)
        if skip:
            s += len(tok.encode(body[:skip.end()], add_special_tokens = False))
    e = min(e, s + row_scored)
    if e - s < 48:
        return None
    return ids, [(s, e)]


def main(args):
    rng = random.Random(args.seed)
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    rows, report = [], {}

    def add(lang, convs, n, thinking):
        taken = 0
        for msgs in convs:
            if taken >= n:
                break
            got = make_row(tok, msgs, thinking, args.max_row, args.row_scored)
            if not got:
                continue
            ids, spans = got
            cut = spans[0][0]
            rows.append({"conversation": len(rows), "turn": 0, "source": lang, "input_ids": ids[:cut],
                         "response_ids": ids[cut:], "score_spans": [list(x) for x in spans]})
            taken += 1
        report[lang] = report.get(lang, 0) + taken
        print(f"    {lang}: {taken} rows", flush = True)

    add("ru", gm_conversations(rng), args.ru_rows, True)
    for lang in ("de", "es", "fr", "it", "zh"):
        for kind in ("math", "code"):
            add(lang, ml_conversations(ML1.format(kind = kind, lang = lang), rng), args.per_kind, False)
    for lang in ("ja", "ko", "pt", "hi"):
        for kind in ("math", "code"):
            add(lang, ml_conversations(ML2.format(kind = kind, lang = lang), rng), args.per_kind, False)

    scored = sum(sum(e - s for s, e in r["score_spans"]) for r in rows)
    total = sum(len(r["input_ids"]) + len(r["response_ids"]) for r in rows)
    json.dump({"model": "multilingual teacher-forced trace (GrandMaster Qwen3 ru, Nemotron-SFT-Multilingual)",
               "vocab_size": len(tok),
               "meta": {"rows": len(rows), "tokens": total, "scored_tokens": scored, "sources": report},
               "rows": rows}, open(args.output, "w"))
    print(f" -- {len(rows)} rows, {total:,} tokens, {scored:,} scored -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--max_row", type = int, default = 8192)
    ap.add_argument("--row_scored", type = int, default = 500)
    ap.add_argument("--ru_rows", type = int, default = 16)
    ap.add_argument("--per_kind", type = int, default = 2)
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
