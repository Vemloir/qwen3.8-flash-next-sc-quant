"""
Merge several safetensors files into one by streaming (no tensor is loaded): headers are combined,
offsets recomputed, data copied in chunks. Used to rebuild the single n-gram table file that
convert.py --ngram_file expects from the two parts published on the Hub (50 GB per-file limit).
Inputs are deleted one by one after they are copied with --delete.

    python merge_safetensors.py -o ngram_embedding.safetensors part1.safetensors part2.safetensors
"""
import argparse, json, os, struct


def header(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n)), 8 + n


def main(args):
    heads = [header(p) for p in args.inputs]
    merged, meta, offset, plan = {}, {}, 0, []
    for p, (h, base) in zip(args.inputs, heads):
        meta.update(h.pop("__metadata__", {}) or {})
        for name, t in sorted(h.items(), key = lambda kv: kv[1]["data_offsets"][0]):
            a, b = t["data_offsets"]
            assert name not in merged, f"duplicate tensor {name}"
            merged[name] = {"dtype": t["dtype"], "shape": t["shape"], "data_offsets": [offset, offset + b - a]}
            plan.append((p, base + a, b - a))
            offset += b - a
    if meta:
        merged["__metadata__"] = meta
    hb = json.dumps(merged, separators = (",", ":")).encode()
    hb += b" " * (-len(hb) % 8)
    with open(args.output, "wb") as out:
        out.write(struct.pack("<Q", len(hb)))
        out.write(hb)
        done = {}
        for p, start, n in plan:
            with open(p, "rb") as f:
                f.seek(start)
                while n:
                    chunk = f.read(min(n, 64 << 20))
                    out.write(chunk)
                    n -= len(chunk)
            done[p] = done.get(p, 0) + 1
            if args.delete and done[p] == sum(1 for q, _, _ in plan if q == p):
                os.remove(p)
    print(f" -- {len(plan)} tensors, {offset / 1e9:.1f} GB -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--delete", action = "store_true")
    ap.add_argument("inputs", nargs = "+")
    main(ap.parse_args())
