"""
Side-by-side check of a quant on a few hard prompts (JSON list of {id, text}) with thinking enabled.
Saves the full output, thinking included, to a markdown file.

    PYTHONPATH=<exllamav3 tree> python reason_test.py -m <model> -mcl 44 -cs 65536 --prompts p.json --out out.md
"""
import argparse, json, time
import torch
from transformers import AutoTokenizer
from exllamav3 import Generator, Job, model_init
from exllamav3.generator.sampler import ComboSampler


def main(args):
    model, config, cache, tokenizer = model_init.init(args)[:4]
    hf_tok = AutoTokenizer.from_pretrained(args.model_dir)
    gen = Generator(model = model, cache = cache, tokenizer = tokenizer)
    sampler = ComboSampler(temperature = 1.0, top_k = 20, top_p = 0.95)
    out = open(args.out, "w")
    out.write(f"# {args.model_dir}\n\nreasoning_effort {args.effort}, temperature 1.0, top_k 20, "
              f"top_p 0.95, seed {args.seed}\n")
    prompts = json.load(open(args.prompts))
    for i, item in enumerate(prompts):
        p = item["text"]
        ids = hf_tok.apply_chat_template([{"role": "user", "content": p}], add_generation_prompt = True,
                                         tokenize = True, enable_thinking = True,
                                         reasoning_effort = args.effort)
        ids = torch.tensor([ids["input_ids"] if hasattr(ids, "input_ids") else ids])
        t0 = time.time()
        job = Job(input_ids = ids, max_new_tokens = args.max_new, sampler = sampler, seed = args.seed + i,
                  stop_conditions = config.eos_token_id_list, decode_special_tokens = True)
        gen.enqueue(job)
        text, n = "", 0
        while gen.num_remaining_jobs():
            for r in gen.iterate():
                if r.get("stage") == "streaming":
                    text += r.get("text", "")
                    n += len(r["token_ids"][0]) if "token_ids" in r else 0
        dt = time.time() - t0
        out.write(f"\n\n## Задача {i + 1} ({item['id']})\n\n{p}\n\n"
                  f"### Ответ ({n} токенов, {n / dt:.1f} tok/s)\n\n{text.strip()}\n")
        out.flush()
        print(f" -- prompt {i + 1}: {n} tokens in {dt:.0f} s", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 65536)
    ap.add_argument("--max_new", type = int, default = 48000)
    ap.add_argument("--effort", default = "xhigh")
    ap.add_argument("--seed", type = int, default = 1234)
    ap.add_argument("--prompts", required = True)
    ap.add_argument("--out", required = True)
    main(ap.parse_args())
