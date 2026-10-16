"""
Tensor types of a (split) GGUF model on the Hub, read from the headers only (HTTP range requests).

    python gguf_types.py <repo> <dir-in-repo> -o types.json
"""
import json, struct, sys, urllib.request, os

GGML = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K",
        11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS",
        19: "IQ1_S", 20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16", 26: "I32",
        27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16", 34: "TQ1_0", 35: "TQ2_0", 39: "MXFP4"}


class Remote:
    def __init__(self, url):
        self.url, self.buf, self.pos = url, b"", 0

    def need(self, n):
        while len(self.buf) < self.pos + n:
            a = len(self.buf)
            req = urllib.request.Request(self.url, headers = {"Range": f"bytes={a}-{a + (4 << 20) - 1}"})
            self.buf += urllib.request.urlopen(req, timeout = 60).read()

    def read(self, n):
        self.need(n)
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u32(self): return struct.unpack("<I", self.read(4))[0]
    def u64(self): return struct.unpack("<Q", self.read(8))[0]
    def s(self): return self.read(self.u64()).decode("utf-8", "replace")


def skip_value(r, t):
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    if t in sizes:
        r.read(sizes[t])
    elif t == 8:
        r.s()
    elif t == 9:
        et, n = r.u32(), r.u64()
        for _ in range(n):
            skip_value(r, et)


def tensors(url):
    r = Remote(url)
    assert r.read(4) == b"GGUF"
    r.u32()
    nt, nkv = r.u64(), r.u64()
    for _ in range(nkv):
        r.s()
        skip_value(r, r.u32())
    out = {}
    for _ in range(nt):
        name = r.s()
        nd = r.u32()
        dims = [r.u64() for _ in range(nd)]
        t = r.u32()
        r.u64()
        out[name] = {"type": GGML.get(t, str(t)), "shape": dims}
    return out


def main():
    repo, d = sys.argv[1], sys.argv[2]
    from huggingface_hub import HfApi
    files = sorted(f for f in HfApi().list_repo_files(repo) if f.startswith(d + "/") and f.endswith(".gguf"))
    allt = {}
    for f in files:
        allt.update(tensors(f"https://huggingface.co/{repo}/resolve/main/{f}"))
    json.dump(allt, open(sys.argv[sys.argv.index("-o") + 1], "w"), indent = 0)
    print(d, len(allt), "tensors")


main()
