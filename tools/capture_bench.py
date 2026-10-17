"""
Time convert.py's calibration capture pass on one MoE layer (layer 0 of Qwen3.8-Flash-Next, which
comes before any PLE layer, so the n-gram table is not needed): a real calibration row goes through
embed_tokens and hc_expand, then layer 0 is run with every expert active and Hessian capture on,
as convert.py does. Prints tokens/s and, with --profile, the top CUDA kernels and Python callers.

    PYTHONPATH=<exllamav3 tree> python tools/capture_bench.py -m <BF16 dir> --cal calib_v3/cal_trace.safetensors \
        --tokens 8192 [--profile]
"""
import argparse, time
import torch
from safetensors.torch import load_file


@torch.inference_mode()
def main(args):
    from exllamav3 import Config, Model
    from exllamav3.conversion.moe_affinity import make_store as make_affinity_store
    config = Config.from_directory(args.model_dir)
    model = Model.from_config(config)
    dev = torch.device("cuda", 0)
    cal = load_file(args.cal)
    lengths = cal["lengths"]
    row = int(torch.nonzero(lengths >= args.tokens)[0])
    ids = cal["input_ids"][row:row + 1, :args.tokens].clone()
    print(f" -- row {row}, {args.tokens} tokens")

    keys = [m.key for m in model.modules]
    target = keys.index(args.layer)
    x = ids
    for m in model.modules[:target]:
        m.load(device = dev)
        params = {"attn_mode": "flash_attn_nc", "input_ids": ids}
        x = m.prepare_for_device(x, params)
        x = m.forward(x, params)
        m.unload()
    module = model.modules[target]
    module.load(device = dev)
    torch.cuda.synchronize()
    print(f" -- {module.key} loaded, state {tuple(x.shape)} {x.dtype}, VRAM {torch.cuda.memory_allocated() / 2**30:.1f} GiB")

    import exllamav3.modules.linear as L

    def capture(fast = False, tf32 = False):
        L._CAPTURE_TF32 = tf32
        params = {"attn_mode": "flash_attn_nc", "capture": {}, "capture_sens": {},
                  "activate_all_experts": model.calibration_all_experts, "input_ids": ids}
        if fast:
            params["capture_only"] = True
        if args.aff_eps is not None:
            params["moe_affinity"] = make_affinity_store(args.aff_eps, 262144, args.tokens * 4, seed = 0)
        rs = module.prepare_for_device(x, params)
        rs = module.forward(rs, params)
        torch.cuda.synchronize()
        return params

    def timed(**kw):
        capture(**kw)                               # warm-up
        t0 = time.time()
        for _ in range(args.reps):
            p = capture(**kw)
        dt = (time.time() - t0) / args.reps
        print(f" -- capture {kw}: {dt:.2f} s per {args.tokens} tokens = {args.tokens / dt:,.0f} tokens/s "
              f"-> {2.15e6 / (args.tokens / dt) / 60:.1f} min per layer for 2.15M tokens", flush = True)
        return p

    base = timed(fast = False, tf32 = False)
    variants = [dict(fast = True, tf32 = False), dict(fast = True, tf32 = True)] if args.compare else []
    for kw in variants:
        p = timed(**kw)
        worst = {}
        for k, hd in base["capture"].items():
            for t in ("H", "H_aff"):
                if t in hd:
                    a, b = hd[t], p["capture"][k][t]
                    rel = ((a - b).abs().max() / a.abs().max().clamp_min(1e-30)).item()
                    worst[t] = max(worst.get(t, 0), rel)
            if hd["count"] != p["capture"][k]["count"]:
                worst["count_mismatch"] = worst.get("count_mismatch", 0) + 1
        missing = set(base["capture_sens"]) ^ set(p["capture_sens"])
        if missing:
            worst["sens_keys_differ"] = len(missing)
        for k, v in base["capture_sens"].items():
            w = p["capture_sens"].get(k)
            if w is None:
                continue
            a, b = v["sum"], w["sum"]
            worst["sens"] = max(worst.get("sens", 0), ((a - b).abs().max() / a.abs().max().clamp_min(1e-30)).item())
            if v["count"] != w["count"]:
                worst["sens_count_mismatch"] = worst.get("sens_count_mismatch", 0) + 1
        print(f"    vs baseline: {len(base['capture'])} H entries, max relative diff {worst}", flush = True)

    if args.profile:
        from torch.profiler import profile, ProfilerActivity
        with profile(activities = [ProfilerActivity.CPU, ProfilerActivity.CUDA], with_stack = False) as prof:
            capture(fast = args.compare, tf32 = args.compare)
        print(prof.key_averages().table(sort_by = "cuda_time_total", row_limit = 18, max_name_column_width = 60))
        print(prof.key_averages().table(sort_by = "cpu_time_total", row_limit = 12, max_name_column_width = 60))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model_dir", required = True)
    ap.add_argument("--cal", required = True)
    ap.add_argument("--layer", default = "model.language_model.layers.0")
    ap.add_argument("--tokens", type = int, default = 8192)
    ap.add_argument("--reps", type = int, default = 2)
    ap.add_argument("--aff_eps", type = float, default = 0.1)
    ap.add_argument("--profile", action = "store_true")
    ap.add_argument("--compare", action = "store_true", help = "also run the capture_only fast path (+ TF32) and diff the Hessians")
    main(ap.parse_args())
