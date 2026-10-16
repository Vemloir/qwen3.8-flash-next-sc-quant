"""
Continue sc_trace responses that stopped at --max_new_tokens, to their natural end.

sc_trace.py caps each turn at 3072 new tokens by default; with reasoning_effort xhigh most
Flash-Next answers are longer, so those rows end mid-reasoning. Each truncated row's exact token
stream (templated context + response so far) is fed back as the prompt and generation continues
until a stop token or until the row reaches --max_total tokens (the calibration row width; longer
is cut at packing anyway). Sampling uses the same sampler as sc_trace (exllamav3 DefaultSampler),
so a row stays one distribution. Progress is saved every --save_every finished rows and the run
resumes from the output file.

    PYTHONPATH=<exllamav3 tree> python continue_trace.py -m <model> -mcl 44 -cs 65536 -ambs 8 \\
        --trace trace/chat_trace.json -o trace/chat_trace_full.json
"""
import argparse, json, os, sys, time, zlib
import torch

from exllamav3 import Generator, Job, model_init
from exllamav3.constants import PAGE_SIZE


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main(args):
    # a job needs one cache page beyond its last token, so a row can use at most cache_size - PAGE_SIZE
    args.max_total = min(args.max_total, args.cache_size - PAGE_SIZE)
    src = json.load(open(args.output if os.path.exists(args.output) else args.trace))
    rows = src["rows"]
    only = set(json.load(open(args.rows))["continue"]) if args.rows else None
    todo = [i for i, r in enumerate(rows)
            if (only is None or i in only)
            and r.get("eos_reason") == "max_new_tokens" and not r.get("continued_done")
            and args.max_total - len(r["input_ids"]) - len(r["response_ids"]) >= 64]
    if only is not None:
        # enqueue in the selection's order: it lists rows grouped by language/domain, so the jobs that
        # share a batch tend to route to the same experts (cheaper CPU-MoE decode steps)
        rank = {i: k for k, i in enumerate(json.load(open(args.rows))["continue"])}
        todo.sort(key = lambda i: rank[i])
    print(f" -- {len(rows)} rows, {len(todo)} to continue (cap {args.max_total} tokens per row)", flush = True)
    if not todo:
        return

    model, config, cache, tokenizer = model_init.init(args)[:4]
    generator = Generator(model = model, cache = cache, tokenizer = tokenizer, max_chunk_size = 2048)

    for i in todo:
        r = rows[i]
        ids = torch.tensor([r["input_ids"] + r["response_ids"]], dtype = torch.long)
        generator.enqueue(Job(
            input_ids = ids,
            max_new_tokens = args.max_total - ids.shape[-1],
            stop_conditions = config.eos_token_id_list,
            decode_special_tokens = True,
            identifier = i,
            seed = zlib.crc32(f"continue|{args.seed}|{i}".encode()),
        ))

    pending = {i: [] for i in todo}
    finished = 0
    new_total = 0
    t0 = time.time()
    deadline = time.mktime(time.strptime(args.until, "%Y-%m-%d %H:%M")) if args.until else None
    while generator.num_remaining_jobs():
        if deadline and time.time() > deadline:
            # rows still in flight keep their original (truncated) state and resume next run
            print(f" -- deadline {args.until} reached, stopping", flush = True)
            break
        for res in generator.iterate():
            if res["stage"] != "streaming":
                continue
            i = res["identifier"]
            if "token_ids" in res:
                pending[i].append(res["token_ids"])
            if res["eos"]:
                new = torch.cat(pending.pop(i), dim = -1)[0].tolist() if pending[i] else []
                r = rows[i]
                r["response_ids"] = r["response_ids"] + new
                r["eos_reason"] = res.get("eos_reason")
                r["continued_tokens"] = len(new)
                r["continued_done"] = True
                finished += 1
                new_total += len(new)
                dt = time.time() - t0
                print(f"    row {i:3}: +{len(new):5} tokens ({r['eos_reason']}), "
                      f"{finished}/{len(todo)} done, {new_total / dt:.1f} tok/s", flush = True)
                if finished % args.save_every == 0:
                    save(args.output, src)
    save(args.output, src)
    print(f" -- done: {finished} rows continued, {new_total:,} new tokens in {(time.time() - t0) / 60:.1f} min", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 65536, default_autosplit_max_batch_size = 8)
    ap.add_argument("--trace", required = True, help = "sc_trace JSON")
    ap.add_argument("-o", "--output", required = True, help = "output JSON (resumed if present)")
    ap.add_argument("--max_total", type = int, default = 16384, help = "max tokens per row (context + response)")
    ap.add_argument("--save_every", type = int, default = 10)
    ap.add_argument("--seed", type = int, default = 0)
    ap.add_argument("--rows", default = None, help = "selection JSON; only its \"continue\" row indices are continued")
    ap.add_argument("--until", default = None, help = "stop (saving progress) at this local time, \"YYYY-MM-DD HH:MM\"")
    main(ap.parse_args())
