"""
Token rows with images from the regenerated image answers (gen_images.py output), laid out the
way exllamav3 lays out a prompt with an image: the image's text alias inline before the question,
the chat template rendered to text and tokenized with the embedding, so the image becomes one
token per embedding position. Those positions are written as the image token; convert.py
(patched) re-embeds the image bytes stored with the row through the unquantized vision tower.

The image block length comes from exllamav3's preprocessing and is checked against the server's
image token count for the same request (both follow the model's preprocessor config).

    PYTHONPATH=<exllamav3 tree> python regen/build_image_rows.py --images regen/out/images_final.jsonl \
        --qa regen/out/qa.json -m <model_dir> -o regen/out/image_rows.json
"""
import argparse, base64, json, os, sys
import torch
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODES


@torch.inference_mode()
def main(args):
    from exllamav3 import Config, Model, Tokenizer
    from exllamav3.tokenizer.mm_embedding import FIRST_MM_EMBEDDING_INDEX
    config = Config.from_directory(args.model_dir)
    tokenizer = Tokenizer.from_config(config)
    vision = Model.from_config(config, component = "vision")
    vision.load(device = torch.device("cuda", 0))
    verdict = {e["key"]: e["verdict"] for e in json.load(open(args.qa)) if e["kind"] == "image"}
    accept = set(args.accept.split(",")) if args.accept else set()
    rows, mismatch, skipped = [], 0, 0
    for line in open(args.images):
        r = json.loads(line)
        v = "ok" if r["key"] in accept else verdict.get(r["key"], "missing")
        if r["status"] != "ok" or v == "reject":
            skipped += 1
            continue
        mme = vision.get_image_embeddings(tokenizer, Image.open(r["file"]).convert("RGB"))
        messages = [{"role": "user", "content": mme.text_alias + r["prompt"]}]
        tvars = MODES[r["mode"]]
        prompt_text = tokenizer.hf_render_chat_template(messages, add_generation_prompt = True, **tvars)
        full_text = tokenizer.hf_render_chat_template(
            messages + [{"role": "assistant", "content": r["content"], "reasoning_content": r["reasoning"]}],
            add_generation_prompt = False, **tvars)
        assert full_text.startswith(prompt_text), f"{r['key']}: rendered answer does not extend the prompt"
        prompt = tokenizer.encode(prompt_text, encode_special_tokens = True, embeddings = [mme])[0]
        full = tokenizer.encode(full_text, encode_special_tokens = True, embeddings = [mme])[0]
        mm = (full >= FIRST_MM_EMBEDDING_INDEX).nonzero().flatten()
        assert mm.numel() == mme.mm_length and int(mm[-1] - mm[0]) + 1 == mm.numel(), f"{r['key']}: image block"
        full = full.clone()
        full[mm] = config.image_token_id
        served = ((r.get("usage") or {}).get("prompt_tokens_details") or {}).get("multimodal_tokens", {}).get("image")
        if served and served != mme.mm_length:
            mismatch += 1
            print(f" !! {r['key']}: {mme.mm_length} image positions here, server used {served}")
        rows.append({"key": f"image:{r['key']}", "mode": r["mode"], "source": r["source"], "lang": r["lang"],
                     "qa": v, "ids": full.tolist(), "n_prompt": int(prompt.shape[-1]),
                     "mm_start": int(mm[0]), "mm_len": int(mm.numel()), "file": r["file"]})
    vision.unload()
    json.dump(rows, open(args.out, "w"))
    print(f" -- {len(rows)} rows, {sum(len(r['ids']) for r in rows):,} tokens, skipped {skipped}, "
          f"{mismatch} image length mismatches -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required = True)
    ap.add_argument("--qa", required = True)
    ap.add_argument("-m", "--model_dir", required = True)
    ap.add_argument("--accept", default = "")
    ap.add_argument("-o", "--out", required = True)
    main(ap.parse_args())
