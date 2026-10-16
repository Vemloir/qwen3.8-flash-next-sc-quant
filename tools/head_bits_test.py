"""
Output head at K bits: KLD of the logits of the quantized head against the BF16 head, on held-out
tokens, with the head's inputs taken from the BF16 model (so only the head's own error is measured).
The head is quantized as convert.py does it (EXL3, mul1, Hessian from calibration tokens).

    PYTHONPATH=<exllamav3 tree> python head_bits_test.py --bits 6 8
"""
import argparse, json, os, sys, time
import torch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exllamav3 import Config, Model
from exllamav3.modules import Linear
from moe_hessian_test import rows_from_cal, rows_from_trace, H_data_for, quant, DEV, Q


@torch.inference_mode()
def collect_head_inputs(model, config, rows_sets):
    """Stream the BF16 model through every module but the head; return the head Linear's inputs per set."""
    grabbed = {}
    which = {"set": None}
    orig = Linear.forward

    def hook(self, x, params, out_dtype = None):
        if self.key.endswith("lm_head") and which["set"] is not None:
            grabbed.setdefault(which["set"], []).append(x.reshape(-1, x.shape[-1]).half().cpu())
            return x.new_empty((*x.shape[:-1], 1))
        return orig(self, x, params, out_dtype)

    Linear.forward = hook
    all_rows = [r for s in rows_sets for r in s]
    sets = [k for k, s in enumerate(rows_sets) for _ in s]
    params_rows = [{} for _ in all_rows]
    states = [model.prepare_inputs(r, p) for r, p in zip(all_rows, params_rows)]
    head = None
    try:
        for idx, module in enumerate(model.modules):
            config.stc.begin_deferred_load()
            module.load(DEV if not module.caps.get("prefer_cpu") else "cpu")
            config.stc.end_deferred_load()
            last = idx == len(model.modules) - 1
            for r in range(len(states)):
                which["set"] = sets[r] if last else None
                p = params_rows[r]
                p.setdefault("input_ids", all_rows[r])
                x = module.forward(module.prepare_for_device(states[r], p), p)
                states[r] = None if last else (x.cpu() if torch.is_tensor(x) else x)
            if last:
                head = module
            else:
                module.unload()
                config.stc.close()
    finally:
        Linear.forward = orig
    stack = [head]
    while not (isinstance(stack[-1], Linear) and stack[-1].key.endswith("lm_head")):
        m = stack.pop()
        stack.extend(getattr(m, "modules", []))
    return [torch.cat(grabbed[k]) for k in range(len(rows_sets))], stack[-1]


@torch.inference_mode()
def logit_kld(Xe, W, Wq, vocab, chunk = 256):
    kl_sum, agree, n = 0.0, 0, 0
    for a in range(0, Xe.shape[0], chunk):
        x = Xe[a:a + chunk].to(DEV).float()
        ref = (x @ W)[:, :vocab].log_softmax(-1)
        q = (x @ Wq)[:, :vocab].log_softmax(-1)
        kl_sum += (ref.exp() * (ref - q)).sum().item()
        agree += (ref.argmax(-1) == q.argmax(-1)).sum().item()
        n += x.shape[0]
    return kl_sum / n, agree / n


def main(args):
    config = Config.from_directory(args.model)
    config.override_dynamic_seq_len(args.max_len + 64)
    model = Model.from_config(config)
    cal = rows_from_cal(args.cal, args.cal_tokens, args.max_len)
    ev = rows_from_trace(args.eval, args.eval_tokens, args.max_len)
    t0 = time.time()
    (Xc, Xe), head = collect_head_inputs(model, config, [cal, ev])
    vocab = config.vocab_size
    print(f" -- head inputs: {Xc.shape[0]:,} calibration, {Xe.shape[0]:,} held-out tokens ({time.time() - t0:.0f} s)")
    W = head.inner.get_weight_tensor().float()
    Xg = Xc.to(DEV).float()
    H = Xg.T @ Xg
    del Xg
    res = {}
    for K in args.bits:
        t0 = time.time()
        Wq = quant(W, H_data_for(H.clone(), Xc.shape[0], f"lm_head.{K}"), K, 0)
        kld, agree = logit_kld(Xe, W, Wq, vocab)
        res[K] = {"kld": kld, "top1_agree": agree, "extra_MB_vs_6": (K - 6) * W.numel() / 8 / 2**20}
        print(f" -- head K={K}: KLD {kld:.3e}  top-1 agreement {agree * 100:.3f}%  ({time.time() - t0:.0f} s)", flush = True)
        del Wq
        torch.cuda.empty_cache()
    json.dump(res, open(args.out, "w"), indent = 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/BF16"))
    ap.add_argument("--cal", default = f"{Q}/calib_v2/cal_trace.safetensors")
    ap.add_argument("--eval", default = f"{Q}/eval/heldout_trace.json")
    ap.add_argument("--bits", type = int, nargs = "+", default = [6, 8])
    ap.add_argument("--cal_tokens", type = int, default = 100000)
    ap.add_argument("--eval_tokens", type = int, default = 30000)
    ap.add_argument("--max_len", type = int, default = 6144)
    ap.add_argument("--out", default = f"{Q}/eval/head_bits_test.json")
    main(ap.parse_args())
