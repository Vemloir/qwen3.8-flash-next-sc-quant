"""
Results for the model card and the evaluation dataset from the qbench cache: per-source KL divergence of
this quant and turboderp's 4.05 bpw against BF16 on the Nemotron and multilingual traces, with 95%
paired-bootstrap intervals over rows (results/kld_by_source.json), and the per-token KL vectors behind
the histogram (results/per_token_kld.safetensors). Optional --prev adds the previous version of this quant.

Models are named by their qbench cache key (the last hash in kl_<data>_<reference>_<model>_m5.safetensors).

    PYTHONPATH=pylibs python tools/kld_results.py --cache eval/_logit_cache/qbench \
        --ours e6391eb51f5e5d42 --turboderp 14faed2bd7b69723 --prev 0ac86f8e46ca3926 -o hf/dataset/results
"""
import argparse, glob, json, os
import numpy as np
from safetensors.numpy import load_file, save_file

TRACES = {"nemotron": "eval/nemotron_trace.json", "multilingual": "eval/multilingual_trace.json"}


def kl_file(cache, model):
    files = glob.glob(os.path.join(cache, f"kl_*_{model}_m5.safetensors"))
    return files


def main(args):
    models = {"sc_4.05": args.ours, "turboderp_4.05": args.turboderp}
    if args.prev:
        models["sc_4.05_prev"] = args.prev
    rng = np.random.default_rng(0)
    out, per_token = {}, {}
    for trace, path in TRACES.items():
        rows = json.load(open(path))["rows"]
        kl, ranges = {}, None
        for name, key in models.items():
            # the data key is the one whose reference logits have as many rows as the trace
            for f in kl_file(args.cache, key):
                data, ref = os.path.basename(f).split("_")[1:3]
                meta_path = os.path.join(args.cache, f"logits_{data}_{ref}", "meta.json")
                if not os.path.exists(meta_path):   # reference logits of other traces may have been evicted
                    continue
                meta = json.load(open(meta_path))
                if meta["rows"] == len(rows) and (ranges is None or meta["ranges"] == ranges):
                    kl[name], ranges = load_file(f)["kl"].astype(np.float64), meta["ranges"]
                    break
            assert name in kl, f"{trace}: no cached KL vector for {name} ({key})"
        lens = np.array([sum(b - a for a, b in spans) for spans in ranges])
        assert lens.sum() == len(kl["sc_4.05"]), f"{trace}: {lens.sum()} scored positions, {len(kl['sc_4.05'])} KL values"
        row_of = np.repeat(np.arange(len(rows)), lens)
        row_sum = {n: np.bincount(row_of, weights = v, minlength = len(rows)) for n, v in kl.items()}
        sources = np.array([r["source"] for r in rows])
        res = {}
        for src in [*dict.fromkeys(sources), "all"]:
            sel = np.ones(len(rows), bool) if src == "all" else sources == src
            idx = np.nonzero(sel)[0]
            n_tok = int(lens[idx].sum())
            bi = idx[rng.integers(0, len(idx), (args.boot, len(idx)))]   # rows resampled within this source
            entry = {"rows": int(len(idx)), "scored_tokens": n_tok}
            for n in models:
                entry[f"kld_{n}"] = float(row_sum[n][idx].sum() / n_tok)
            for a, b, tag in [("sc_4.05", "turboderp_4.05", ""), ("sc_4.05", "sc_4.05_prev", "_vs_prev")]:
                if b not in models:
                    continue
                rel = row_sum[a][bi].sum(1) / row_sum[b][bi].sum(1) - 1
                entry[f"rel_diff{tag}"] = float(row_sum[a][idx].sum() / row_sum[b][idx].sum() - 1)
                entry[f"ci95{tag}"] = [float(x) for x in np.quantile(rel, [0.025, 0.975])]
                entry[f"rows_sc_lower{tag}"] = int((row_sum[a][idx] < row_sum[b][idx]).sum())
            res[src] = entry
        out[trace] = res
        for n, v in kl.items():
            per_token[f"{trace}.{n}"] = v.astype(np.float32)
        a = res["all"]
        print(f" -- {trace}: {a['rows']} rows, {a['scored_tokens']} tokens, ours {a['kld_sc_4.05']:.6f}, "
              f"turboderp {a['kld_turboderp_4.05']:.6f}, {a['rel_diff'] * 100:+.1f}% "
              f"[{a['ci95'][0] * 100:+.1f}, {a['ci95'][1] * 100:+.1f}]"
              + (f", vs previous {a['rel_diff_vs_prev'] * 100:+.1f}% [{a['ci95_vs_prev'][0] * 100:+.1f}, "
                 f"{a['ci95_vs_prev'][1] * 100:+.1f}]" if args.prev else ""))
    os.makedirs(args.output, exist_ok = True)
    json.dump(out, open(os.path.join(args.output, "kld_by_source.json"), "w"), indent = 1)
    save_file(per_token, os.path.join(args.output, "per_token_kld.safetensors"))
    print(f" -- wrote {args.output}/kld_by_source.json and per_token_kld.safetensors")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default = "eval/_logit_cache/qbench")
    ap.add_argument("--ours", required = True, help = "qbench model key of this quant")
    ap.add_argument("--turboderp", required = True, help = "qbench model key of turboderp's 4.05 bpw")
    ap.add_argument("--prev", help = "qbench model key of the previous version of this quant")
    ap.add_argument("--boot", type = int, default = 20000)
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
