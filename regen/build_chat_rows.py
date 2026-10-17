"""
Token rows from the regenerated chats (gen_chat.py output): each conversation rendered with the
model's chat template exactly as the server rendered its last request (history answers without
their reasoning, the reasoning mode in the template variables), then the last answer with its
reasoning. The prompt length is checked against the server's prompt_tokens for that request.

    python regen/build_chat_rows.py --chat regen/out/chat_final.jsonl --qa regen/out/qa.json \
        --tokenizer <model_dir> -o regen/out/chat_rows.json
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODES


def ids_of(x):
    return list(x["input_ids"] if hasattr(x, "input_ids") or isinstance(x, dict) else x)


def main(args):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    verdict = {e["key"]: e["verdict"] for e in json.load(open(args.qa)) if e["kind"] == "chat"}
    accept = set(args.accept.split(",")) if args.accept else set()
    reject = set(args.reject.split(",")) if args.reject else set()
    rows, mismatch, skipped = [], 0, {}
    for line in open(args.chat):
        r = json.loads(line)
        v = "reject" if r["key"] in reject else "ok" if r["key"] in accept else verdict.get(r["key"], "missing")
        if r["status"] != "ok" or v == "reject":
            skipped[v if r["status"] == "ok" else r["status"]] = skipped.get(v if r["status"] == "ok" else r["status"], 0) + 1
            continue
        msgs = []
        for t in r["turns"][:-1]:
            msgs += [{"role": "user", "content": t["user"]}, {"role": "assistant", "content": t["content"]}]
        last = r["turns"][-1]
        msgs.append({"role": "user", "content": last["user"]})
        tvars = MODES[r["mode"]]
        prompt = ids_of(tok.apply_chat_template(msgs, add_generation_prompt = True, tokenize = True, **tvars))
        full = ids_of(tok.apply_chat_template(
            msgs + [{"role": "assistant", "content": last["content"], "reasoning_content": last["reasoning"]}],
            add_generation_prompt = False, tokenize = True, **tvars))
        assert full[:len(prompt)] == prompt, f"{r['key']}: rendered answer does not extend the prompt"
        served = (last.get("usage") or {}).get("prompt_tokens")
        if served and served != len(prompt):
            mismatch += 1
            print(f" !! {r['key']}: prompt {len(prompt)} tokens here, server counted {served}")
        rows.append({"key": f"chat:{r['key']}", "conv": r["conv"], "mode": r["mode"], "source": r["source"],
                     "lang": r["lang"], "qa": v, "ids": full, "n_prompt": len(prompt)})
    json.dump(rows, open(args.out, "w"))
    print(f" -- {len(rows)} rows, {sum(len(r['ids']) for r in rows):,} tokens, skipped {skipped}, "
          f"{mismatch} prompt length mismatches -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required = True)
    ap.add_argument("--qa", required = True)
    ap.add_argument("--tokenizer", required = True)
    ap.add_argument("--accept", default = "", help = "rejected keys accepted by hand, comma-separated")
    ap.add_argument("--reject", default = "", help = "keys rejected by hand (e.g. language slips), comma-separated")
    ap.add_argument("-o", "--out", required = True)
    main(ap.parse_args())
