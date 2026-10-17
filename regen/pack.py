"""
Calibration set 2: a selection from the regenerated rows (build_chat_rows.py, build_image_rows.py,
agent-env/build_rows.py), packed for the patched convert.py -cd.

  budget   --budget tokens, split by reasoning mode over all parts (xhigh 40%, medium 20%, low 15%,
           off 25%); agents get --agent_share of it, every image row is taken, chat fills the rest
  agents   whole sessions, never cut: rows over --max_row or with a model-text share below
           --min_model are left out; the rest are taken by model-text share, round-robin over task
           domains, until the agent budget is used
  chat     one reasoning mode per conversation (no prompt appears twice), chosen to fill each
           mode's remaining budget, Russian conversations first; rows over --chat_cap only when
           the conversation has no shorter mode
  order    the first rows are short (convert.py reports its error on the first five)

Writes cal_trace.safetensors ("input_ids" padded, "lengths", per image "mm_row", "mm_start",
"mm_len", "mm_image_<k>" file bytes) and trace_manifest.json (selected keys, report, held out).

    python regen/pack.py --out calib_v3/
"""
import argparse, json, os, random, re
import torch
from safetensors.torch import save_file

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SHARE = {"xhigh": 0.40, "medium": 0.20, "low": 0.15, "off": 0.25}


def model_share(tok, ids):
    text = tok.decode(ids)
    parts = text.split("<|im_start|>")
    return sum(len(p) for p in parts if p.startswith("assistant")) / max(1, len(text))


def main(args):
    rng = random.Random(args.seed)
    chats = json.load(open(f"{OUT}/chat_rows.json"))
    images = json.load(open(f"{OUT}/image_rows.json"))
    agents = json.load(open(f"{OUT}/agent_rows.json"))
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.tokenizer)

    target = {m: args.budget * s for m, s in SHARE.items()}
    used = {m: 0 for m in SHARE}
    sel = []

    # images: all of them
    for r in images:
        sel.append(dict(r, src = "image"))
        used[r["mode"]] += len(r["ids"])
    n_img = len(sel)

    # agents
    pool, take_forced = [], []
    for r in agents:
        m = re.search(r"-eff_(\w+)$", r["session"]).group(1)
        share = model_share(tok, r["ids"])
        r = {"key": f"agent:{r['session']}", "ids": r["ids"], "mode": m, "domain": r["session"].split("-")[2],
             "model_share": round(share, 3), "src": "agent"}
        if r["key"] in args.force:
            take_forced.append(r)
            continue
        if len(r["ids"]) > args.max_row or share < args.min_model:
            print(f"    agent left out: {r['key']} {len(r['ids'])} tokens, model share {share:.1%}")
            continue
        pool.append(r)
    agent_budget, agent_used = args.budget * args.agent_share, 0
    agent_mode = {m: 0 for m in SHARE}
    def take_agent(r):
        nonlocal agent_used
        sel.append(r); used[r["mode"]] += len(r["ids"]); agent_mode[r["mode"]] += len(r["ids"])
        agent_used += len(r["ids"])
    for r in take_forced:               # --force: whatever the filters say (e.g. the longest session)
        print(f"    agent forced in: {r['key']} {len(r['ids'])} tokens, model share {r['model_share']:.1%}")
        take_agent(r)
    # one long-context session (the one with the most model text), then the rest with long rows last,
    # so the budget covers many tasks; agents take at most --agent_mode_max of any mode's budget
    longs = [r for r in pool if len(r["ids"]) > args.long_row]
    if longs:
        lead = max(longs, key = lambda r: r["model_share"])
        take_agent(lead); pool = [r for r in pool if r is not lead]
    doms = {}
    for r in sorted(pool, key = lambda r: (len(r["ids"]) > args.long_row, -r["model_share"])):
        doms.setdefault(r["domain"], []).append(r)
    order = sorted(doms); rng.shuffle(order)
    while agent_used < agent_budget and any(doms.values()):
        for d in order:
            while doms[d]:
                r = doms[d].pop(0)
                if agent_mode[r["mode"]] + len(r["ids"]) <= target[r["mode"]] * args.agent_mode_max:
                    if agent_used < agent_budget:
                        take_agent(r)
                    break

    # chat: one mode per conversation
    by_conv = {}
    for r in chats:
        by_conv.setdefault(r["conv"], []).append(dict(r, src = "chat"))
    convs = list(by_conv)
    rng.shuffle(convs)
    convs.sort(key = lambda c: by_conv[c][0]["lang"] != "ru")       # Russian first
    chat_budget = args.budget - sum(used.values())
    chat_used = 0
    for c in convs:
        if chat_used >= chat_budget:
            break
        cands = by_conv[c]
        short = [r for r in cands if len(r["ids"]) <= args.chat_cap]
        cands = short or cands
        open_ = [r for r in cands if target[r["mode"]] - used[r["mode"]] > 0]
        if not open_:
            continue
        if cands[0]["lang"] == "ru":
            # Russian: the longest open mode, to raise the Russian share
            r = max(open_, key = lambda r: len(r["ids"]))
        else:
            # the mode with the largest remaining deficit, relative to its target
            r = max(open_, key = lambda r: (target[r["mode"]] - used[r["mode"]]) / target[r["mode"]])
        sel.append(r); used[r["mode"]] += len(r["ids"]); chat_used += len(r["ids"])

    rng.shuffle(sel)
    sel.sort(key = lambda r: len(r["ids"]) > args.ref_max)
    width = max(len(r["ids"]) for r in sel)
    ids = torch.zeros((len(sel), width), dtype = torch.long)
    lengths = torch.zeros((len(sel),), dtype = torch.long)
    mm = []
    for i, r in enumerate(sel):
        ids[i, :len(r["ids"])] = torch.tensor(r["ids"], dtype = torch.long)
        lengths[i] = len(r["ids"])
        if r["src"] == "image":
            mm.append((i, r["mm_start"], r["mm_len"], open(r["file"], "rb").read()))
    tensors = {"input_ids": ids, "lengths": lengths,
               "mm_row": torch.tensor([x[0] for x in mm], dtype = torch.int32),
               "mm_start": torch.tensor([x[1] for x in mm], dtype = torch.int32),
               "mm_len": torch.tensor([x[2] for x in mm], dtype = torch.int32)}
    for k, x in enumerate(mm):
        tensors[f"mm_image_{k}"] = torch.frombuffer(bytearray(x[3]), dtype = torch.uint8)
    os.makedirs(args.out, exist_ok = True)
    save_file(tensors, os.path.join(args.out, "cal_trace.safetensors"))

    total = int(lengths.sum())
    rep = {}
    for r in sel:
        k = (r["src"], r["mode"])
        rep.setdefault(k, [0, 0]); rep[k][0] += 1; rep[k][1] += len(r["ids"])
    keys = {r["key"] for r in sel}
    tasks = {t["session"]: t["lang"] for t in json.load(open(os.path.join(HERE, "agent_tasks.json")))}
    for r in sel:
        if r["src"] == "agent":
            r["lang"] = tasks.get(r["key"].split(":", 1)[1], "en")
    ru = sum(len(r["ids"]) for r in sel if r.get("lang") == "ru")
    manifest = {"cal": [r["key"] for r in sel], "cols": width, "budget": args.budget, "seed": args.seed,
                "tokens": total, "russian_tokens": ru,
                "report": {f"{s}:{m}": v for (s, m), v in sorted(rep.items())},
                "agents": {r["key"]: r["model_share"] for r in sel if r["src"] == "agent"},
                "held_out": sorted([r["key"] for r in chats if r["key"] not in keys] +
                                   [f"agent:{r['session']}" for r in agents if f"agent:{r['session']}" not in keys])}
    json.dump(manifest, open(os.path.join(args.out, "trace_manifest.json"), "w"), indent = 1)
    for m in SHARE:
        parts = "  ".join(f"{s} {rep.get((s, m), [0, 0])[0]:3} rows {rep.get((s, m), [0, 0])[1]:8,}"
                          for s in ("agent", "chat", "image"))
        mt = sum(rep.get((s, m), [0, 0])[1] for s in ("agent", "chat", "image"))
        print(f" -- {m:6} {mt / total:5.1%} (target {SHARE[m]:.0%}): {parts}")
    print(f" -- {len(sel)} rows ({n_img} with images), {total:,} tokens, width {width}, "
          f"Russian {ru / total:.1%} -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required = True)
    ap.add_argument("--tokenizer", default = os.path.expanduser("~/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.05-v3"))
    ap.add_argument("--budget", type = int, default = 2_000_000)
    ap.add_argument("--agent_share", type = float, default = 0.45)
    ap.add_argument("--agent_mode_max", type = float, default = 0.6, help = "max agent share of one mode's budget")
    ap.add_argument("--long_row", type = int, default = 100_000)
    ap.add_argument("--max_row", type = int, default = 160_000)
    ap.add_argument("--min_model", type = float, default = 0.08)
    ap.add_argument("--chat_cap", type = int, default = 32_768)
    ap.add_argument("--ref_max", type = int, default = 16384)
    ap.add_argument("--force", nargs = "*", default = [], help = "agent keys to include regardless of the filters")
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
