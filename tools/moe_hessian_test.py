"""
Prototype: does weighting each expert's Hessian by its routing affinity improve the quantized experts?

convert.py captures expert Hessians with every expert active on every calibration token, so all 512
experts of a layer share one gate/up Hessian (H = sum over all tokens of x x^T) and each down Hessian
also counts all tokens equally. In inference an expert only sees the tokens routed to it. Here, for a
few layers of the BF16 model, a sample of experts is quantized (EXL3, K bits, as convert.py does it)
with
  shared        the current Hessians
  soft eps      H_e = sum_t (eps + p_te / mean_t p_te) x x^T, p = full router softmax
  routed eps    H_e = sum_t (eps + r_te / mean_t r_te) x x^T, r = top-k routing weight (0 if not routed)
and scored on held-out tokens by the routed output error of each expert:
  sum_t r_te^2 |f_q(x_t) - f(x_t)|^2 / sum_t r_te^2 |f(x_t)|^2

    PYTHONPATH=<exllamav3 tree> python moe_hessian_test.py --layers 4 24 44 --experts 48
"""
import argparse, json, os, re, time
import torch
import torch.nn.functional as F
from safetensors.torch import load_file
from exllamav3 import Config, Model
from exllamav3.modules.block_sparse_mlp import BlockSparseMLP
from exllamav3.modules.quant.exl3_lib.quantize import quantize_exl3

Q = os.path.expanduser("~/lm-inference/sc-quant")
DEV = torch.device("cuda", 0)


def rows_from_cal(path, budget, max_len):
    d = load_file(path)
    skip = set(d["mm_row"].tolist()) if "mm_row" in d else set()
    out, n = [], 0
    for i in range(d["input_ids"].shape[0]):
        L = int(d["lengths"][i])
        if i in skip or L > max_len:
            continue
        out.append(d["input_ids"][i:i + 1, :L].long())
        n += L
        if n >= budget:
            break
    return out


def rows_from_trace(path, budget, max_len):
    out, n = [], 0
    for r in json.load(open(path))["rows"]:
        ids = r["input_ids"] + r["response_ids"]
        if len(ids) > max_len:
            continue
        out.append(torch.tensor([ids], dtype = torch.long))
        n += len(ids)
        if n >= budget:
            break
    return out


@torch.inference_mode()
def collect(model, config, rows_sets, layers, on_layer):
    """Stream the BF16 model up to the last target layer; at each target layer call
    on_layer(layer, module, [MoE inputs per set]) while the layer is resident."""
    keys = {f"model.language_model.layers.{l}": l for l in layers}
    grabbed, current = {}, {"layer": None, "set": None}
    orig_forward = BlockSparseMLP.forward

    def hook(self, x, params, out_dtype = None):
        if current["layer"] is not None:
            grabbed.setdefault((current["layer"], current["set"]), []).append(
                x.view(-1, self.hidden_size).to(torch.half).cpu())
        return orig_forward(self, x, params, out_dtype)

    BlockSparseMLP.forward = hook
    all_rows = [r for s in rows_sets for r in s]
    which = [k for k, s in enumerate(rows_sets) for _ in s]
    params_rows = [{} for _ in all_rows]
    states = [model.prepare_inputs(r, p) for r, p in zip(all_rows, params_rows)]
    last = max(keys[k] for k in keys)
    try:
        for idx, module in enumerate(model.modules):
            config.stc.begin_deferred_load()
            module.load(DEV if not module.caps.get("prefer_cpu") else "cpu")
            config.stc.end_deferred_load()
            current["layer"] = keys.get(module.key)
            for r in range(len(states)):
                current["set"] = which[r]
                p = params_rows[r]
                p.setdefault("input_ids", all_rows[r])
                x = module.forward(module.prepare_for_device(states[r], p), p)
                states[r] = x.cpu() if torch.is_tensor(x) else x
            l = current["layer"]
            current["layer"] = None
            if l is not None:
                inputs = [torch.cat(grabbed.pop((l, s))) for s in range(len(rows_sets))]
                on_layer(l, module, inputs)
            module.unload()
            config.stc.close()
            if l == last:
                break
    finally:
        BlockSparseMLP.forward = orig_forward


def H_data_for(H, count, key):
    return {"H": H.contiguous(), "first_key": key, "count": count, "finalized": False, "num_total": 0,
            "inf_nan": torch.zeros(2, dtype = torch.long, device = DEV), "device": DEV}


def quant(w, H_data, K, seed):
    qa = {"seed": seed, "K": K, "devices": [0], "device_ratios": None, "apply_out_scales": True,
          "mul1": True, "sigma_reg_out": 0.025}
    # quantize_exl3 regularizes the weight in place, so it gets a copy
    wq, perr, _ = quantize_exl3(w.float().clone(), H_data, qa, True, None, False, None)
    if os.environ.get("MOE_H_DEBUG"):
        print(f"      {H_data['first_key']:10} proxy {perr:.3e}  |W| {w.float().norm():.3e}  |Wq-W| {(wq.float() - w.float()).norm():.3e}  count {H_data['count']}")
    return wq.float()


def expert_fwd(x, g, u, d):
    return (F.silu(x @ g) * (x @ u)) @ d


@torch.inference_mode()
def test_layer(module, Xc, Xe, n_experts, K, variants, seed):
    Xc = Xc.to(DEV).float()
    Xe = Xe.to(DEV).float()
    cfg = module.routing_cfg
    # full softmax affinities on calibration tokens, top-k routing weights on both sets
    _, Pc = module.routing_fn(Xc.shape[0], cfg, Xc.half(), {"activate_all_experts": True})
    Pc = Pc.float()
    sel_c, rw_c = module.routing_fn(Xc.shape[0], cfg, Xc.half(), {})
    Rc = torch.zeros_like(Pc).scatter_(1, sel_c.long(), rw_c.float())
    sel_e, rw_e = module.routing_fn(Xe.shape[0], cfg, Xe.half(), {})
    Re = torch.zeros((Xe.shape[0], Pc.shape[1]), device = DEV).scatter_(1, sel_e.long(), rw_e.float())

    # experts with enough routed held-out tokens to score, sampled evenly over the layer
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
        A_c = F.silu(Xc @ W_g) * (Xc @ W_u)          # down inputs on calibration tokens
        m = Re[:, e] > 0
        xe, re_ = Xe[m], Re[m, e]
        ref = expert_fwd(xe, W_g, W_u, W_d)
        den += (re_[:, None] ** 2 * ref ** 2).sum().item()
        for v in variants:
            if v == "shared":
                w = None
            else:
                kind, eps = v.split()
                a = Pc[:, e] if kind == "soft" else Rc[:, e]
                w = float(eps) + a / a.mean().clamp_min(1e-12)
            if w is None:
                Hgu, cgu = H_shared.clone(), Xc.shape[0]
                Hd, cd = A_c.T @ A_c, A_c.shape[0]
            else:
                Hgu, cgu = (Xc * w[:, None]).T @ Xc, w.sum().item()
                Hd, cd = (A_c * w[:, None]).T @ A_c, w.sum().item()
            hgu = H_data_for(Hgu, cgu, f"e{e}.gu")
            qg = quant(W_g, hgu, K, seed)
            qu = quant(W_u, hgu, K, seed)
            qd = quant(W_d, H_data_for(Hd, cd, f"e{e}.d"), K, seed)
            out = expert_fwd(xe, qg, qu, qd)
            num[v] += (re_[:, None] ** 2 * (out - ref) ** 2).sum().item()
    return {v: num[v] / den for v in variants}, len(pick)


def main(args):
    config = Config.from_directory(args.model)
    config.override_dynamic_seq_len(args.max_len + 64)
    model = Model.from_config(config)
    cal = rows_from_cal(args.cal, args.cal_tokens, args.max_len)
    ev = rows_from_trace(args.eval, args.eval_tokens, args.max_len)
    print(f" -- {sum(r.shape[1] for r in cal):,} calibration tokens, {sum(r.shape[1] for r in ev):,} held-out tokens")
    variants = args.variants
    results = {}

    def on_layer(l, module, inputs):
        t0 = time.time()
        if args.save_inputs:
            torch.save({"cal": inputs[0], "eval": inputs[1]}, os.path.join(args.save_inputs, f"moe_inputs_{l}.pt"))
        stack = [module]
        while not isinstance(stack[-1], BlockSparseMLP):
            m = stack.pop()
            stack.extend(getattr(m, "modules", []))
        res, n = test_layer(stack[-1], inputs[0], inputs[1], args.experts, args.K, variants, args.seed + l)
        results[l] = res
        base = res["shared"]
        print(f" -- layer {l:2} ({n} experts, {time.time() - t0:.0f} s): " +
              "  ".join(f"{v} {res[v]:.3e} ({(res[v] / base - 1) * 100:+.1f}%)" for v in variants), flush = True)
        json.dump(results, open(args.out, "w"), indent = 1)

    cached = args.save_inputs and all(os.path.exists(os.path.join(args.save_inputs, f"moe_inputs_{l}.pt"))
                                      for l in args.layers)
    if cached:
        # inputs from an earlier run: load just the target layers
        for idx, module in enumerate(model.modules):
            m = re.fullmatch(r"model\.language_model\.layers\.(\d+)", module.key)
            if m and int(m.group(1)) in args.layers:
                l = int(m.group(1))
                config.stc.begin_deferred_load()
                module.load(DEV)
                config.stc.end_deferred_load()
                d = torch.load(os.path.join(args.save_inputs, f"moe_inputs_{l}.pt"))
                on_layer(l, module, [d["cal"], d["eval"]])
                module.unload()
                config.stc.close()
    else:
        collect(model, config, [cal, ev], args.layers, on_layer)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("--cal", default = f"{Q}/calib_v2/cal_trace.safetensors")
    ap.add_argument("--eval", default = f"{Q}/eval/heldout_trace.json")
    ap.add_argument("--layers", type = int, nargs = "+", default = [4, 24, 44])
    ap.add_argument("--experts", type = int, default = 48)
    ap.add_argument("--K", type = int, default = 4)
    ap.add_argument("--cal_tokens", type = int, default = 50000)
    ap.add_argument("--eval_tokens", type = int, default = 30000)
    ap.add_argument("--max_len", type = int, default = 6144)
    ap.add_argument("--seed", type = int, default = 0)
    ap.add_argument("--out", default = f"{Q}/eval/moe_hessian_test.json")
    ap.add_argument("--variants", nargs = "+", default = ["shared", "soft 0.1", "routed 0.1", "routed 0.01"])
    ap.add_argument("--save_inputs", default = None, help = "directory to cache the collected MoE inputs in")
    main(ap.parse_args())
