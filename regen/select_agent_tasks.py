"""
Agent tasks for the regenerated calibration set, from MiMo-V2.6-RL-oss: a fresh draw that skips
every task of the first set (trace and held-out alike), one session per task, each task at one
reasoning mode. The first set ran 7 tasks as 20 sessions (the same task at several modes), so
sessions shared their early steps; here every session is a different task.

Modes follow the token budget of the set (xhigh 40%, medium 20%, low 15%, off 25%), harnesses go
round-robin (pi, opencode, codex), and about a quarter of the non-cyber tasks ask for replies in
Russian. Task prompts that are themselves Chinese stay as they are and are labelled "zh". Images
above --max_gb compressed are skipped. Workspaces of general-agent tasks are fetched from the Hub
for the selected tasks only.

    venv/bin/python regen/select_agent_tasks.py -o regen/agent_tasks.json
"""
import argparse, glob, json, os, random, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ENV = os.path.join(os.path.dirname(HERE), "agent-env")
sys.path.insert(0, AGENT_ENV)
import select_tasks as S

PER_DOMAIN = {"code": 10, "general_agent": 10, "terminal_bench": 5, "webdev": 8, "cyber": 7}
MODE_WEIGHTS = {"xhigh": 0.40, "medium": 0.20, "low": 0.15, "off": 0.25}
RU = "Общайся со мной и пиши пояснения на русском языке."


def used_instances():
    used = set()
    for path in glob.glob(os.path.join(AGENT_ENV, "tasks*.json")):
        used |= {t.get("instance_id") or t["prompt"][:200] for t in json.load(open(path))}
    return used


def mode_list(n, rng):
    counts = {m: round(w * n) for m, w in MODE_WEIGHTS.items()}
    counts["xhigh"] += n - sum(counts.values())
    modes = [m for m, c in counts.items() for _ in range(c)]
    rng.shuffle(modes)
    return modes


def fetch_workspaces(tasks):
    from huggingface_hub import snapshot_download
    dirs = sorted({t["env_task_dir"] for t in tasks if t.get("env_task_dir")})
    missing = [d for d in dirs if not os.path.isdir(os.path.join(S.DS, "general", d))]
    if missing:
        print(f" -- fetching {len(missing)} task workspaces", flush = True)
        snapshot_download("XiaomiMiMo/MiMo-V2.6-RL-oss", repo_type = "dataset", local_dir = S.DS,
                          allow_patterns = [f"general/{d}/*" for d in missing])


def main(args):
    rng = random.Random(args.seed)
    used = used_instances()
    pools = {
        "code": S.load("code.parquet"),
        "general_agent": S.load("general/train.parquet", "mimoagent/general_agent"),
        "terminal_bench": S.load("general/train.parquet", "mimoagent/terminal_bench"),
        "webdev": S.load("webdev.parquet"),
        "cyber": S.load("cyber.parquet"),
    }
    sized, out = {}, []
    for domain, rows in pools.items():
        picked = []
        for r in rng.sample(rows, len(rows)):
            if len(picked) >= PER_DOMAIN[domain]:
                break
            info = json.loads(r["extra_info"]["instance_json"])
            prompt = r["prompt"][-1]["content"]
            if (info.get("instance_id") or prompt[:200]) in used:
                continue
            tag = S.image_tag(info["docker_image"])
            if tag not in sized:
                try:
                    sized[tag] = S.image_gb(tag)
                except Exception as e:        # some task images are missing from the registry
                    print(f"    skip {domain} {tag}: {e}", flush = True)
                    sized[tag] = float("inf")
            if sized[tag] > args.max_gb:
                continue
            picked.append({"domain": domain, "instance_id": info.get("instance_id"), "image": f"{S.REPO}:{tag}",
                           "image_gb": round(sized[tag], 2), "cwd": info.get("cwd") or "/",
                           "env_task_dir": info.get("env_task_dir"), "prompt": prompt, "split": "trace"})
        out += picked
    rng.shuffle(out)
    modes = mode_list(len(out), rng)
    n_ru = 0
    for i, t in enumerate(out):
        t["harness"] = S.HARNESSES[i % len(S.HARNESSES)]
        t["mode"] = modes[i]
        if len(re.findall(r"[一-鿿]", t["prompt"])) > 20:
            t["lang"] = "zh"
        elif t["domain"] != "cyber" and rng.random() < args.ru_share:
            t["lang"] = "ru"
            t["prompt"] += "\n\n" + RU
            n_ru += 1
        else:
            t["lang"] = "en"
        t["session"] = f"r2-{i:03d}-{t['domain']}-{t['harness']}-eff_{t['mode']}"
        print(f" -- {t['session']:48} {t['image'].split(':')[1]:28} {t['image_gb']:5.2f} GB {t['lang']}  "
              f"{t['prompt'][:50]!r}")
    fetch_workspaces(out)
    json.dump(out, open(args.out, "w"), ensure_ascii = False, indent = 1)
    count = lambda k: {v: sum(t[k] == v for t in out) for v in sorted({t[k] for t in out})}
    print(f" -- {len(out)} tasks {count('domain')} {count('mode')} {count('lang')} {count('harness')}, "
          f"{len({t['image'] for t in out})} images -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", required = True)
    ap.add_argument("--seed", type = int, default = 20261017)
    ap.add_argument("--max_gb", type = float, default = 3.0)
    ap.add_argument("--ru_share", type = float, default = 0.25)
    main(ap.parse_args())
