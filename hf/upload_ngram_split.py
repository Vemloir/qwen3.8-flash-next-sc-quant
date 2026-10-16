"""
Upload the n-gram table as two safetensors files (Hugging Face caps files at 50 GB; the table is 51.8 GB), built on
the fly from the single local file: each output is a new header plus byte ranges of the original, nothing is written
to disk. exllamav3 finds the table shards by tensor name across all *.safetensors files of a model.
"""
import io, json, os, struct, sys
from huggingface_hub import HfApi, CommitOperationAdd

SRC = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.05-v3/ngram_embedding.safetensors")
REPO, BRANCH = "Vemloir/Qwen3.8-Flash-Next-sc-exl3", "4.05bpw_sc_h8"


class SlicedFile(io.RawIOBase):
    """Read-only, seekable concatenation of an in-memory header and byte ranges of one file"""
    def __init__(self, header: bytes, path: str, ranges: list):
        self.parts = [("mem", header, 0, len(header))] + [("file", path, o, n) for o, n in ranges]
        self.size = sum(p[3] for p in self.parts)
        self.pos = 0
        self.fh = open(path, "rb")
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence = 0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos
    def readinto(self, b):
        want, out, start = len(b), 0, 0
        for kind, src, o, n in self.parts:
            end = start + n
            if self.pos < end and out < want:
                rel = self.pos - start
                k = min(n - rel, want - out)
                if kind == "mem":
                    b[out:out + k] = src[rel:rel + k]
                else:
                    self.fh.seek(o + rel)
                    b[out:out + k] = self.fh.read(k)
                out += k
                self.pos += k
            start = end
            if out >= want:
                break
        return out
    def close(self):
        self.fh.close()
        super().close()


def split(path):
    f = open(path, "rb")
    n = struct.unpack("<Q", f.read(8))[0]
    h = json.loads(f.read(n))
    meta = h.pop("__metadata__", None)
    base = 8 + n
    shard = lambda k: int(k.split(".shard_")[1].split(".")[0]) if ".shard_" in k else -1
    num = max(shard(k) for k in h) + 1
    groups = [[k for k in h if shard(k) < num // 2], [k for k in h if shard(k) >= num // 2]]
    out = []
    for keys in groups:
        keys.sort(key = lambda k: h[k]["data_offsets"][0])
        hdr, ranges, off = {}, [], 0
        for k in keys:
            a, b = h[k]["data_offsets"]
            hdr[k] = {"dtype": h[k]["dtype"], "shape": h[k]["shape"], "data_offsets": [off, off + b - a]}
            ranges.append((base + a, b - a))
            off += b - a
        if meta:
            hdr["__metadata__"] = meta
        j = json.dumps(hdr, separators = (",", ":")).encode()
        j += b" " * (-len(j) % 8)
        out.append((struct.pack("<Q", len(j)) + j, ranges, len(keys)))
    return out


if __name__ == "__main__":
    parts = split(SRC)
    raw = [SlicedFile(hdr, SRC, ranges) for hdr, ranges, _ in parts]
    files = [io.BufferedReader(r, buffer_size = 8 << 20) for r in raw]
    names = [f"ngram_embedding-{i + 1:05d}-of-{len(files):05d}.safetensors" for i in range(len(files))]
    for nm, r, (_, _, k) in zip(names, raw, parts):
        print(f" -- {nm}: {k} tensors, {r.size / 1e9:.2f} GB", flush = True)
    if "--dry" in sys.argv:
        sys.exit(0)
    api = HfApi()
    api.create_commit(repo_id = REPO, revision = BRANCH, commit_message = "n-gram table (8 bpw), as two files",
                      operations = [CommitOperationAdd(path_in_repo = nm, path_or_fileobj = f) for nm, f in zip(names, files)])
    print("upload done", flush = True)
