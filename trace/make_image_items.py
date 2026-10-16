"""
Items for trace_images.py: CoSyn-400K figures and PixMo photos with their dataset questions, and
the generated frontend screenshots with an agent-style review request. Each item gets a reasoning
mode (xhigh 40%, medium 20%, low 15%, off 25%), about a fifth of the dataset questions are asked in
Russian, and every fourth item per source goes to the held-out evaluation set.

    python make_image_items.py --images images/ --pages pages/pages.json -o items_cal.json --holdout items_eval.json
"""
import argparse, json, os, random

MODE_CYCLE = ["xhigh"] * 8 + ["medium"] * 4 + ["low"] * 3 + ["off"] * 5

REVIEW = {
    "en": "This is a screenshot of the page you built for this brief:\n\n{spec}\n\nCompare it with the brief, list "
          "what is missing or visually broken, and explain how to fix each problem.",
    "ru": "Это скриншот страницы, которую ты сверстал по этому ТЗ:\n\n{spec}\n\nСравни её с ТЗ, перечисли, чего не "
          "хватает и что сломано визуально, и объясни, как исправить каждую проблему.",
}


def main(args):
    rnd = random.Random(0)
    sources = []
    cosyn = json.load(open(os.path.join(args.images, "cosyn.json")))
    sources.append([{"file": os.path.abspath(os.path.join(args.images, c["file"])), "source": "cosyn", "category": c["category"],
                     "question": c["questions"][0]} for c in cosyn])
    pixmo = json.load(open(os.path.join(args.images, "pixmo.json")))
    sources.append([{"file": os.path.abspath(os.path.join(args.images, p["file"])), "source": "pixmo",
                     "question": p["questions"][0].strip()} for p in pixmo])
    if args.pages and os.path.exists(args.pages):
        pages = json.load(open(args.pages))
        sources.append([{"file": os.path.abspath(p["file"]), "source": "screenshot", "lang": "ru" if p["lang"] == "ru" else "en",
                         "prompt": REVIEW["ru" if p["lang"] == "ru" else "en"].format(spec = p["spec"])} for p in pages])

    cal, held = [], []
    for items in sources:
        modes = MODE_CYCLE * (len(items) // len(MODE_CYCLE) + 1)
        rnd.shuffle(modes)
        for k, it in enumerate(items):
            it["mode"] = modes[k]
            if "prompt" not in it:
                if rnd.random() < 0.2:
                    it["lang"], it["prompt"] = "ru", "Ответь на русском. " + it["question"]
                else:
                    it["lang"], it["prompt"] = "en", it["question"]
            (held if k % 4 == 3 else cal).append(it)
    json.dump(cal, open(args.output, "w"), ensure_ascii = False, indent = 1)
    json.dump(held, open(args.holdout, "w"), ensure_ascii = False, indent = 1)
    count = lambda xs, key: {v: sum(x[key] == v for x in xs) for v in sorted({x[key] for x in xs})}
    print(f" -- calibration {len(cal)} {count(cal, 'source')} {count(cal, 'mode')} {count(cal, 'lang')}")
    print(f" -- held out {len(held)} {count(held, 'source')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required = True)
    ap.add_argument("--pages", default = None)
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--holdout", required = True)
    main(ap.parse_args())
