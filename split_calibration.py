"""
Split calibration v2 (calib_v2/cal_trace.safetensors, 180 rows, ~1M tokens) into three datasets by purpose:

  agents      agent sessions (MiMo-V2.6-RL-oss tasks run by the Codex / OpenCode / pi harnesses)
  chat        chat conversations (exllamav3 sc_trace prompts, our RU/EN and music prompts), all languages
  multimodal  image prompts (CoSyn figures, frontend screenshots, PixMo photos)

Each gets data.jsonl (one row per calibration row: id, metadata, token count, decoded text with special tokens,
input_ids) and cal_trace.safetensors in exllamav3 --cal_data format. multimodal also gets images/ and the mm_*
tensors; PixMo photos are third-party images, so they are left out and listed by URL and SHA-256 instead (their rows
keep their tokens; the image has to be fetched to rebuild the embeddings).

    python split_calibration.py -o hf/calib_splits
"""
import argparse, hashlib, json, os, re, shutil
import torch
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

Q = os.path.dirname(os.path.abspath(__file__))
MODES = ("xhigh", "medium", "low", "off")


def chat_prompts():
    src = open(os.path.expanduser("~/lm-inference/exllamav3-1.5.3-patched/sc_trace.py")).read()
    ns = {}
    exec(src[src.index("CONVERSATIONS = ["):src.index("\ncol_default")], ns)
    base = ns["CONVERSATIONS"]
    extra = [tuple(x) for x in json.load(open(f"{Q}/trace/extra_conversations.json"))]
    music = [(m["prompt"], []) for m in json.load(open(f"{Q}/trace/music_prompts.json"))]
    kinds = ["sc_trace"] * len(base) + ["ours_ru_en"] * len(extra) + ["ours_music"] * len(music)
    return base + extra + music, kinds


STOPWORDS = {
    "es": {"el", "los", "las", "que", "una", "para", "por", "qué", "cómo", "es"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "wie", "was"},
    "fr": {"le", "les", "des", "une", "est", "pour", "que", "et", "pas", "comment"},
    "it": {"il", "che", "una", "per", "non", "sono", "della", "come"},
    "pt": {"não", "uma", "para", "como", "você", "que", "os"},
}


def lang_of(text):
    """Language of a prompt by script, and by common words for Latin-script languages"""
    for lang, pat in (("ru", "[а-яА-ЯёЁ]"), ("ja", "[぀-ヿ]"), ("zh", "[一-鿿]"), ("ko", "[가-힯]")):
        if re.search(pat, text):
            return lang
    words = set(re.findall(r"[a-záéíóúñüäöß]+", text.lower()))
    for lang, sw in STOPWORDS.items():
        if len(words & sw) >= 3:
            return lang
    return "en"


def main(args):
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    d = load_file(f"{Q}/calib_v2/cal_trace.safetensors")
    keys = json.load(open(f"{Q}/calib_v2/trace_manifest.json"))["cal"]
    ids, lengths = d["input_ids"], d["lengths"]
    mm = {int(r): (int(s), int(n), k) for k, (r, s, n) in enumerate(zip(d["mm_row"], d["mm_start"], d["mm_len"]))}
    convs, conv_kind = chat_prompts()
    items = {os.path.basename(x["file"]): x for x in json.load(open(f"{Q}/trace/mixed/image_items_cal.json"))}
    pixmo = {os.path.basename(x["file"]): x for x in json.load(open(f"{Q}/images/pixmo.json"))}

    subsets = {"agents": [], "chat": [], "multimodal": []}
    for i, key in enumerate(keys):
        row = ids[i, :int(lengths[i])].tolist()
        kind, rest = key.split(":", 1)
        rec = {"id": key, "tokens": len(row)}
        if kind == "agent":
            m = re.match(r"(full-\d+)-(.+)-(codex|opencode|pi)(?:-eff_(off|low|medium|xhigh))?$", rest)
            rec.update({"task": m.group(1), "domain": m.group(2), "harness": m.group(3),
                        "reasoning": m.group(4) or "xhigh"})
            sub = "agents"
        elif kind == "chat":
            src, conv, turn = rest.split(":")
            seed = convs[int(conv)][0]
            rec.update({"reasoning": src.replace("mt_", ""), "multi_turn": src.startswith("mt_") or int(turn) > 0,
                        "prompt_source": conv_kind[int(conv)], "lang": lang_of(seed)})
            sub = "chat"
        else:
            it = items[rest]
            rec.update({"reasoning": it["mode"], "lang": it["lang"], "source": it["source"],
                        "category": it.get("category"), "question": it.get("question") or it.get("prompt")})
            s, n, k = mm[i]
            rec.update({"image_token_start": s, "image_token_count": n})
            data = d[f"mm_image_{k}"].numpy().tobytes()
            if it["source"] == "pixmo":
                p = pixmo[rest]
                rec.update({"image": None, "image_url": p["url"], "image_sha256": hashlib.sha256(data).hexdigest()})
            else:
                rec.update({"image": f"images/{rest}"})
            rec["_bytes"] = data
            sub = "multimodal"
        rec["text"] = tok.decode(row, skip_special_tokens = False)
        rec["input_ids"] = row
        subsets[sub].append(rec)

    for sub, recs in subsets.items():
        out = os.path.join(args.output, sub)
        shutil.rmtree(out, ignore_errors = True)
        os.makedirs(out)
        width = max(r["tokens"] for r in recs)
        t_ids = torch.zeros((len(recs), width), dtype = torch.long)
        t_len = torch.zeros((len(recs),), dtype = torch.long)
        tensors, mm_rows = {}, []
        for j, r in enumerate(recs):
            t_ids[j, :r["tokens"]] = torch.tensor(r["input_ids"])
            t_len[j] = r["tokens"]
            data = r.pop("_bytes", None)
            if data is not None and r.get("image"):
                os.makedirs(os.path.join(out, "images"), exist_ok = True)
                open(os.path.join(out, r["image"]), "wb").write(data)
                mm_rows.append((j, r["image_token_start"], r["image_token_count"], data))
        tensors.update({"input_ids": t_ids, "lengths": t_len})
        if mm_rows:
            tensors["mm_row"] = torch.tensor([x[0] for x in mm_rows], dtype = torch.int32)
            tensors["mm_start"] = torch.tensor([x[1] for x in mm_rows], dtype = torch.int32)
            tensors["mm_len"] = torch.tensor([x[2] for x in mm_rows], dtype = torch.int32)
            for k, x in enumerate(mm_rows):
                tensors[f"mm_image_{k}"] = torch.frombuffer(bytearray(x[3]), dtype = torch.uint8)
        save_file(tensors, os.path.join(out, "cal_trace.safetensors"))
        with open(os.path.join(out, "data.jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii = False) + "\n")
        tok_by_mode = {m: sum(r["tokens"] for r in recs if r["reasoning"] == m) for m in MODES}
        print(f" -- {sub}: {len(recs)} rows, {sum(r['tokens'] for r in recs):,} tokens, by reasoning {tok_by_mode}"
              + (f", {len(mm_rows)} images in safetensors" if sub == "multimodal" else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
