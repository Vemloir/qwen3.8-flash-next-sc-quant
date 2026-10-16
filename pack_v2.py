"""
Calibration v2: a mixed-reasoning-mode selection from the whole trace pool, packed for convert.py -cd.

Pool, per reasoning mode (xhigh / medium / low / off):
  chat     one row per conversation (its longest turn, which contains the earlier ones): the xhigh
           chat trace, the mixed-mode first turns and the mixed-mode multi-turn conversations
  agent    full agent sessions (build_rows.py rows): the xhigh full-* sessions and the mixed runs
  image    rows with images (trace_images.py calibration file), optional

Per mode a token budget is split between agent, chat and image rows. A chat conversation is used
in at most one mode, agent sessions are taken round-robin over task domains (in random order),
rows are capped at --cols tokens and agent rows outside xhigh at --agent_cap (their first part:
task, early steps), so every domain fits each mode's budget; xhigh starts from its longest session
and gives agents a larger share (--xhigh_agent), keeping long-context rows. Excluded sessions are
skipped. Everything not selected is listed in the manifest
as the held-out pool for evaluation.

    python pack_v2.py --out calib_v2/ [--images trace/mixed/images_cal.safetensors]
"""
import argparse, json, os, random, re
import torch
from safetensors.torch import load_file, save_file

Q = os.path.dirname(os.path.abspath(__file__))
MODES = ["xhigh", "medium", "low", "off"]
BUDGET = {"xhigh": 0.40, "medium": 0.20, "low": 0.15, "off": 0.25}
SPLIT = {"agent": 0.50, "chat": 0.40, "image": 0.10}


def chat_pool():
    """{mode: {conversation: row}} with the longest row per conversation."""
    pool = {m: {} for m in MODES}
    def add(mode, conv, r, src):
        ids = r["input_ids"] + r["response_ids"]
        if conv not in pool[mode] or len(ids) > len(pool[mode][conv]["ids"]):
            pool[mode][conv] = {"key": f"chat:{src}:{conv}:{r['turn']}", "ids": ids, "mode": mode, "src": "chat"}
    for r in json.load(open(f"{Q}/trace/chat_trace_full.json"))["rows"]:
        add("xhigh", r["conversation"], r, "xhigh")
    split = json.load(open(f"{Q}/trace/mixed/multiturn_split.json"))
    for m in ("medium", "low", "off"):
        for r in json.load(open(f"{Q}/trace/mixed/chat_{m}.json"))["rows"]:
            add(m, r["conversation"], r, m)
        # multi-turn runs number their conversations within the --only list
        for r in json.load(open(f"{Q}/trace/mixed/chat_mt_{m}.json"))["rows"]:
            add(m, split[m][r["conversation"]], r, f"mt_{m}")
    return pool


def russian_conversations():
    """Indices (in trace_chat.py's conversation list) of conversations whose first prompt is Russian."""
    src = open(os.path.expanduser("~/lm-inference/exllamav3-1.5.3-patched/sc_trace.py")).read()
    ns = {}
    exec(src[src.index("CONVERSATIONS = ["):src.index("\ncol_default")], ns)
    convs = ns["CONVERSATIONS"] + [tuple(x) for x in json.load(open(f"{Q}/trace/extra_conversations.json"))]
    return {i for i, (seed, _) in enumerate(convs) if re.search("[а-яА-ЯёЁ]", seed)}


def agent_pool(exclude):
    pool = {m: [] for m in MODES}
    for r in json.load(open(f"{Q}/agent-runs/rows_all.json")):
        if r["session"].startswith("full-"):
            pool["xhigh"].append(r)
    for r in json.load(open(f"{Q}/trace/mixed/agent_rows_mixed.json")):
        if r["session"] in exclude:
            continue
        pool[re.search(r"-eff_(\w+)$", r["session"]).group(1)].append(r)
    out = {}
    for m, rows in pool.items():
        out[m] = [{"key": f"agent:{r['session']}", "ids": r["ids"], "mode": m, "src": "agent",
                   "domain": r["session"].split("-")[2]} for r in rows]
    return out


def image_pool(path):
    pool = {m: [] for m in MODES}
    if not path:
        return pool
    data = load_file(path)
    meta = json.load(open(path.replace("_cal.safetensors", "_trace.json")))["rows"]
    for k, r in enumerate(meta):
        n = int(data["lengths"][k])
        pool[r["mode"]].append({"key": f"image:{os.path.basename(r['file'])}", "ids": data["input_ids"][k, :n].tolist(),
                                "mode": r["mode"], "src": "image",
                                "mm": (int(data["mm_start"][k]), int(data["mm_len"][k]), data[f"mm_image_{k}"])})
    return pool


def take(rows, budget, cols, rng, by_domain = False, longest_first = False):
    rows = [dict(r, ids = r["ids"][:cols]) for r in rows]
    rng.shuffle(rows)
    lead = []
    if longest_first and rows:
        # one long-context session up front, so the calibration keeps rows near --cols
        lead = [max(rows, key = lambda r: len(r["ids"]))]
        rows = [r for r in rows if r is not lead[0]]
    if by_domain:
        # round-robin over domains, shortest first within a domain, so one long session cannot eat the budget
        doms = {}
        for r in sorted(rows, key = lambda r: len(r["ids"])):
            doms.setdefault(r["domain"], []).append(r)
        rows = []
        order = sorted(doms)
        rng.shuffle(order)
        while any(doms.values()):
            for d in order:
                if doms[d]:
                    rows.append(doms[d].pop(0))
    rows = lead + rows
    got, total = [], 0
    for r in rows:
        if total >= budget:
            break
        got.append(r)
        total += min(len(r["ids"]), cols)
    return got, total


def main(args):
    rng = random.Random(args.seed)
    exclude = {l.strip() for l in open(f"{Q}/agent-runs-mixed/exclude.txt") if l.strip() and not l.startswith("#")}
    chats, agents, images = chat_pool(), agent_pool(exclude), image_pool(args.images)
    ru = russian_conversations()

    cal, report, used_convs = [], {}, set()
    for m in MODES:
        b = args.budget * BUDGET[m]
        img, ti = take(images[m], b * SPLIT["image"], args.cols, rng)
        if m == "xhigh":
            ag, ta = take(agents[m], b * args.xhigh_agent, args.cols, rng, by_domain = True, longest_first = True)
        else:
            ag, ta = take(agents[m], b * SPLIT["agent"], args.agent_cap, rng, by_domain = True)
        free = {c: r for c, r in chats[m].items() if c not in used_convs}
        # Russian conversations first, a fair share per mode, then the rest
        ru_free = [r for c, r in free.items() if c in ru]
        rng.shuffle(ru_free)
        ru_rows = ru_free[:len(ru) // len(MODES) + 1]
        ch_ru, tr = take(ru_rows, b - ti - ta, args.cols, rng)
        rest = [r for c, r in free.items() if r not in ch_ru]
        ch, tc = take(rest, b - ti - ta - tr, args.cols, rng)
        ch, tc = ch_ru + ch, tr + tc
        used_convs |= {int(r["key"].split(":")[2]) for r in ch}
        cal += img + ag + ch
        report[m] = {"agent": (len(ag), ta), "chat": (len(ch), tc), "image": (len(img), ti)}

    rng.shuffle(cal)
    # convert.py measures its error report on the first 5 rows; keep those short (no chunked path)
    cal.sort(key = lambda r: min(len(r["ids"]), args.cols) > args.ref_max)
    width = max(min(len(r["ids"]), args.cols) for r in cal)
    ids = torch.zeros((len(cal), width), dtype = torch.long)
    lengths = torch.zeros((len(cal),), dtype = torch.long)
    tensors, mm = {}, []
    for i, r in enumerate(cal):
        x = r["ids"][:width]
        ids[i, :len(x)] = torch.tensor(x, dtype = torch.long)
        lengths[i] = len(x)
        if "mm" in r:
            mm.append((i, *r["mm"]))
    tensors.update({"input_ids": ids, "lengths": lengths})
    if mm:
        tensors["mm_row"] = torch.tensor([x[0] for x in mm], dtype = torch.int32)
        tensors["mm_start"] = torch.tensor([x[1] for x in mm], dtype = torch.int32)
        tensors["mm_len"] = torch.tensor([x[2] for x in mm], dtype = torch.int32)
        for k, x in enumerate(mm):
            tensors[f"mm_image_{k}"] = x[3]
    os.makedirs(args.out, exist_ok = True)
    save_file(tensors, os.path.join(args.out, "cal_trace.safetensors"))

    selected = {r["key"] for r in cal}
    held = {m: {"chat": [r["key"] for c, r in chats[m].items() if r["key"] not in selected],
                "agent": [r["key"] for r in agents[m] if r["key"] not in selected],
                "image": [r["key"] for r in images[m] if r["key"] not in selected]} for m in MODES}
    json.dump({"cal": [r["key"] for r in cal], "report": report, "cols": width, "budget": args.budget,
               "seed": args.seed, "excluded": sorted(exclude), "held_out": held},
              open(os.path.join(args.out, "trace_manifest.json"), "w"), indent = 1)
    total = int(lengths.sum())
    for m in MODES:
        r = report[m]
        print(f" -- {m:6}: agent {r['agent'][0]:2} rows {r['agent'][1]:7,}  chat {r['chat'][0]:3} rows {r['chat'][1]:7,}"
              f"  image {r['image'][0]:2} rows {r['image'][1]:6,}")
    print(f" -- {len(cal)} rows, {total:,} tokens, width {width}, {len(mm)} images -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required = True)
    ap.add_argument("--images", default = None)
    ap.add_argument("--budget", type = int, default = 1_000_000)
    ap.add_argument("--cols", type = int, default = 135168)
    ap.add_argument("--agent_cap", type = int, default = 25000, help = "max tokens of an agent row outside xhigh")
    ap.add_argument("--xhigh_agent", type = float, default = 0.6, help = "agent share of the xhigh budget (long sessions)")
    ap.add_argument("--ref_max", type = int, default = 16384)
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
