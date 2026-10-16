"""
Run agent-harness sessions on MiMo tasks inside their task containers, against the local model
behind trace_proxy.py, for calibration traces.

Per task: pull the image, start the container (harnesses, Node and a fresh per-session HOME are
bind-mounted read-only/rw; host networking so the harness reaches the proxy on 127.0.0.1), run
the harness non-interactively, and stop the session when the proxy reports a prompt of
--cap_tokens or more, when the harness exits, or after --max_minutes. Harnesses never run on the
host. Results go to <runs>/results.jsonl; the proxy logs are the actual trace.

    python run_agents.py --tasks tasks.json --runs ../agent-runs --split trace --parallel 2
"""
import argparse, json, os, shutil, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_cfg as H

MIMO = "/home/daniil/lm-inference/datasets/MiMo-V2.6-RL-oss/general"
TOKENIZER = "/home/daniil/lm-inference/models/Qwen3.8-Flash-Next/EXL3-4.05"
LOCK = threading.Lock()
PULLED = {}


def sh(*cmd, timeout = None):
    return subprocess.run(cmd, capture_output = True, text = True, timeout = timeout)


def pull(image):
    with LOCK:
        ev = PULLED.get(image)
        if ev is None:
            ev = PULLED[image] = threading.Event()
            owner = True
        else:
            owner = False
    if owner:
        if sh("docker", "image", "inspect", image).returncode != 0:
            for attempt in range(5):
                r = sh("docker", "pull", "-q", image)
                if r.returncode == 0:
                    break
                print(f"    pull {image} attempt {attempt}: {r.stderr.strip()[:200]}", flush = True)
                time.sleep(60)
        ev.set()
    ev.wait()
    return sh("docker", "image", "inspect", image).returncode == 0


def proxy_status(proxy, session):
    try:
        st = json.load(urllib.request.urlopen(f"{proxy}/_status", timeout = 10))
        return st.get(session, {})
    except Exception:
        return {}


_TOK = {}
def logged_prompt_tokens(runs, session):
    """Exact prompt size of the session's last logged request (chat template + tools), for
    harnesses whose streams carry no usage block (codex 0.80 chat)."""
    path = os.path.join(runs, "logs", f"{session}.jsonl")
    if not os.path.exists(path):
        return 0
    last = None
    for line in open(path):
        if line.strip():
            last = line
    if last is None:
        return 0
    try:
        req = json.loads(last)["request"]
        if "tok" not in _TOK:
            from transformers import AutoTokenizer
            _TOK["tok"] = AutoTokenizer.from_pretrained(TOKENIZER)
        from build_rows import normalize
        ids = _TOK["tok"].apply_chat_template(normalize(req["messages"]), tools = req.get("tools"),
                                               add_generation_prompt = True, tokenize = True,
                                               preserve_thinking = True)
        return len(ids["input_ids"] if hasattr(ids, "input_ids") else ids)
    except Exception as e:
        print(f"    token count failed for {session}: {e}", flush = True)
        return 0


def container_env_path(image):
    r = sh("docker", "image", "inspect", "-f", "{{json .Config.Env}}", image)
    for kv in json.loads(r.stdout or "[]") or []:
        if kv.startswith("PATH="):
            return kv[5:]
    return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def run_task(t, args, remaining_uses):
    session = t["session"]
    name = "sct_" + session.replace("/", "_")
    res = {"session": session, "domain": t["domain"], "harness": t["harness"], "image": t["image"], "t0": time.time()}
    sh("docker", "rm", "-f", name)
    if not pull(t["image"]):
        res["error"] = "pull failed"
        return res

    harness = t["harness"]
    # Containers run on the default bridge network (not --network host, so they never share the
    # host's network namespace with the VPN daemon) and reach the proxy via the host gateway
    base_url = f"http://host.docker.internal:{args.proxy_port}/{session}/v1"
    homes = os.path.join(args.runs, "homes")
    home = H.make_home(harness, session, base_url, homes)
    mounts = ["-v", f"{H.AGENT_ENV}/node:/opt/node:ro",
              "-v", f"{H.AGENT_ENV}/agents:/opt/agents:ro",
              "-v", f"{H.AGENT_ENV}/agents-codex080:/opt/agents-codex080:ro",
              "-v", f"{home}:/agenthome"]
    cwd = t.get("cwd") or "/"
    if t.get("env_task_dir"):
        ws = os.path.join(args.runs, "workspaces", session)
        if os.path.exists(ws):
            shutil.rmtree(ws)
        shutil.copytree(os.path.join(MIMO, t["env_task_dir"], "workspace"), ws)
        mounts += ["-v", f"{ws}:/work/workspace"]
        cwd = "/work/workspace"
    r = sh("docker", "run", "-d", "--name", name, "--add-host", "host.docker.internal:host-gateway",
           "--memory", args.mem, "--memory-swap", args.mem, "--pids-limit", "4096", "--cpus", str(args.cpus),
           "--entrypoint", "sleep", *mounts, t["image"], "infinity")
    if r.returncode != 0:
        res["error"] = "run failed: " + r.stderr.strip()[:300]
        return res
    try:
        sh("docker", "exec", name, "mkdir", "-p", cwd)
        # Node-based harnesses need a glibc the bundled Node accepts; codex is a static musl binary
        if harness != "codex" and sh("docker", "exec", name, "/opt/node/bin/node", "--version").returncode != 0:
            res["fallback"] = f"{harness}->codex (node unusable in image)"
            harness = "codex"
            home = H.make_home(harness, session, base_url, homes)
        res["harness_used"] = harness
        agents = "/opt/agents-codex080/bin" if harness == "codex" else "/opt/agents/bin"
        env = H.env(harness, "/agenthome")
        env["PATH"] = f"{agents}:/opt/node/bin:" + container_env_path(t["image"])
        env_args = sum((["-e", f"{k}={v}"] for k, v in env.items()), [])
        cmd = ["docker", "exec", "-w", cwd, *env_args, name, "timeout", str(args.max_minutes * 60),
               *H.command(harness, t["prompt"])]
        log = open(os.path.join(args.runs, "harness_logs", f"{session}.log"), "w")
        p = subprocess.Popen(cmd, stdout = log, stderr = subprocess.STDOUT, stdin = subprocess.DEVNULL)
        reason = None
        n_counted, est_tokens = 0, 0
        t_start = time.time()
        while reason is None:
            time.sleep(5)
            st = proxy_status(args.proxy, session)
            if st and not st.get("max_prompt_tokens") and st.get("requests", 0) > n_counted:
                n_counted = st["requests"]
                est_tokens = logged_prompt_tokens(args.runs, session)
            if max(st.get("max_prompt_tokens", 0), est_tokens) >= args.cap_tokens:
                reason = "cap"
            elif p.poll() is not None:
                reason = f"exit {p.returncode}"
            # the proxy keeps per-session state across runs; a rerun session must not inherit the
            # previous attempt's last-call time
            elif st and st.get("inflight", 0) == 0 and \
                    time.time() - max(st.get("last", 0), t_start) > args.idle_minutes * 60:
                reason = "idle"
            elif time.time() - t_start > args.max_minutes * 60 + 30:
                reason = "timeout"
        res.update({"stop": reason, "requests": st.get("requests", 0),
                    "max_prompt_tokens": max(st.get("max_prompt_tokens", 0), est_tokens), "minutes": round((time.time() - t_start) / 60, 1)})
    finally:
        sh("docker", "rm", "-f", name)
        with LOCK:
            remaining_uses[t["image"]] -= 1
            drop = remaining_uses[t["image"]] == 0 and t["image"] not in args.keep_images
        if drop:
            sh("docker", "rmi", t["image"])
    return res


def main(args):
    os.makedirs(os.path.join(args.runs, "harness_logs"), exist_ok = True)
    tasks = [t for t in json.load(open(args.tasks)) if args.split in (t["split"], "all")]
    done = set()
    results_path = os.path.join(args.runs, "results.jsonl")
    if os.path.exists(results_path):
        done = {json.loads(l)["session"] for l in open(results_path) if l.strip()}
    tasks = [t for t in tasks if t["session"] not in done]
    if args.limit:
        tasks = tasks[:args.limit]
    # Shared images first-come; per-task images are pulled one task ahead by the prefetcher
    remaining = {}
    for t in tasks:
        remaining[t["image"]] = remaining.get(t["image"], 0) + 1
    print(f" -- {len(tasks)} sessions ({len(done)} already done), parallel {args.parallel}", flush = True)

    def prefetch():
        for t in tasks:
            pull(t["image"])
    threading.Thread(target = prefetch, daemon = True).start()

    with ThreadPoolExecutor(args.parallel) as ex:
        for res in ex.map(lambda t: run_task(t, args, remaining), tasks):
            res["t1"] = time.time()
            with LOCK, open(results_path, "a") as f:
                f.write(json.dumps(res) + "\n")
            print(f" -- {res['session']:42} {res.get('harness_used', res['harness']):9} "
                  f"{res.get('stop', res.get('error'))!s:10} req={res.get('requests', 0):3} "
                  f"prompt={res.get('max_prompt_tokens', 0):6} {res.get('minutes', 0)} min "
                  f"{res.get('fallback', '')}", flush = True)
    print(" -- ALL DONE", time.strftime("%H:%M:%S"), flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required = True)
    ap.add_argument("--runs", required = True)
    ap.add_argument("--split", default = "trace", choices = ["trace", "eval", "all"])
    ap.add_argument("--parallel", type = int, default = 2)
    ap.add_argument("--limit", type = int, default = 0)
    ap.add_argument("--cap_tokens", type = int, default = 12000)
    ap.add_argument("--max_minutes", type = int, default = 25)
    ap.add_argument("--idle_minutes", type = float, default = 6, help = "stop when the harness has not called the model for this long")
    ap.add_argument("--proxy", default = "http://127.0.0.1:1338")
    ap.add_argument("--proxy_port", type = int, default = 1338)
    ap.add_argument("--keep_images", nargs = "*", default = [])
    ap.add_argument("--mem", default = "6g", help = "per-container memory cap (no swap)")
    ap.add_argument("--cpus", type = float, default = 8)
    main(ap.parse_args())
