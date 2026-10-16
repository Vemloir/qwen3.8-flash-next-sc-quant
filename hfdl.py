"""
Rate-limit-friendly parallel HF downloader (stdlib only).

The repo tree (sizes, LFS sha256) is fetched once and cached; each file's CDN URL is resolved
once and reused for all byte ranges (re-resolved only when the signed URL expires); ranges are
fetched by a thread pool with retries and backoff; the result is sha256-verified before it is
renamed into place. Already present files with the right size are skipped.

    python hfdl.py --repo Qwen/Qwen3.8-Flash-Next-FP8 --out <dir> [--files f1 f2 ...] [--threads 8]
"""
import argparse, hashlib, json, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

CHUNK = 64 << 20
UA = {"User-Agent": "hfdl/1.0"}


RANGE_DEADLINE = 600


def api_tree(repo, rev, cache, kind):
    if os.path.exists(cache):
        return json.load(open(cache))
    url = f"https://huggingface.co/api/{kind}s/{repo}/tree/{rev}?recursive=true"
    files = {}
    while url:
        for attempt in range(20):
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers = UA), timeout = 60)
                data = json.load(r)
                break
            except Exception as e:
                print(f"tree attempt {attempt}: {e}", flush = True)
                time.sleep(min(300, 15 * (attempt + 1)))
        else:
            raise RuntimeError("could not list repo")
        files.update({f["path"]: {"size": f["size"], "sha": (f.get("lfs") or {}).get("oid")}
                      for f in data if f["type"] == "file"})
        link = r.headers.get("Link") or ""
        url = link.split(";")[0].strip("<> ") if 'rel="next"' in link else None
    json.dump(files, open(cache, "w"))
    return files


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def resolve(repo, rev, path, kind = "model"):
    """Final CDN URL for a file (follows HF's redirect chain without downloading)."""
    pre = "" if kind == "model" else "datasets/"
    url = f"https://huggingface.co/{pre}{repo}/resolve/{rev}/{urllib.request.quote(path)}"
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(5):
        req = urllib.request.Request(url, method = "HEAD", headers = UA)
        try:
            r = opener.open(req, timeout = 60)
            return url
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers["Location"]
                url = loc if loc.startswith("http") else "https://huggingface.co" + loc
                continue
            raise
    return url


def fetch_file(repo, rev, path, meta, out, threads, kind = "model"):
    dst = os.path.join(out, path)
    os.makedirs(os.path.dirname(dst), exist_ok = True)
    if os.path.exists(dst) and os.path.getsize(dst) == meta["size"]:
        return "skip"
    tmp = dst + ".part"
    size = meta["size"]
    with open(tmp, "ab") as f:
        f.truncate(size)
    done_file = tmp + ".done"
    done = set(json.load(open(done_file))) if os.path.exists(done_file) else set()
    lock = threading.Lock()
    state = {"url": None, "t": 0}

    def url():
        with lock:
            if state["url"] is None or time.time() - state["t"] > 1800:
                state["url"], state["t"] = resolve(repo, rev, path, kind), time.time()
            return state["url"]

    def get_range(start):
        end = min(start + CHUNK, size) - 1
        for attempt in range(40):
            try:
                req = urllib.request.Request(url(), headers = {**UA, "Range": f"bytes={start}-{end}"})
                # a trickling connection never trips the socket timeout; cap the whole range too
                deadline = time.time() + RANGE_DEADLINE
                parts = []
                with urllib.request.urlopen(req, timeout = 120) as r:
                    while True:
                        piece = r.read(1 << 20)
                        if not piece:
                            break
                        parts.append(piece)
                        if time.time() > deadline:
                            raise IOError("range deadline exceeded")
                buf = b"".join(parts)
                if len(buf) != end - start + 1:
                    raise IOError(f"short read {len(buf)}")
                fd = os.open(tmp, os.O_WRONLY)
                try:
                    os.pwrite(fd, buf, start)
                finally:
                    os.close(fd)
                with lock:
                    done.add(start)
                    json.dump(sorted(done), open(done_file, "w"))
                return
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 410):
                    with lock:
                        state["url"] = None
                time.sleep(min(120, 5 * (attempt + 1)))
            except Exception:
                time.sleep(min(120, 5 * (attempt + 1)))
        raise RuntimeError(f"range {start} of {path} failed")

    todo = [s for s in range(0, size, CHUNK) if s not in done]
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(get_range, todo))
    if meta.get("sha"):
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for blk in iter(lambda: f.read(16 << 20), b""):
                h.update(blk)
        if h.hexdigest() != meta["sha"]:
            os.remove(tmp)
            os.remove(done_file)
            raise RuntimeError(f"sha256 mismatch for {path}")
    os.replace(tmp, dst)
    if os.path.exists(done_file):
        os.remove(done_file)
    return "ok"


def main(args):
    os.makedirs(args.out, exist_ok = True)
    tree = api_tree(args.repo, args.rev, os.path.join(args.out, ".hfdl_tree.json"), args.repo_type)
    files = args.files or sorted(tree)
    if args.prefix:
        files = [f for f in files if f.startswith(tuple(args.prefix))]
    failed = []
    for p in files:
        t0 = time.time()
        try:
            r = fetch_file(args.repo, args.rev, p, tree[p], args.out, args.threads, args.repo_type)
            dt = time.time() - t0
            print(f"{r} {p} {tree[p]['size'] / 1e9:.2f} GB in {dt:.0f} s"
                  + (f" ({tree[p]['size'] / dt / 1e6:.1f} MB/s)" if r == "ok" else ""), flush = True)
        except Exception as e:
            failed.append(p)
            print(f"FAILED {p}: {e}", flush = True)
    print(f"ALL DONE {time.strftime('%H:%M:%S')} failed={len(failed)} {failed}", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required = True)
    ap.add_argument("--rev", default = "main")
    ap.add_argument("--out", required = True)
    ap.add_argument("--files", nargs = "*")
    ap.add_argument("--threads", type = int, default = 8)
    ap.add_argument("--repo_type", default = "model", choices = ["model", "dataset"])
    ap.add_argument("--prefix", nargs = "*", help = "only paths starting with one of these")
    main(ap.parse_args())
