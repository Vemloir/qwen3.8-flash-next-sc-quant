"""
Expert sensitivity per layer: for every MoE layer of the BF16 model, a fixed sample of experts is quantized
(EXL3, as convert.py does it) under several bit splits and Hessian/scale options, and scored on held-out tokens
by the routed output error of the experts (same metric as moe_hessian_test.py):
  sum_t r_te^2 |f_q(x_t) - f(x_t)|^2 / sum_t r_te^2 |f(x_t)|^2

A variant is "Kg/Ku/Kd[:opts]", opts a comma list of
  noos     no output channel scales on gate and up (exllamav3 1.5.4 --out_scales auto does this for experts)
  soft     affinity-weighted Hessians, H_e = sum_t (0.1 + p_te / mean_t p_te) x x^T, p = full router softmax

    PYTHONPATH=<exllamav3 tree> python layer_sens.py --layers 0-47 --experts 64
"""
import argparse, json, os, sys, time
import torch
import torch.nn.functional as F
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exllamav3 import Config, Model
from exllamav3.modules.block_sparse_mlp import BlockSparseMLP
from exllamav3.modules.quant.exl3_lib.quantize import quantize_exl3
from moe_hessian_test import rows_from_cal, rows_from_trace, collect, H_data_for, expert_fwd, DEV, Q

DEFAULT_VARIANTS = ["4/4/4", "3.5/3.5/5", "3.5/3.5/3.5", "5/5/5", "4/4/4:noos", "4/4/4:soft", "3.5/3.5/5:soft"]


def parse_variant(v):
    ks, _, opts = v.partition(":")
    kg, ku, kd = (float(k) if "." in k else int(k) for k in ks.split("/"))
    opts = set(o for o in opts.split(",") if o)
    return kg, ku, kd, opts


def quant(w, H_data, K, seed, out_scales):
    qa = {"seed": seed, "K": K, "devices": [0], "device_ratios": None, "apply_out_scales": out_scales,
          "mul1": True, "sigma_reg_out": 0.025}
    # quantize_exl3 regularizes the weight in place, so it gets a copy
    wq, _, _ = quantize_exl3(w.float().clone(), H_data, qa, True, None, False, None)
    return wq.float()


@torch.inference_mode()
def test_layer(module, Xc, Xe, n_experts, variants, seed):
    Xc = Xc.to(DEV).float()
    Xe = Xe.to(DEV).float()
    cfg = module.routing_cfg
    _, Pc = module.routing_fn(Xc.shape[0], cfg, Xc.half(), {"activate_all_experts": True})
    Pc = Pc.float()
    sel_e, rw_e = module.routing_fn(Xe.shape[0], cfg, Xe.half(), {})
    Re = torch.zeros((Xe.shape[0], Pc.shape[1]), device = DEV).scatter_(1, sel_e.long(), rw_e.float())
    load = (Re > 0).float().mean(0)

    cand = [e for e in range(Pc.shape[1]) if (Re[:, e] > 0).sum() >= 8]
    g = torch.Generator().manual_seed(seed)
    pick = [cand[i] for i in torch.randperm(len(cand), generator = g)[:n_experts].tolist()]

    H_shared = Xc.T @ Xc
    num = {v: 0.0 for v in variants}
    den = 0.0
    for e in pick:
        W_g = module.gates[e].inner.get_weight_tensor().float()
        W_u = module.ups[e].inner.get_weight_tensor().float()
        W_d = module.downs[e].inner.get_weight_tensor().float()
        A_c = F.silu(Xc @ W_g) * (Xc @ W_u)
        m = Re[:, e] > 0
        xe, re_ = Xe[m], Re[m, e]
        ref = expert_fwd(xe, W_g, W_u, W_d)
        den += (re_[:, None] ** 2 * ref ** 2).sum().item()
        a = Pc[:, e]
        w_soft = 0.1 + a / a.mean().clamp_min(1e-12)
        for v in variants:
            kg, ku, kd, opts = parse_variant(v)
            if "soft" in opts:
                Hgu, cgu = (Xc * w_soft[:, None]).T @ Xc, w_soft.sum().item()
                Hd, cd = (A_c * w_soft[:, None]).T @ A_c, w_soft.sum().item()
            else:
                Hgu, cgu = H_shared.clone(), Xc.shape[0]
                Hd, cd = A_c.T @ A_c, A_c.shape[0]
            os_gu = "noos" not in opts
            qg = quant(W_g, H_data_for(Hgu.clone(), cgu, f"e{e}.g"), kg, seed, os_gu)
            qu = quant(W_u, H_data_for(Hgu, cgu, f"e{e}.u"), ku, seed, os_gu)
            qd = quant(W_d, H_data_for(Hd, cd, f"e{e}.d"), kd, seed, True)
            out = expert_fwd(xe, qg, qu, qd)
            num[v] += (re_[:, None] ** 2 * (out - ref) ** 2).sum().item()
    return {v: num[v] / den for v in variants}, len(pick), load


def main(args):
    config = Config.from_directory(args.model)
    config.override_dynamic_seq_len(args.max_len + 64)
    model = Model.from_config(config)
    cal = rows_from_cal(args.cal, args.cal_tokens, args.max_len)
    ev = rows_from_trace(args.eval, args.eval_tokens, args.max_len)
    print(f" -- {sum(r.shape[1] for r in cal):,} calibration tokens, {sum(r.shape[1] for r in ev):,} held-out tokens", flush = True)
    a, _, b = args.layers.partition("-")
    layers = list(range(int(a), int(b or a) + 1))
    results = json.load(open(args.out)) if os.path.exists(args.out) and args.resume else {}
    t_start = time.time()

    def on_layer(l, module, inputs):
        if str(l) in results:
            return
        t0 = time.time()
        stack = [module]
        while not isinstance(stack[-1], BlockSparseMLP):
            m = stack.pop()
            stack.extend(getattr(m, "modules", []))
        res, n, load = test_layer(stack[-1], inputs[0], inputs[1], args.experts, args.variants, args.seed + l)
        results[str(l)] = {"err": res, "experts": n, "load_cv": (load.std() / load.mean()).item()}
        base = res[args.variants[0]]
        print(f" -- layer {l:2} ({n} experts, {time.time() - t0:.0f} s, {(time.time() - t_start) / 60:.0f} min total): base {base:.3e}  " +
              "  ".join(f"{v} {(res[v] / base - 1) * 100:+.1f}%" for v in args.variants[1:]), flush = True)
        json.dump({"variants": args.variants, **results}, open(args.out, "w"), indent = 1)

    collect(model, config, [cal, ev], layers, on_layer)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("--cal", default = f"{Q}/calib_v2/cal_trace.safetensors")
    ap.add_argument("--eval", default = f"{Q}/eval/heldout_trace.json")
    ap.add_argument("--layers", default = "0-47")
    ap.add_argument("--experts", type = int, default = 64)
    ap.add_argument("--cal_tokens", type = int, default = 50000)
    ap.add_argument("--eval_tokens", type = int, default = 30000)
    ap.add_argument("--max_len", type = int, default = 6144)
    ap.add_argument("--seed", type = int, default = 0)
    ap.add_argument("--variants", nargs = "+", default = DEFAULT_VARIANTS)
    ap.add_argument("--out", default = f"{Q}/eval/layer_sens.json")
    ap.add_argument("--resume", action = "store_true")
    main(ap.parse_args())
