"""
Image part of the regenerated calibration set: CoSyn figures and PixMo photos with their dataset
questions, and the frontend screenshots with a review request, answered by the FP4-Local server.
Writes one JSONL record per item; rows (with the image token block laid out by exllamav3's vision
preprocessing) are built locally afterwards.

Fixes over the first set: the screenshot briefs came from agent tasks and two still carried the
agent-session language directive ("Comunícate conmigo ... en español"), so the model answered in
Spanish as an agent and called tools it did not have. Directives are stripped from the briefs, the
answer language is set by the request itself, the request says only the screenshot is available,
and no tools are sent.

    python regen/gen_images.py --base http://127.0.0.1:18000 -o regen/out/images.jsonl --parallel 16
"""
import argparse, asyncio, base64, json, mimetypes, os, random, re, sys
import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODES, Store, api_key, chat_stream

Q = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = os.path.join(Q, "images")
MODE_CYCLE = ["xhigh"] * 8 + ["medium"] * 4 + ["low"] * 3 + ["off"] * 5
# directives select_tasks.py appended to agent prompts
LANG_DIRECTIVE = re.compile(r"\n*(Общайся со мной и пиши пояснения на русском языке\.|请用中文与我交流并撰写说明。|"
                            r"Comunícate conmigo y escribe las explicaciones en español\.)\s*$")
REVIEW = {
    "en": "This is a screenshot of the page you built for this brief:\n\n{spec}\n\nYou only have the screenshot, not "
          "the code. Compare it with the brief, list what is missing or visually broken, and explain how to fix each "
          "problem.",
    "ru": "Это скриншот страницы, которую ты сверстал по этому ТЗ:\n\n{spec}\n\nКода у тебя нет, только скриншот. "
          "Сравни страницу с ТЗ, перечисли, чего не хватает и что сломано визуально, и объясни, как исправить каждую "
          "проблему.",
}


def items(ru_share):
    rnd = random.Random(0)
    out = []
    for c in json.load(open(os.path.join(IMAGES, "cosyn.json"))):
        out.append({"id": os.path.basename(c["file"]), "file": os.path.join(IMAGES, c["file"]), "source": "cosyn",
                    "category": c["category"], "question": c["questions"][0]})
    for p in json.load(open(os.path.join(IMAGES, "pixmo.json"))):
        out.append({"id": os.path.basename(p["file"]), "file": os.path.join(IMAGES, p["file"]), "source": "pixmo",
                    "question": p["questions"][0].strip()})
    for p in json.load(open(os.path.join(IMAGES, "pages", "pages.json"))):
        spec = LANG_DIRECTIVE.sub("", p["spec"]).strip()
        lang = "ru" if p["lang"] == "ru" else "en"
        out.append({"id": os.path.basename(p["file"]), "file": p["file"], "source": "screenshot", "lang": lang,
                    "prompt": REVIEW[lang].format(spec = spec)})
    by_source = {}
    for it in out:
        by_source.setdefault(it["source"], []).append(it)
    for group in by_source.values():
        modes = MODE_CYCLE * (len(group) // len(MODE_CYCLE) + 1)
        rnd.shuffle(modes)
        for k, it in enumerate(group):
            it["mode"] = modes[k]
            if "prompt" not in it:
                it["lang"] = "ru" if rnd.random() < ru_share else "en"
                it["prompt"] = ("Ответь на русском. " if it["lang"] == "ru" else "") + it["question"]
    return out


def image_part(path):
    mime = mimetypes.guess_type(path)[0] or "image/png"
    data = base64.b64encode(open(path, "rb").read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


async def run_one(session, args, key, store, it, sem):
    rec_key = f"{it['id']}:{it['mode']}"
    if store.has(rec_key):
        return
    async with sem:
        messages = [{"role": "user", "content": [image_part(it["file"]), {"type": "text", "text": it["prompt"]}]}]
        r = await chat_stream(session, args.base, key, messages, it["mode"])
        await store.put({"key": rec_key, **it, **r})
        toks = (r.get("usage") or {}).get("completion_tokens", 0)
        print(f" -- {rec_key:60} {it['source']:10} {it['lang']} {r['status']:6} {toks:6} tokens "
              f"{r.get('seconds', 0):7.1f} s", flush = True)


async def main(args):
    key = api_key()
    its = items(args.ru_share)
    if args.limit:
        its = its[:args.limit]
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok = True)
    store = Store(args.output)
    print(f" -- {len(its)} items, {sum(store.has(f'{i['id']}:{i['mode']}') for i in its)} done", flush = True)
    sem = asyncio.Semaphore(args.parallel)
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(run_one(session, args, key, store, it, sem) for it in its))
    print(" -- ALL DONE", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default = "http://127.0.0.1:18000")
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--parallel", type = int, default = 16)
    ap.add_argument("--ru_share", type = float, default = 0.2)
    ap.add_argument("--limit", type = int, default = 0)
    asyncio.run(main(ap.parse_args()))
