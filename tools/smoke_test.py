"""
Load an EXL3 model with CPU-offloaded experts and answer a few prompts, to check a fresh quant
loads and produces coherent text before running the real evaluation.

    PYTHONPATH=<exllamav3 tree> python smoke_test.py -m <model> -mcl 44 -cs 16384
"""
import argparse, time
import torch
from transformers import AutoTokenizer
from exllamav3 import Generator, Job, model_init
from exllamav3.generator.sampler import ArgmaxSampler

PROMPTS = [
    "Объясни коротко, почему небо голубое.",
    "Write a Python function that checks whether a string is a palindrome, ignoring case and punctuation.",
    "Напиши три предложения о зиме в Сибири.",
]


def main(args):
    model, config, cache, tokenizer = model_init.init(args)[:4]
    hf_tok = AutoTokenizer.from_pretrained(args.model_dir)
    gen = Generator(model = model, cache = cache, tokenizer = tokenizer)
    for p in PROMPTS:
        ids = hf_tok.apply_chat_template([{"role": "user", "content": p}], add_generation_prompt = True,
                                         tokenize = True, enable_thinking = False)
        ids = torch.tensor([ids["input_ids"] if hasattr(ids, "input_ids") else ids])
        t0 = time.time()
        job = Job(input_ids = ids, max_new_tokens = args.max_new, sampler = ArgmaxSampler(),
                  stop_conditions = config.eos_token_id_list, decode_special_tokens = False)
        gen.enqueue(job)
        text, n = "", 0
        while gen.num_remaining_jobs():
            for r in gen.iterate():
                if r.get("stage") == "streaming":
                    text += r.get("text", "")
                    n += len(r["token_ids"][0]) if "token_ids" in r else 0
        dt = time.time() - t0
        print(f"\n=== {p}\n{text.strip()}\n--- {n} tokens, {n / dt:.1f} tok/s", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 16384)
    ap.add_argument("--max_new", type = int, default = 300)
    main(ap.parse_args())
