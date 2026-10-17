"""
Runs ON a Colab CPU VM with Google Drive mounted: copy the BF16 checkpoint of Qwen3.8-Flash-Next
from the Hub to Drive, so a GPU VM can convert from it without holding ~250 GB on its own disk.

Shards that hold only the per-layer n-gram (PLE) table are skipped: the quant uses its own 8-bit
table. Each shard is downloaded to local disk over plain HTTP, copied to Drive, checked by size and
deleted locally; a rerun skips shards already on Drive with the right size.

    python drive_bf16.py --dest /content/drive/MyDrive/sc-quant/BF16 --workers 3
"""
import argparse, json, os, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor

os.environ["HF_HUB_DISABLE_XET"] = "1"
from huggingface_hub import HfApi, hf_hub_download

REPO = "Qwen/Qwen3.8-Flash-Next"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush = True)


def main(args):
    api = HfApi()
    sizes = {f.path: f.size for f in api.list_repo_tree(REPO, recursive = True) if hasattr(f, "size")}
    os.makedirs(args.dest, exist_ok = True)
    os.makedirs(args.stage, exist_ok = True)
    index_path = hf_hub_download(REPO, "model.safetensors.index.json", local_dir = args.stage)
    weight_map = json.load(open(index_path))["weight_map"]
    kinds = {}
    for name, shard in weight_map.items():
        kinds.setdefault(shard, set()).add("ngram" if "ngram" in name else "other")
    shards = sorted(s for s, k in kinds.items() if k != {"ngram"})
    small = [p for p in sizes if not p.endswith(".safetensors") and "/" not in p]
    todo = [p for p in small + shards
            if not (os.path.exists(os.path.join(args.dest, p)) and os.path.getsize(os.path.join(args.dest, p)) == sizes[p])]
    total = sum(sizes[p] for p in todo)
    log(f"{len(shards)} weight shards needed ({len(kinds) - len(shards)} n-gram-only skipped), "
        f"{len(todo)} files to copy, {total / 1e9:.1f} GB")

    done = [0]
    def one(p):
        # DriveFS keeps written files in a local content cache until they are uploaded; wait
        # rather than run the VM's disk full
        while shutil.disk_usage("/").free / 1e9 < args.min_free_gb:
            log(f"waiting for DriveFS to free its cache ({shutil.disk_usage('/').free / 1e9:.0f} GB free)")
            time.sleep(60)
        for attempt in range(5):
            try:
                local = hf_hub_download(REPO, p, local_dir = args.stage)
                dst = os.path.join(args.dest, p)
                shutil.copyfile(local, dst + ".part")
                os.replace(dst + ".part", dst)
                if os.path.getsize(dst) != sizes[p]:
                    raise IOError(f"size mismatch on Drive: {os.path.getsize(dst)} != {sizes[p]}")
                os.remove(local)
                done[0] += sizes[p]
                log(f"ok {p} ({done[0] / 1e9:.1f} / {total / 1e9:.1f} GB)")
                return
            except Exception as e:
                log(f"retry {p} ({attempt}): {e!r}"[:300])
                time.sleep(30)
        log(f"FAILED {p}")

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(one, todo))
    missing = [p for p in small + shards if not os.path.exists(os.path.join(args.dest, p))]
    log("ALL DONE" if not missing else f"INCOMPLETE, missing {len(missing)}: {missing[:5]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default = "/content/drive/MyDrive/sc-quant/BF16")
    ap.add_argument("--stage", default = "/content/stage")
    ap.add_argument("--workers", type = int, default = 3)
    ap.add_argument("--min_free_gb", type = float, default = 40)
    main(ap.parse_args())
