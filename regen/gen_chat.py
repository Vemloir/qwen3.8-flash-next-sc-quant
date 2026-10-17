"""
Chat part of the regenerated calibration set: every conversation at every reasoning mode, sampled
from the FP4-Local server, follow-up turns included. Writes one JSONL record per (conversation,
mode) with the raw turns; rows are built and checked later (build_chat_rows.py, qa.py).

Conversations: exllamav3 sc_trace.py's seed list, trace/extra_conversations.json (Russian and
English, some multi-turn), regen/prompts_ru.json (Russian everyday, study and work topics, added
because the first set had 28 Russian conversations of 207), trace/music_prompts.json and
regen/prompts_science.json (biology, chemistry, physics, medicine, earth sciences in English and
Russian: the other sources had about 20 natural-science conversations).

History turns carry the previous answer only, without its reasoning, as chat front ends send it;
the chat template then renders an empty think block for them (preserve_thinking default). A
conversation stops at the first turn that does not end normally (loop, context limit, error).

    python regen/gen_chat.py --base http://127.0.0.1:18000 -o regen/out/chat.jsonl --parallel 24
"""
import argparse, asyncio, json, os, random, sys
import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODES, Store, api_key, chat_stream, has_cyrillic

Q = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SC_TRACE = os.path.expanduser("~/lm-inference/exllamav3-1.5.3-patched/sc_trace.py")


def conversations():
    src = open(SC_TRACE).read()
    ns = {}
    exec(src[src.index("CONVERSATIONS = ["):src.index("\ncol_default")], ns)
    out = [{"source": "sc_trace", "turns": [seed] + list(fu)} for seed, fu in ns["CONVERSATIONS"]]
    out += [{"source": "own", "turns": [seed] + list(fu)} for seed, fu in json.load(open(f"{Q}/trace/extra_conversations.json"))]
    out += [{"source": "own_ru", "turns": [seed] + list(fu)} for seed, fu in json.load(open(f"{Q}/regen/prompts_ru.json"))]
    out += [{"source": "music", "turns": [m["prompt"]]} for m in json.load(open(f"{Q}/trace/music_prompts.json"))]
    # appended last, so earlier conversations keep their indices (records are keyed by them)
    out += [{"source": "science", "turns": [seed] + list(fu)}
            for seed, fu in json.load(open(f"{Q}/regen/prompts_science.json"))]
    for i, c in enumerate(out):
        c["conv"] = i
        c["lang"] = "ru" if has_cyrillic(c["turns"][0]) else "other"
    return out


async def run_one(session, args, key, store, conv, mode, sem):
    rec_key = f"{conv['conv']}:{mode}"
    if store.has(rec_key):
        return
    async with sem:
        messages, turns, status = [], [], "ok"
        for user in conv["turns"]:
            messages.append({"role": "user", "content": user})
            r = await chat_stream(session, args.base, key, messages, mode)
            turns.append({"user": user, **r})
            status = r["status"]
            if status != "ok":
                break
            messages.append({"role": "assistant", "content": r["content"]})
        await store.put({"key": rec_key, "conv": conv["conv"], "mode": mode, "source": conv["source"],
                         "lang": conv["lang"], "status": status, "turns": turns})
        toks = sum((t.get("usage") or {}).get("completion_tokens", 0) for t in turns)
        print(f" -- {rec_key:10} {conv['source']:8} {status:6} {len(turns)} turn(s) {toks:6} tokens "
              f"{sum(t.get('seconds', 0) for t in turns):7.1f} s", flush = True)


async def main(args):
    key = api_key()
    convs = conversations()
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        convs = [c for c in convs if c["conv"] in keep]
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok = True)
    store = Store(args.output)
    if args.per_conv:
        # up to --per_conv modes per conversation: keep the modes already done, fill the rest from a
        # rotation that keeps the modes balanced, and run every conversation's first mode before any
        # second one
        jobs = []
        for c in convs:
            have = [m for m in args.modes if store.has(f"{c['conv']}:{m}")]
            rot = [args.modes[(c["conv"] + k * 2 + k // 2) % len(args.modes)] for k in range(len(args.modes))]
            want = have + [m for m in rot if m not in have][:max(0, args.per_conv - len(have))]
            jobs += [(len(have) + k, c, m) for k, m in enumerate(m for m in want if m not in have)]
        rnd = random.Random(0)
        rnd.shuffle(jobs)
        jobs = [(c, m) for _, c, m in sorted(jobs, key = lambda j: j[0])]
    else:
        jobs = [(c, m) for c in convs for m in args.modes]
        random.Random(0).shuffle(jobs)      # mix long and short jobs across the batch
    if args.limit:
        jobs = jobs[:args.limit]
    print(f" -- {len(convs)} conversations x {len(args.modes)} modes = {len(jobs)} jobs, "
          f"{sum(store.has(f'{c['conv']}:{m}') for c, m in jobs)} done", flush = True)
    sem = asyncio.Semaphore(args.parallel)
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(run_one(session, args, key, store, c, m, sem) for c, m in jobs))
    print(" -- ALL DONE", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default = "http://127.0.0.1:18000")
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--modes", nargs = "+", default = list(MODES), choices = list(MODES))
    ap.add_argument("--parallel", type = int, default = 24)
    ap.add_argument("--per_conv", type = int, default = 0, help = "modes per conversation (0: all)")
    ap.add_argument("--only", default = None, help = "comma-separated conversation indices")
    ap.add_argument("--limit", type = int, default = 0, help = "first N jobs only (smoke test)")
    asyncio.run(main(ap.parse_args()))
