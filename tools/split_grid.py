"""
Every bit split of the experts at once: for every MoE layer given, a fixed sample of experts has each projection
(gate, up, down) quantized once at every K of a grid (EXL3, as convert.py does it), and the routed output error
of the experts on held-out tokens (same metric as moe_hessian_test.py) is taken for every combination
(Kg, Ku, Kd) of the grid from those quantized weights:
  err[g, u, d] = sum_t r_te^2 |f_q(x_t) - f(x_t)|^2 / sum_t r_te^2 |f(x_t)|^2,  summed over the sampled experts

Quantization dominates the time, so the full grid costs about as much as a handful of single splits.
--hessian soft uses the affinity-weighted Hessians H_e = sum_t (0.1 + p_te / mean_t p_te) x x^T.

    PYTHONPATH=<exllamav3 tree> python split_grid.py --layers 0 12 20 30 40 46 --experts 64
"""
import argparse, json, os, sys, time, itertools
import torch
import torch.nn.functional as F
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exllamav3 import Config, Model
from exllamav3.modules.block_sparse_mlp import BlockSparseMLP
from moe_hessian_test import rows_from_cal, rows_from_trace, collect, H_data_for, DEV, Q
from layer_sens import quant


@torch.inference_mode()
def grid_layer(module, Xc, Xe, n_experts, Ks, hessian, seed):
    Xc = Xc.to(DEV).float()
    Xe = Xe.to(DEV).float()
    cfg = module.routing_cfg
    _, Pc = module.routing_fn(Xc.shape[0], cfg, Xc.half(), {"activate_all_experts": True})
    Pc = Pc.float()
    sel_e, rw_e = module.routing_fn(Xe.shape[0], cfg, Xe.half(), {})
    Re = torch.zeros((Xe.shape[0], Pc.shape[1]), device = DEV).scatter_(1, sel_e.long(), rw_e.float())

    cand = [e for e in range(Pc.shape[1]) if (Re[:, e] > 0).sum() >= 8]
    g = torch.Generator().manual_seed(seed)
    pick = [cand[i] for i in torch.randperm(len(cand), generator = g)[:n_experts].tolist()]

    n = len(Ks)
    num = torch.zeros((n, n, n), dtype = torch.float64)
    den = 0.0
    H_shared = Xc.T @ Xc if hessian == "shared" else None
    for e in pick:
        W_g = module.gates[e].inner.get_weight_tensor().float()
        W_u = module.ups[e].inner.get_weight_tensor().float()
        W_d = module.downs[e].inner.get_weight_tensor().float()
        A_c = F.silu(Xc @ W_g) * (Xc @ W_u)
        if hessian == "soft":
            a = Pc[:, e]
            w = 0.1 + a / a.mean().clamp_min(1e-12)
            Hgu, cgu = (Xc * w[:, None]).T @ Xc, w.sum().item()
            Hd, cd = (A_c * w[:, None]).T @ A_c, w.sum().item()
        else:
            Hgu, cgu = H_shared, Xc.shape[0]
            Hd, cd = A_c.T @ A_c, A_c.shape[0]
        m = Re[:, e] > 0
        xe, re2 = Xe[m], Re[m, e][:, None] ** 2
        ref = (F.silu(xe @ W_g) * (xe @ W_u)) @ W_d
        den += (re2 * ref ** 2).sum().item()
        # gate/up pre-activations at every K, down weights at every K
        G = [xe @ quant(W_g, H_data_for(Hgu.clone(), cgu, f"e{e}.g"), K, seed, True) for K in Ks]
        U = [xe @ quant(W_u, H_data_for(Hgu.clone(), cgu, f"e{e}.u"), K, seed, True) for K in Ks]
        D = torch.stack([quant(W_d, H_data_for(Hd.clone(), cd, f"e{e}.d"), K, seed, True) for K in Ks])
        for ig, iu in itertools.product(range(n), range(n)):
            act = F.silu(G[ig]) * U[iu]
            out = torch.einsum("ti,kio->kto", act, D)
            num[ig, iu] += (re2[None] * (out - ref[None]) ** 2).sum(dim = (1, 2)).double().cpu()
    return (num / den).tolist(), len(pick)


def main(args):
    config = Config.from_directory(args.model)
    config.override_dynamic_seq_len(args.max_len + 64)
    model = Model.from_config(config)
    cal = rows_from_cal(args.cal, args.cal_tokens, args.max_len)
    ev = rows_from_trace(args.eval, args.eval_tokens, args.max_len)
    print(f" -- {sum(r.shape[1] for r in cal):,} calibration tokens, {sum(r.shape[1] for r in ev):,} held-out tokens, K grid {args.K}", flush = True)
    results = json.load(open(args.out)) if os.path.exists(args.out) and args.resume else {"K": args.K, "hessian": args.hessian}
    t_start = time.time()

    def on_layer(l, module, inputs):
        if str(l) in results:
            return
        t0 = time.time()
        stack = [module]
        while not isinstance(stack[-1], BlockSparseMLP):
            m = stack.pop()
            stack.extend(getattr(m, "modules", []))
        err, n = grid_layer(stack[-1], inputs[0], inputs[1], args.experts, args.K, args.hessian, args.seed + l)
        results[str(l)] = {"err": err, "experts": n}
        json.dump(results, open(args.out, "w"))
        # best split per average bitrate, against the uniform one
        Ks = args.K
        best = {}
        for ig, iu, id_ in itertools.product(range(len(Ks)), repeat = 3):
            b = (Ks[ig] + Ks[iu] + Ks[id_]) / 3
            if b not in best or err[ig][iu][id_] < best[b][0]:
                best[b] = (err[ig][iu][id_], f"{Ks[ig]}/{Ks[iu]}/{Ks[id_]}")
        i4 = Ks.index(4)
        u4 = err[i4][i4][i4]
        print(f" -- layer {l:2} ({n} experts, {time.time() - t0:.0f} s, {(time.time() - t_start) / 60:.0f} min total): 4/4/4 {u4:.3e}  " +
              "  ".join(f"@{b:.2f} {best[b][1]} {(best[b][0] / u4 - 1) * 100:+.1f}%" for b in (3.5, 11 / 3, 3.75, 4.0, 13 / 3, 4.5) if b in best), flush = True)

    collect(model, config, [cal, ev], args.layers, on_layer)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("--cal", default = f"{Q}/calib_v2/cal_trace.safetensors")
    ap.add_argument("--eval", default = f"{Q}/eval/heldout_trace.json")
    ap.add_argument("--layers", type = int, nargs = "+", default = [0, 12, 20, 30, 40, 46])
    ap.add_argument("--experts", type = int, default = 64)
    ap.add_argument("--K", type = lambda k: int(k) if float(k).is_integer() else float(k), nargs = "+", default = [2, 2.5, 3, 3.5, 4, 5, 6])
    ap.add_argument("--hessian", choices = ["shared", "soft"], default = "soft")
    ap.add_argument("--cal_tokens", type = int, default = 50000)
    ap.add_argument("--eval_tokens", type = int, default = 30000)
    ap.add_argument("--max_len", type = int, default = 6144)
    ap.add_argument("--seed", type = int, default = 0)
    ap.add_argument("--out", default = f"{Q}/eval/split_grid.json")
    ap.add_argument("--resume", action = "store_true")
    main(ap.parse_args())
