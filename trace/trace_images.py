"""
Calibration rows with images: each item (image file + question + reasoning mode) is answered by the
local EXL3 model with its vision tower, and the exact model-visible stream (templated prompt with
the image embedding block, then the sampled response) is packed as a calibration row.

The prompt is built the way tabbyAPI builds it for an OpenAI request with an image: the image's
text alias inline in the message text, the chat template rendered to text, then tokenized with the
embedding, so the image becomes <|vision_start|>, one id per embedding position, <|vision_end|>.

Output: a trace JSON (rows with input/response ids and image references) and a calibration
safetensors file: "input_ids" (rows, cols) with the image token at embedding positions, "lengths",
and per image "mm_row", "mm_start", "mm_len" and "mm_image_<k>" (the image file bytes), which
convert.py turns back into embeddings with the unquantized vision tower.

    PYTHONPATH=<exllamav3 tree> python trace_images.py -m <model> -mcl 44 --items items.json \\
        -o images_trace.json -co images_cal.safetensors
"""
import argparse, json, os, zlib
import torch
from PIL import Image
from safetensors.torch import save_file
from exllamav3 import Generator, Job, Model, model_init
from exllamav3.generator.sampler import ComboSampler
from exllamav3.tokenizer.mm_embedding import FIRST_MM_EMBEDDING_INDEX

MODES = {
    "off": {"enable_thinking": False},
    "low": {"enable_thinking": True, "reasoning_effort": "low"},
    "medium": {"enable_thinking": True, "reasoning_effort": "medium"},
    "xhigh": {"enable_thinking": True, "reasoning_effort": "xhigh"},
}


@torch.inference_mode()
def main(args):
    model, config, cache, tokenizer = model_init.init(args)[:4]
    config.infer_params.vision_pinned = True
    vision = Model.from_config(config, component = "vision")
    vision.load()
    gen = Generator(model = model, cache = cache, tokenizer = tokenizer, max_chunk_size = 2048)
    sampler = ComboSampler(temperature = 1.0, top_k = 20, top_p = 0.95)
    items = json.load(open(args.items))
    base = os.path.dirname(os.path.abspath(args.items))

    pending = {}
    for i, it in enumerate(items):
        path = it["file"] if os.path.isabs(it["file"]) else os.path.join(base, it["file"])
        mme = vision.get_image_embeddings(tokenizer, Image.open(path).convert("RGB"))
        messages = [{"role": "user", "content": mme.text_alias + it["prompt"]}]
        rendered = tokenizer.hf_render_chat_template(messages, add_generation_prompt = True, **MODES[it["mode"]])
        ids = tokenizer.encode(rendered, encode_special_tokens = True, embeddings = [mme])
        job = Job(input_ids = ids, embeddings = [mme], max_new_tokens = args.max_new_tokens, sampler = sampler,
                  stop_conditions = config.eos_token_id_list, decode_special_tokens = True, identifier = i,
                  seed = zlib.crc32(f"{args.seed}|{i}".encode()))
        gen.enqueue(job)
        pending[i] = {"ids": ids, "chunks": [], "eos": None, "path": path}

    rows = []
    while gen.num_remaining_jobs():
        for r in gen.iterate():
            if r["stage"] != "streaming":
                continue
            p = pending[r["identifier"]]
            if "token_ids" in r:
                p["chunks"].append(r["token_ids"])
            if r["eos"]:
                p["eos"] = r.get("eos_reason")
                n = sum(c.shape[-1] for c in p["chunks"])
                print(f"    item {r['identifier']:3}: {n:5} tokens ({p['eos']})", flush = True)

    for i, it in enumerate(items):
        p = pending[i]
        prompt = p["ids"][0]
        resp = torch.cat(p["chunks"], dim = -1)[0] if p["chunks"] else torch.empty(0, dtype = torch.long)
        mm = (prompt >= FIRST_MM_EMBEDDING_INDEX).nonzero().flatten()
        assert mm.numel() and int(mm[-1] - mm[0]) + 1 == mm.numel(), f"item {i}: image positions not contiguous"
        prompt = prompt.clone()
        prompt[mm] = config.image_token_id
        rows.append({**it, "eos_reason": p["eos"], "input_ids": prompt.tolist(), "response_ids": resp.tolist(),
                     "mm_start": int(mm[0]), "mm_len": mm.numel(), "image_path": p["path"]})

    json.dump({"model": args.model_dir, "vocab_size": tokenizer.actual_vocab_size, "rows": rows},
              open(args.output, "w"))
    if args.cal_out:
        streams = [r["input_ids"] + r["response_ids"] for r in rows]
        width = max(len(s) for s in streams)
        packed = torch.zeros((len(streams), width), dtype = torch.int32)
        for k, s in enumerate(streams):
            packed[k, :len(s)] = torch.tensor(s, dtype = torch.int32)
        tensors = {
            "input_ids": packed,
            "lengths": torch.tensor([len(s) for s in streams], dtype = torch.int32),
            "mm_row": torch.arange(len(rows), dtype = torch.int32),
            "mm_start": torch.tensor([r["mm_start"] for r in rows], dtype = torch.int32),
            "mm_len": torch.tensor([r["mm_len"] for r in rows], dtype = torch.int32),
        }
        for k, r in enumerate(rows):
            tensors[f"mm_image_{k}"] = torch.frombuffer(bytearray(open(r["image_path"], "rb").read()), dtype = torch.uint8)
        save_file(tensors, args.cal_out)
    tot = sum(len(r["input_ids"]) + len(r["response_ids"]) for r in rows)
    print(f" -- {len(rows)} rows, {tot:,} tokens -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 65536)
    ap.add_argument("--items", required = True)
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("-co", "--cal_out", default = None)
    ap.add_argument("--max_new_tokens", type = int, default = 4096)
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
