"""
The published data of calibration set 2, in the layout of split_calibration.py (set 1):

  --set calib  the rows that went into calibration (calib_v3/, 2.15M tokens), split into agents / chat / multimodal
  --set full   every row built from the regenerated records (regen/out/*_rows.json, 5.0M tokens), same split

Each part gets data.jsonl (one line per row: id, metadata, token count, chat turns where they exist, decoded text
with special tokens, input_ids) and cal_trace.safetensors in exllamav3 --cal_data format; multimodal also gets
images/ and the mm_* tensors. PixMo photos are third-party images: they are left out and listed by URL and SHA-256.
Calibration rows are checked token for token against the row files they were packed from.

    python regen/split_data.py --set calib -o hf/calib_splits2
    python regen/split_data.py --set full  -o hf/traces_full
"""
import argparse, hashlib, json, os, re, shutil, sys
import torch
from safetensors.torch import load_file, save_file

HERE = os.path.dirname(os.path.abspath(__file__))
Q = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
MODES = ("xhigh", "medium", "low", "off")
STOPWORDS = {
    "es": {"el", "los", "las", "que", "una", "para", "por", "qué", "cómo", "es"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "wie", "was"},
    "fr": {"le", "les", "des", "une", "est", "pour", "que", "et", "pas", "comment"},
    "it": {"il", "che", "una", "per", "non", "sono", "della", "come"},
    "pt": {"não", "uma", "para", "como", "você", "que", "os"},
}


def lang_of(text):
    """Language of a prompt by script and distinctive letters, then by common words for Latin-script languages"""
    for lang, pat in (("uk", "[іїєґІЇЄҐ]"), ("ru", "[а-яА-ЯёЁ]"), ("ja", "[぀-ヿ]"), ("zh", "[一-鿿]"),
                      ("ko", "[가-힯]"), ("hi", "[ऀ-ॿ]"), ("ar", "[؀-ۿ]"),
                      ("vi", "[ạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹđĐ]"),
                      ("tr", "[şğıŞĞİ]"), ("pl", "[ąęłśżźćńĄĘŁŚŻŹĆŃ]")):
        if re.search(pat, text):
            return lang
    words = set(re.findall(r"[a-záéíóúñüäöß]+", text.lower()))
    for lang, sw in STOPWORDS.items():
        if len(words & sw) >= 3:
            return lang
    return "en"


# prompts_science_ml.json holds three conversations per language, in this order
ML_LANGS = ("es", "ja", "zh", "de", "fr", "pt", "ko", "hi", "it", "tr", "pl", "uk", "ar", "vi")


def load_rows():
    """key -> (part, row record) for every built row, with the chat records' turns and the image items"""
    chats = {json.loads(l)["key"]: json.loads(l) for l in open(os.path.join(OUT, "chat_final.jsonl"))}
    images = {json.loads(l)["key"]: json.loads(l) for l in open(os.path.join(OUT, "images_final.jsonl"))}
    ml = json.load(open(os.path.join(HERE, "prompts_science_ml.json")))
    ml_lang = {conv[0]: ML_LANGS[i // 3] for i, conv in enumerate(ml)}
    rows = {}
    for r in json.load(open(os.path.join(OUT, "agent_rows.json"))):
        m = re.match(r"(r2-\d+)-(.+)-(codex|opencode|pi)-eff_(off|low|medium|xhigh)$", r["session"])
        rows[f"agent:{r['session']}"] = ("agents", r["ids"], {
            "task": m.group(1), "domain": m.group(2), "harness": m.group(3), "reasoning": m.group(4),
            "requests": r["requests"]})
    for r in json.load(open(os.path.join(OUT, "chat_rows.json"))):
        c = chats[r["key"][len("chat:"):]]
        turns = [{"user": t["user"], "reasoning": t["reasoning"], "answer": t["content"]} for t in c["turns"]]
        rows[r["key"]] = ("chat", r["ids"], {
            "reasoning": r["mode"], "prompt_source": r["source"], "lang": ml_lang.get(turns[0]["user"]) or lang_of(turns[0]["user"]),
            "multi_turn": len(turns) > 1, "qa": r["qa"], "turns": turns})
    for r in json.load(open(os.path.join(OUT, "image_rows.json"))):
        it = images[r["key"][len("image:"):]]
        rows[r["key"]] = ("multimodal", r["ids"], {
            "reasoning": r["mode"], "lang": r["lang"], "source": r["source"], "category": it.get("category"),
            "question": it.get("question") or it.get("prompt"), "qa": r["qa"],
            "image_token_start": r["mm_start"], "image_token_count": r["mm_len"], "_file": r["file"],
            "turns": [{"user": it["prompt"], "reasoning": it["reasoning"], "answer": it["content"]}]})
    return rows


def main(args):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    rows = load_rows()
    pixmo = {os.path.basename(x["file"]): x for x in json.load(open(os.path.join(Q, "images", "pixmo.json")))}

    if args.set == "calib":
        d = load_file(os.path.join(Q, "calib_v3", "cal_trace.safetensors"))
        man = json.load(open(os.path.join(Q, "calib_v3", "trace_manifest.json")))
        keys, share = man["cal"], man["agents"]
        mm_k = {int(r): k for k, r in enumerate(d["mm_row"])}
        for i, key in enumerate(keys):
            packed = d["input_ids"][i, :int(d["lengths"][i])].tolist()
            assert packed == list(rows[key][1]), f"{key}: packed row differs from its row file"
            if i in mm_k:
                assert int(d["mm_start"][mm_k[i]]) == rows[key][2]["image_token_start"], f"{key}: image offset differs"
        selected = keys
    else:
        share = {}
        order = {"agents": 0, "chat": 1, "multimodal": 2}
        selected = sorted(rows, key = lambda k: (order[rows[k][0]], k))

    subsets = {"agents": [], "chat": [], "multimodal": []}
    for key in selected:
        part, ids, meta = rows[key]
        ids = list(ids)
        rec = {"id": key, "tokens": len(ids), **{k: v for k, v in meta.items() if k not in ("turns", "_file")}}
        if key in share:
            rec["model_text_share"] = share[key]
        if part == "multimodal":
            path = meta["_file"]
            data = open(path, "rb").read()
            name = os.path.basename(path)
            if meta["source"] == "pixmo":
                rec.update({"image": None, "image_url": pixmo[name]["url"],
                            "image_sha256": hashlib.sha256(data).hexdigest()})
            else:
                rec["image"] = f"images/{name}"
            rec["_bytes"] = data
        if "turns" in meta:
            rec["turns"] = meta["turns"]
        rec["text"] = tok.decode(ids, skip_special_tokens = False)
        rec["input_ids"] = ids
        subsets[part].append(rec)

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
            # PixMo photos are not redistributed: their rows keep the tokens, the image comes from its URL
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
        n_by_mode = {m: sum(r["reasoning"] == m for r in recs) for m in MODES}
        langs = {}
        for r in recs:
            if "lang" in r:
                langs[r["lang"]] = langs.get(r["lang"], 0) + 1
        print(f" -- {sub}: {len(recs)} rows, {sum(r['tokens'] for r in recs):,} tokens, rows by reasoning {n_by_mode}, "
              f"tokens {tok_by_mode}, langs {dict(sorted(langs.items(), key = lambda x: -x[1]))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices = ("calib", "full"), required = True)
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/SC2-EXL3-4.05"))
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
