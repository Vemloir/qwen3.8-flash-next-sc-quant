"""
Turn the FP8 checkpoint into an "FP8 body + BF16 n-gram table" source, in place.

exllamav3 reads the n-gram table rows as stored and does not apply the FP8 checkpoint's global
table scale (ngram_embedding.weight_scale = 1.99e-4), so a forward pass over the FP8 table is
wrong by ~5000x. The BF16 checkpoint's table is exact. The loader globs every *.safetensors file
in the model directory (no index, unordered), so the two must not share tensor names:

  - FP8 shards holding table tensors are rewritten without the table and its scale
  - only the table tensors of each BF16 shard are copied into ngram-bf16-<n>.safetensors in the
    FP8 directory, and the BF16 shard is deleted right after (bounded extra disk use)
  - model.safetensors.index.json is updated to match

    python make_hybrid.py --fp8 <FP8 dir> --bf16 <BF16-table dir>
"""
import argparse, json, os
from safetensors import safe_open
from safetensors.torch import save_file

TABLE = "ple_embedding.ngram_embedding."


def is_table(name):
    return TABLE in name


def main(args):
    idx_path = os.path.join(args.fp8, "model.safetensors.index.json")
    idx = json.load(open(idx_path))
    wm = idx["weight_map"]

    # 1. FP8 shards: drop table tensors (and the global table scale)
    fp8_files = sorted({f for k, f in wm.items() if is_table(k)})
    for fn in fp8_files:
        path = os.path.join(args.fp8, fn)
        with safe_open(path, "pt") as f:
            keys = list(f.keys())
            meta = f.metadata()
            keep = {k: f.get_tensor(k) for k in keys if not is_table(k)}
        tmp = path + ".tmp"
        save_file(keep, tmp, metadata = meta)
        os.replace(tmp, path)
        print(f" -- {fn}: kept {len(keep)} of {len(keys)} tensors", flush = True)
    for k in [k for k in wm if is_table(k)]:
        del wm[k]

    # 2. BF16 shards: copy only table tensors next to the FP8 body, then delete the shard
    bidx = json.load(open(os.path.join(args.bf16, "model.safetensors.index.json")))["weight_map"]
    bf16_files = sorted({f for k, f in bidx.items() if is_table(k)})
    for i, fn in enumerate(bf16_files):
        src = os.path.join(args.bf16, fn)
        out_name = f"ngram-bf16-{i:05d}.safetensors"
        with safe_open(src, "pt") as f:
            tensors = {k: f.get_tensor(k) for k in f.keys() if is_table(k)}
        save_file(tensors, os.path.join(args.fp8, out_name))
        for k in tensors:
            wm[k] = out_name
        if not args.keep_bf16:
            os.remove(src)
        print(f" -- {fn} -> {out_name}: {len(tensors)} table tensors", flush = True)

    json.dump(idx, open(idx_path, "w"), indent = 2)
    print(" -- hybrid source ready", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fp8", required = True)
    ap.add_argument("--bf16", required = True)
    ap.add_argument("--keep_bf16", action = "store_true")
    main(ap.parse_args())
