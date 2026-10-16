"""
Pick MiMo-V2.6-RL-oss agent tasks for the calibration trace and the held-out eval from the local
parquet files, per domain, with a fixed seed. Image sizes come from Docker registry manifests
(one manifest GET per candidate); images above --max_gb compressed are skipped. Harnesses are
assigned round-robin and about a third of the tasks get a reply-language directive so agent
sessions also carry non-English text.

    venv/bin/python select_tasks.py -o tasks.json
"""
import argparse, json, os, random, urllib.request
import pyarrow.parquet as pq

DS = os.path.expanduser("~/lm-inference/datasets/MiMo-V2.6-RL-oss/")
REPO = "xiaomimimo/mimo-v2.6-rl-oss"
HARNESSES = ["pi", "opencode", "codex"]
LANG = {
    "ru": "Общайся со мной и пиши пояснения на русском языке.",
    "zh": "请用中文与我交流并撰写说明。",
    "es": "Comunícate conmigo y escribe las explicaciones en español.",
}


def get_json(url, headers = None):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers = headers or {}), timeout = 60))


def image_tag(docker_image):
    name, _, tag = docker_image.partition(":")
    if name.startswith("arvo-rl"):
        return tag.replace("v1-arvo-", "arvo-v1-")
    return name


_TOKEN = {}
def image_gb(tag):
    if "t" not in _TOKEN:
        _TOKEN["t"] = get_json(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull")["token"]
    h = {"Authorization": f"Bearer {_TOKEN['t']}",
         "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, "
                   "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"}
    m = get_json(f"https://registry-1.docker.io/v2/{REPO}/manifests/{tag}", h)
    if "manifests" in m:
        d = next(x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64")
        m = get_json(f"https://registry-1.docker.io/v2/{REPO}/manifests/{d['digest']}", h)
    return sum(l["size"] for l in m["layers"]) / 1e9


def load(name, source = None):
    rows = pq.read_table(DS + name).to_pylist()
    return [r for r in rows if source is None or r["data_source"] == source]


def main(args):
    rng = random.Random(args.seed)
    pools = {
        "code": (load("code.parquet"), 12, 3),
        "general_agent": (load("general/train.parquet", "mimoagent/general_agent"), 8, 2),
        "terminal_bench": (load("general/train.parquet", "mimoagent/terminal_bench"), 2, 1),
        "webdev": (load("webdev.parquet"), 10, 3),
        "cyber": (load("cyber.parquet"), 10, 3),
    }
    sized = {}
    out = []
    for domain, (rows, n_trace, n_eval) in pools.items():
        picked = []
        for r in rng.sample(rows, len(rows)):
            if len(picked) >= n_trace + n_eval:
                break
            info = json.loads(r["extra_info"]["instance_json"])
            tag = image_tag(info["docker_image"])
            if tag not in sized:
                sized[tag] = image_gb(tag)
            if sized[tag] > args.max_gb:
                print(f"    skip {domain} {tag} {sized[tag]:.2f} GB")
                continue
            prompt = r["prompt"][-1]["content"]
            picked.append({"domain": domain, "instance_id": info.get("instance_id"),
                           "image": f"{REPO}:{tag}", "image_gb": round(sized[tag], 2),
                           "cwd": info.get("cwd") or "/", "env_task_dir": info.get("env_task_dir"),
                           "prompt": prompt})
        for i, p in enumerate(picked):
            p["split"] = "trace" if i < n_trace else "eval"
        out += picked
    rng.shuffle(out)
    for i, p in enumerate(out):
        p["harness"] = HARNESSES[i % len(HARNESSES)]
        p["lang"] = ["en", "en", "ru", "en", "zh", "en", "es"][i % 7] if p["domain"] != "cyber" else "en"
        p["session"] = f"{p['split']}-{i:03d}-{p['domain']}-{p['harness']}"
        if p["lang"] != "en":
            p["prompt"] = p["prompt"] + "\n\n" + LANG[p["lang"]]
        print(f" -- {p['session']:42} {p['image'].split(':')[1]:28} {p['image_gb']:5.2f} GB {p['lang']}  {p['prompt'][:60]!r}")
    json.dump(out, open(args.out, "w"), ensure_ascii = False, indent = 1)
    print(f" -- {sum(p['split'] == 'trace' for p in out)} trace + {sum(p['split'] == 'eval' for p in out)} eval tasks, "
          f"{len({p['image'] for p in out})} distinct images, "
          f"{sum(sized[p['image'].split(':')[1]] for p in {q['image']: q for q in out}.values()):.1f} GB compressed -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", required = True)
    ap.add_argument("--seed", type = int, default = 20261003)
    ap.add_argument("--max_gb", type = float, default = 3.0)
    main(ap.parse_args())
