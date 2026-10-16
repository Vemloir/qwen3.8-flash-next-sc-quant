"""
Pack the chat trace (sc_trace JSON) and agent session rows (build_rows.py JSON) into:

  cal_trace.safetensors      calibration rows for convert.py -cd: one example per row from
                             position 0, right-padded to --cols, with true lengths
  measure_trace.safetensors  rows for sc_measure.py -tr: the first --m_rows are the KL eval rows
                             (chat + agent sessions whose task content falls inside --m_len
                             tokens), the next --h_rows feed the shaped-noise Hessian capture
  trace_manifest.json        which source rows went where (for publishing / reproducibility)

    python pack.py --chat trace/chat_trace.json --agent agent_rows.json --out trace/
"""
import argparse, json, os, random
import torch
from safetensors.torch import save_file


def pack(rows, cols):
    ids = torch.zeros((len(rows), cols), dtype = torch.long)
    lengths = torch.zeros((len(rows),), dtype = torch.long)
    for i, r in enumerate(rows):
        x = r["ids"][:cols]
        ids[i, :len(x)] = torch.tensor(x, dtype = torch.long)
        lengths[i] = len(x)
    return {"input_ids": ids, "lengths": lengths}


def main(args):
    rng = random.Random(args.seed)
    chat = []
    only = set(json.load(open(args.select))["include"]) if args.select else None
    for i, r in enumerate(json.load(open(args.chat))["rows"]):
        if only is not None and i not in only:
            continue
        chat.append({"src": "chat", "key": f"chat:{i}", "ids": r["input_ids"] + r["response_ids"]})
    agent = []
    if args.agent:
        for r in json.load(open(args.agent)):
            if not r["session"].startswith(args.agent_prefix):
                continue
            agent.append({"src": "agent", "key": f"agent:{r['session']}", "ids": r["ids"],
                          "harness": r["session"].rsplit("-", 1)[-1]})
    rng.shuffle(chat)
    rng.shuffle(agent)

    # Calibration: every agent row, then chat rows until the token budget is reached
    cal, total = [], 0
    for r in agent:
        cal.append(r)
        total += min(len(r["ids"]), args.cols)
    for r in chat:
        if total >= args.budget:
            break
        cal.append(r)
        total += min(len(r["ids"]), args.cols)
    rng.shuffle(cal)
    # convert.py measures its per-module error report on the first 5 rows (num_ref_states); keep those
    # short so they never need the chunked long-row path (stable: the shuffle order is otherwise kept)
    cal.sort(key = lambda r: min(len(r["ids"]), args.cols) > args.ref_max)
    save_file(pack(cal, args.cols), os.path.join(args.out, "cal_trace.safetensors"))
    print(f" -- cal: {len(cal)} rows ({sum(r['src'] == 'agent' for r in cal)} agent), {total:,} tokens, cols {args.cols}")

    # Measurement: eval rows = chat rows + pi sessions (short system prompt, so the task and the
    # agent's first actions fit in m_len); h_rows = a different mix of both
    pi_rows = [r for r in agent if r.get("harness") == "pi"]
    n_agent = min(args.m_agent, len(pi_rows))
    m_eval = chat[:args.m_rows - n_agent] + pi_rows[:n_agent]
    rest = chat[args.m_rows - n_agent:] + [r for r in agent if r not in pi_rows[:n_agent]]
    rng.shuffle(rest)
    m_h = rest[:args.h_rows]
    save_file(pack(m_eval + m_h, args.m_len), os.path.join(args.out, "measure_trace.safetensors"))
    print(f" -- measure: {len(m_eval)} eval rows ({n_agent} agent) + {len(m_h)} Hessian rows, len {args.m_len}")

    json.dump({"cal": [r["key"] for r in cal], "measure_eval": [r["key"] for r in m_eval],
               "measure_h": [r["key"] for r in m_h], "cols": args.cols, "m_len": args.m_len,
               "budget": args.budget, "seed": args.seed,
               "chat_source": args.chat, "chat_selection": args.select, "agent_prefix": args.agent_prefix},
              open(os.path.join(args.out, "trace_manifest.json"), "w"), indent = 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required = True)
    ap.add_argument("--agent", default = None)
    ap.add_argument("--agent_prefix", default = "", help = "only agent sessions whose name starts with this")
    ap.add_argument("--select", default = None, help = "chat selection JSON; only its \"include\" rows are used")
    ap.add_argument("--out", required = True)
    ap.add_argument("--cols", type = int, default = 16384)
    ap.add_argument("--budget", type = int, default = 1_000_000)
    ap.add_argument("--m_rows", type = int, default = 16)
    ap.add_argument("--m_agent", type = int, default = 6)
    ap.add_argument("--m_len", type = int, default = 2048)
    ap.add_argument("--h_rows", type = int, default = 64)
    ap.add_argument("--ref_max", type = int, default = 16384, help = "rows longer than this go after all shorter rows")
    ap.add_argument("--seed", type = int, default = 0)
    main(ap.parse_args())
