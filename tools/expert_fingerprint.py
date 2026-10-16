"""
Router fingerprints of trace rows, to batch rows that route to the same experts.

With the experts in system RAM (CPU MoE), a batched decode step costs roughly the number of distinct
experts it touches per layer: two sequences that pick the same experts read their weights once. This
prefills each selected row (prompt + the response it already has), records the experts the router
picks for every token in every MoE layer, and estimates for each pair of rows the per-layer union
of two random decode steps, |A u B| / top_k: 1.0 means the batch of two costs one step, 2.0 means no
sharing at all. The same statistic within one row (two of its own positions) is the ceiling; rows
from unrelated groups are the floor.

    PYTHONPATH=<exllamav3 tree> python expert_fingerprint.py -m <model> -mcl 44 -cs 65536 \\
        --trace trace/chat_trace_full.json --rows trace/chat_selection.json -o trace/expert_fp.pt
"""
import argparse, json, random, sys
import torch

from exllamav3 import Generator, Job, model_init
from exllamav3.modules.block_sparse_mlp import BlockSparseMLP

CUR = {"row": None}
REC = {}     # row -> {layer_idx: [tensor(tokens, top_k)]}


def hook(module, layer_idx):
    fn = module.routing_fn
    def wrapped(bsz, cfg, z, params):
        sel, w = fn(bsz, cfg, z, params)
        if CUR["row"] is not None and bsz > 1:      # prefill chunks only, not the closing decode step
            REC[CUR["row"]].setdefault(layer_idx, []).append(sel.to("cpu", torch.int16))
        return sel, w
    module.routing_fn = wrapped


def union_factor(a, b, layers, n, rng):
    """Mean over layers and n random position pairs of |experts(a, i) u experts(b, j)| / top_k."""
    tot, k = 0.0, None
    for l in layers:
        ea, eb = a[l], b[l]
        k = ea.shape[1]
        ia = torch.tensor([rng.randrange(ea.shape[0]) for _ in range(n)])
        ib = torch.tensor([rng.randrange(eb.shape[0]) for _ in range(n)])
        sa, sb = ea[ia].long(), eb[ib].long()
        # distinct experts in each pair = 2k - |intersection|
        inter = (sa.unsqueeze(2) == sb.unsqueeze(1)).any(dim = 2).sum(dim = 1).float()
        tot += ((2 * k - inter) / k).mean().item()
    return tot / len(layers)


def main(args):
    sel = json.load(open(args.rows))
    rows = json.load(open(args.trace))["rows"]
    order = sel["continue"]
    group_of = {i: g for g, ids in sel.get("groups", {}).items() for i in ids}

    model, config, cache, tokenizer = model_init.init(args)[:4]
    moe = [m for m in model if isinstance(m, BlockSparseMLP)]
    for li, m in enumerate(moe):
        hook(m, li)
    print(f" -- {len(moe)} MoE layers hooked, {len(order)} rows", flush = True)
    generator = Generator(model = model, cache = cache, tokenizer = tokenizer, max_chunk_size = 2048)

    for i in order:
        r = rows[i]
        ids = r["input_ids"] + r["response_ids"][:args.max_tokens]
        CUR["row"] = i
        REC[i] = {}
        generator.enqueue(Job(input_ids = torch.tensor([ids]), max_new_tokens = 1, identifier = i))
        while generator.num_remaining_jobs():
            generator.iterate()
        CUR["row"] = None
        REC[i] = {l: torch.cat(v) for l, v in REC[i].items()}
        print(f"    row {i:3} ({group_of.get(i, '?')}): {len(ids)} tokens", flush = True)

    torch.save({"rows": order, "groups": group_of, "experts": REC}, args.output)

    rng = random.Random(0)
    layers = sorted(next(iter(REC.values())).keys())
    names = [f"{i}{group_of.get(i, '?')[0]}" for i in order]
    M = torch.zeros(len(order), len(order))
    for x, a in enumerate(order):
        for y, b in enumerate(order):
            if y >= x:
                M[x, y] = M[y, x] = union_factor(REC[a], REC[b], layers, args.samples, rng)
    print("\n -- decode-step cost of a batch of two, |A u B| / top_k (1.0 = full sharing, 2.0 = none)")
    print("        " + " ".join(f"{n:>6}" for n in names))
    for x, n in enumerate(names):
        print(f"{n:>7} " + " ".join(f"{M[x, y]:6.3f}" for y in range(len(order))))

    def mean_over(pairs):
        v = [M[x, y].item() for x, y in pairs]
        return sum(v) / len(v) if v else float("nan")
    idx = range(len(order))
    same_row = mean_over([(x, x) for x in idx])
    same_grp = mean_over([(x, y) for x in idx for y in idx if x < y and group_of.get(order[x]) == group_of.get(order[y])])
    diff_grp = mean_over([(x, y) for x in idx for y in idx if x < y and group_of.get(order[x]) != group_of.get(order[y])])
    print(f"\n -- mean cost: same row {same_row:.3f}, same group {same_grp:.3f}, different groups {diff_grp:.3f}")
    print(f" -- throughput gain of a batch of two vs one at a time (CPU-MoE bound): "
          f"same group {2 / same_grp:.2f}x, different groups {2 / diff_grp:.2f}x")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 65536)
    ap.add_argument("--trace", required = True)
    ap.add_argument("--rows", required = True, help = "selection JSON with \"continue\" and \"groups\"")
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--max_tokens", type = int, default = 8192, help = "response tokens per row to prefill")
    ap.add_argument("--samples", type = int, default = 400, help = "random position pairs per layer")
    main(ap.parse_args())
