"""
Check that a top-level module run over one long calibration row in chunks (with a cache carrying
attention K/V, QSA indexer keys and GDN/PLE recurrent state across chunk boundaries) reproduces the
plain full-row forward that convert.py uses. Loads the unquantized model one module at a time like
convert.py, runs both paths, prints the relative difference per module and passes the full-row
output on to the next module.

    PYTHONPATH=<exllamav3 tree> python chunk_forward_test.py -m <model> --cal calib/cal_trace.safetensors \\
        --row 0 --length 32768 --chunk 8192 --modules 6
"""
import argparse, math
import torch
from safetensors.torch import load_file
from exllamav3 import Config, Model, Cache
from exllamav3.constants import PAGE_SIZE
from exllamav3.cache.recurrent_util import advance_recurrent_states


@torch.inference_mode()
def chunked_forward(module, model, cache, x, ids, chunk, device):
    """Forward one row through one module in chunks of <chunk> tokens, outputs gathered on the CPU."""
    L = x.shape[1]
    cap = math.ceil(L / PAGE_SIZE) * PAGE_SIZE
    outs = []
    state = cache.get_new_state() if cache.recurrent_layers else None
    try:
        for a in range(0, L, chunk):
            b = min(a + chunk, L)
            params = {
                "attn_mode": "flash_attn",
                "cache": cache,
                "batch_shape": (1, cap),
                "past_len": a,
                "input_ids": ids[:, :b] if a == 0 else ids[:, a:b],
            }
            if state is not None:
                params["recurrent_states"] = [state]
            # same input preparation as a generator prefill chunk (block table, positions,
            # recurrent slots)
            model.prepare_inputs(ids[:, a:b], params)
            xa = module.prepare_for_device(x[:, a:b], params)
            y = module.forward(xa, params)
            advance_recurrent_states(ids[:, a:b], params, model)
            outs.append(y.cpu())
            del xa, y
    finally:
        if state is not None:
            cache.release_state(state)
    return torch.cat(outs, dim = 1)


def main(args):
    dev = torch.device("cuda", 0)
    data = load_file(args.cal)
    n = min(args.length, int(data["lengths"][args.row]))
    ids = data["input_ids"][args.row : args.row + 1, :n].clone()
    print(f" -- row {args.row}: {n} tokens, chunk {args.chunk}")

    config = Config.from_directory(args.model)
    model = Model.from_config(config)
    cache = Cache(model, max_num_tokens = math.ceil(n / PAGE_SIZE) * PAGE_SIZE, max_batch_size = 1)
    cache.initialized = True

    state = ids
    for idx, module in enumerate(model.modules[:args.modules]):
        config.stc.begin_deferred_load()
        module.load(dev if not module.caps.get("prefer_cpu") else "cpu")
        config.stc.end_deferred_load()
        params = {"attn_mode": "flash_attn_nc", "input_ids": ids}
        x = module.prepare_for_device(state, params)
        full = module.forward(x, params).cpu()
        del x
        torch.cuda.empty_cache()
        if idx == 0 or full.dim() < 3 or full.shape[1] != n:
            print(f"    {module.key:45} full only {tuple(full.shape)}")
        else:
            part = chunked_forward(module, model, cache, state, ids, args.chunk, dev)
            d = (part.float() - full.float())
            rel = (d.square().sum() / full.float().square().sum()).sqrt().item()
            print(f"    {module.key:45} rel diff {rel:.3e}  max abs {d.abs().max().item():.3e}  "
                  f"peak VRAM {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB")
        torch.cuda.reset_peak_memory_stats()
        state = full
        module.unload()
        config.stc.close()
        torch.cuda.empty_cache()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", required = True)
    ap.add_argument("--cal", required = True)
    ap.add_argument("--row", type = int, default = 0)
    ap.add_argument("--length", type = int, default = 32768)
    ap.add_argument("--chunk", type = int, default = 8192)
    ap.add_argument("--modules", type = int, default = 6)
    main(ap.parse_args())
