"""
Results for the model card and the evaluation dataset from the qbench cache: per-source KL divergence of
this quant and turboderp's 4.05 bpw against BF16 on the Nemotron and multilingual traces, with 95%
paired-bootstrap intervals over rows (results/kld_by_source.json), and the per-token KL vectors behind
the histogram (results/per_token_kld.safetensors). Optional --prev adds the previous version of this quant,
--turboderp_sc turboderp's own self-calibrated SC 4.18 bpw.

Scored positions inside the generation prompt that the chat template writes itself (`<think>\n` with reasoning on,
`<think>\n\n</think>\n\n` with it off) are left out: in real use the model never generates those tokens, while
the teacher-forced traces score them as part of the assistant turn. --keep_template_tokens keeps them (plain qbench).

Models are named by their qbench cache key (the last hash in kl_<data>_<reference>_<model>_m5.safetensors); a model
scored on the two traces from different source paths has two keys, given comma-separated.

    PYTHONPATH=pylibs python tools/kld_results.py --cache eval/_logit_cache/qbench \
        --ours e6391eb51f5e5d42 --turboderp 14faed2bd7b69723 --prev 0ac86f8e46ca3926 -o hf/dataset/results
"""
import argparse, glob, json, os
import numpy as np
from safetensors.numpy import load_file, save_file
from transformers import AutoTokenizer

TRACES = {"nemotron": "eval/nemotron_trace.json", "multilingual": "eval/multilingual_trace.json"}


def kl_file(cache, model):
    files = glob.glob(os.path.join(cache, f"kl_*_{model}_m5.safetensors"))
    return files


def template_mask(rows, tok):
    """Per scored position in trace order: False where the token is part of the generation prompt the chat
    template writes (from <think> on), True elsewhere"""
    prompts = [tok.encode(s, add_special_tokens = False) for s in
               ("<|im_start|>assistant\n<think>\n\n</think>\n\n", "<|im_start|>assistant\n<think>\n")]
    start = prompts[0].index(tok.convert_tokens_to_ids("<think>"))
    keep = []
    for r in rows:
        seq = r["input_ids"] + r["response_ids"]
        forced = set()
        for s in range(len(seq)):
            for g in prompts:
                if seq[s:s + len(g)] == g:
                    forced.update(range(s + start, s + len(g)))
                    break
        keep += [p not in forced for a, b in r["score_spans"] for p in range(a, b)]
    return np.array(keep)


def main(args):
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    models = {"sc_4.05": args.ours, "turboderp_4.05": args.turboderp}
    if args.prev:
        models["sc_4.05_prev"] = args.prev
    if args.turboderp_sc:
        models["turboderp_sc_4.18"] = args.turboderp_sc
    rng = np.random.default_rng(0)
    out, per_token = {}, {}
    for trace, path in TRACES.items():
        rows = json.load(open(path))["rows"]
        kl, ranges = {}, None
        for name, key in models.items():
            # the data key is the one whose reference logits have as many rows as the trace
            for f in [f for k in key.split(",") for f in kl_file(args.cache, k)]:
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
        assert list(lens) == [sum(b - a for a, b in r["score_spans"]) for r in rows], f"{trace}: spans differ from the trace"
        row_of = np.repeat(np.arange(len(rows)), lens)
        keep = np.ones(len(row_of), bool) if args.keep_template_tokens else template_mask(rows, tok)
        kl = {n: v[keep] for n, v in kl.items()}
        row_of = row_of[keep]
        lens = np.bincount(row_of, minlength = len(rows))
        print(f" -- {trace}: {int((~keep).sum())} template-written positions left out of {len(keep)}")
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
            for a, b, tag in [("sc_4.05", "turboderp_4.05", ""), ("sc_4.05", "sc_4.05_prev", "_vs_prev"),
                              ("sc_4.05", "turboderp_sc_4.18", "_vs_turboderp_sc_4.18")]:
                if b not in models:
                    continue
                rel = row_sum[a][bi].sum(1) / row_sum[b][bi].sum(1) - 1
                entry[f"rel_diff{tag}"] = float(row_sum[a][idx].sum() / row_sum[b][idx].sum() - 1)
                entry[f"ci95{tag}"] = [float(x) for x in np.quantile(rel, [0.025, 0.975])]
                entry[f"rows_sc_lower{tag}"] = int((row_sum[a][idx] < row_sum[b][idx]).sum())
            res[src] = entry
        res["all"]["excluded_template_tokens"] = int((~keep).sum())
        out[trace] = res
        for n, v in kl.items():
            per_token[f"{trace}.{n}"] = v.astype(np.float32)
        a = res["all"]
        print(f" -- {trace}: {a['rows']} rows, {a['scored_tokens']} tokens, ours {a['kld_sc_4.05']:.6f}, "
              f"turboderp {a['kld_turboderp_4.05']:.6f}, {a['rel_diff'] * 100:+.1f}% "
              f"[{a['ci95'][0] * 100:+.1f}, {a['ci95'][1] * 100:+.1f}]"
              + (f", vs previous {a['rel_diff_vs_prev'] * 100:+.1f}% [{a['ci95_vs_prev'][0] * 100:+.1f}, "
                 f"{a['ci95_vs_prev'][1] * 100:+.1f}]" if args.prev else "")
              + (f", vs turboderp SC 4.18 {a['rel_diff_vs_turboderp_sc_4.18'] * 100:+.1f}% "
                 f"[{a['ci95_vs_turboderp_sc_4.18'][0] * 100:+.1f}, {a['ci95_vs_turboderp_sc_4.18'][1] * 100:+.1f}]"
                 if args.turboderp_sc else ""))
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
    ap.add_argument("--turboderp_sc", help = "qbench model key(s) of turboderp's SC 4.18 bpw")
    ap.add_argument("--boot", type = int, default = 20000)
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/SC2-EXL3-4.05"))
    ap.add_argument("--keep_template_tokens", action = "store_true", help = "score every position, as plain qbench does")
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
