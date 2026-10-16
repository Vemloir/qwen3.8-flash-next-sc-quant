"""
Is the Qwen FP8 checkpoint's n-gram table a good enough source for the K-bit trellis table, or do
we need the BF16 original? Samples rows present in both checkpoints, then measures each variant's
reconstruction error against the BF16 rows:

    fp8          FP8 rows * global weight_scale, as-is
    K<k> bf16    trellis-quantized from the BF16 rows
    K<k> fp8     trellis-quantized from the dequantized FP8 rows
    reference    the existing quantized table file (e.g. turboderp's EXL3-4.05), same rows

rfn = ||x_hat - x|| / ||x|| over the sample (the convert_ngram.py convention), also given against the
head-bias-centered rows, which is the part the trellis actually encodes.

    PYTHONPATH=<exllamav3 tree> python ngram_fp8_vs_bf16.py --bf16 <shard> --fp8 <shard> \\
        --ref <ngram_embedding.safetensors> --rows 262144 --bits 6 8
"""
import argparse, re
import torch
from safetensors import safe_open
from exllamav3.conversion.ngram import quantize_rows, NgramTableReader

PFX = "model.language_model.layers.1.ple.ple_embedding.ngram_embedding."


def load_shards(path):
    out = {}
    with safe_open(path, "pt") as f:
        for k in f.keys():
            m = re.fullmatch(re.escape(PFX) + r"shard_(\d+)\.weight", k)
            if m:
                out[int(m.group(1))] = f.get_tensor(k)
        scale = f.get_tensor(PFX + "weight_scale") if PFX + "weight_scale" in f.keys() else None
    return out, scale


def rfn(x_hat, x):
    return ((x_hat - x).square().sum().sqrt() / x.square().sum().sqrt()).item()


def main(args):
    dev = "cuda:0"
    bf, _ = load_shards(args.bf16)
    f8, scale = load_shards(args.fp8)
    assert scale is not None, "no weight_scale in the FP8 shard"
    shards = sorted(set(bf) & set(f8))
    print(f" -- shards {shards}, rows/shard {bf[shards[0]].shape[0]}, fp8 dtype {f8[shards[0]].dtype}, "
          f"weight_scale {scale.flatten().tolist()}")
    ref = NgramTableReader(args.ref)
    rows_per_shard = ref.num_rows // 128 + (ref.num_rows % 128 > 0)
    assert all(bf[s].shape[0] == rows_per_shard for s in shards if s < 127), (rows_per_shard, bf[shards[0]].shape)

    g = torch.Generator().manual_seed(0)
    per = args.rows // len(shards)
    idx_g, x_l, y_l = [], [], []
    for s in shards:
        r = torch.randint(0, bf[s].shape[0], (per,), generator = g).unique()
        idx_g.append(r + s * rows_per_shard)
        x_l.append(bf[s][r].float())
        y_l.append(f8[s].view(torch.uint8)[r].view(f8[s].dtype).float())
    idx = torch.cat(idx_g)
    x = torch.cat(x_l).to(dev)
    y_raw = torch.cat(y_l).to(dev)
    del bf, f8

    sc = scale.float().flatten()[0].item()
    y = y_raw * sc
    heads = (torch.searchsorted(ref.head_offsets, idx, right = True) - 1).clamp(0, ref.head_offsets.numel() - 1)
    bias = ref.head_bias[heads].to(dev).float()
    xc = x - bias
    print(f" -- {x.shape[0]} rows, heads {heads.unique().tolist()}, |x| rms {x.square().mean().sqrt().item():.4e}")
    print(f" -- fp8*scale vs bf16 rms ratio {y.square().mean().sqrt().item() / x.square().mean().sqrt().item():.4f} "
          f"(fp8/scale would be {(y_raw / sc).square().mean().sqrt().item() / x.square().mean().sqrt().item():.3e})")
    nz = x != 0
    print(f" -- underflow: {((y == 0) & nz).sum().item() / nz.sum().item():.4%} of nonzero bf16 elements are 0 in fp8")

    def report(name, x_hat):
        print(f"    {name:22} rfn {rfn(x_hat, x):.5f}   centered rfn {rfn(x_hat - bias, xc):.5f}")

    report("fp8 (dequant)", y)
    ref_deq = ref.dequant(idx, dev, out_dtype = torch.float)
    report(f"reference K{ref.K}", ref_deq)
    for K in args.bits:
        for name, src in (("bf16", x), ("fp8", y)):
            deq = torch.empty_like(x)
            for a in range(0, x.shape[0], args.chunk):
                _, d = quantize_rows(src[a : a + args.chunk], bias[a : a + args.chunk].half(), K)
                deq[a : a + args.chunk] = d.float()
            report(f"K{K} from {name}", deq)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bf16", required = True)
    ap.add_argument("--fp8", required = True)
    ap.add_argument("--ref", required = True, help = "existing quantized ngram_embedding.safetensors")
    ap.add_argument("--rows", type = int, default = 262144)
    ap.add_argument("--chunk", type = int, default = 32768)
    ap.add_argument("--bits", type = int, nargs = "+", default = [6, 8])
    main(ap.parse_args())
