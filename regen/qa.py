"""
Checks on the regenerated raw outputs before they become calibration rows. Every problem found in
the first set has a check here:

  reject  status not "ok" (loop stopped, context limit, error), empty answer, reasoning that never
          closed or leaked into the answer, tool-call markup in a chat or image answer, an answer
          in the wrong language (Russian requested, or English expected and not given), a
          follow-up turn whose history lacks the previous answer
  review  repetition below the runaway level (a block repeated many times, or many short repeats
          in a well-compressing tail): music and tables do this legitimately, loops too, so these
          get a human look; very long answers (packing caps their share)

Prints a summary and writes a JSON report with the verdict and a snippet per flagged record; the
packer only takes records with verdict "ok" or reviewed and accepted (--accept key,key,...).

    python regen/qa.py --chat regen/out/chat.jsonl --images regen/out/images.jsonl -o regen/out/qa.json
"""
import argparse, json, re, sys, zlib, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import max_block_repeats

CYR = re.compile("[а-яА-ЯёЁ]")
CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
LETTER = re.compile(r"[^\W\d_]")
TOOL_MARKUP = re.compile(r"<tool_call>|<function=|<parameter=")


def prose(text):
    """Text without code, quotes and blockquotes, which legitimately use another language."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags = re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"«[^»]*»|“[^”]*”|\"[^\"\n]*\"", " ", text)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def share(rx, text):
    """Share of letters matching rx in the prose; None when there is too little prose to judge
    (an answer that is all code)."""
    text = prose(text)
    letters = len(LETTER.findall(text))
    return len(rx.findall(text)) / letters if letters >= 40 else None


def repetition(text):
    tail = text[-12000:]
    if len(tail) < 2000:
        return None
    ratio = len(zlib.compress(tail.encode(), 6)) / len(tail.encode())
    return {"ratio": round(ratio, 3), "rep160": max_block_repeats(tail, 160), "rep40": max_block_repeats(tail, 40)}


def check_turn(t, mode, lang, kind):
    """(reasons_reject, reasons_review) for one generated turn."""
    rej, rev = [], []
    if t["status"] != "ok":
        rej.append(f"status {t['status']}")
        return rej, rev
    ans, rs = t.get("content") or "", t.get("reasoning") or ""
    if not ans.strip():
        rej.append("empty answer")
    if "<think>" in ans or "</think>" in ans:
        rej.append("think markup in answer")
    if mode == "off" and rs.strip():
        rej.append("reasoning in off mode")
    if mode != "off" and not rs.strip():
        rev.append("no reasoning")
    if TOOL_MARKUP.search(ans):
        rej.append("tool-call markup")
    cyr, cjk = share(CYR, ans), share(CJK, ans)
    if lang == "ru" and cyr is not None and cyr < 0.3:
        rej.append(f"Russian requested, answer {cyr:.0%} Cyrillic")
    if lang == "en" and cyr is not None and (cyr > 0.3 or cjk > 0.3):
        rej.append("English expected, answer in another script")
    if lang == "en" and kind == "image" and re.search(r"\b(Voy a|déjame|explicaciones)\b", ans):
        rej.append("English expected, answer in Spanish")
    for name, text in (("answer", ans), ("reasoning", rs)):
        rp = repetition(text)
        if rp and (rp["rep160"] >= 8 or (rp["rep40"] >= 20 and rp["ratio"] < 0.3)):
            rev.append(f"repetition in {name} {rp}")
    toks = (t.get("usage") or {}).get("completion_tokens", 0)
    if toks > 16000:
        rev.append(f"long: {toks} tokens")
    return rej, rev


def snippet(text, n = 300):
    text = text or ""
    return text[:n] + (" ... " + text[-n:] if len(text) > 2 * n else text[n:])


def main(args):
    report = []
    accepted = set(args.accept.split(",")) if args.accept else set()
    for kind, path in (("chat", args.chat), ("image", args.images)):
        if not path:
            continue
        for line in open(path):
            if not line.strip():
                continue
            rec = json.loads(line)
            turns = rec["turns"] if kind == "chat" else [rec]
            lang = rec.get("lang")
            if kind == "chat" and lang == "other":
                lang = None                      # sc_trace prompts in many languages; no check
            rej, rev = [], []
            if kind == "chat" and rec["status"] != "ok":
                rej.append(f"conversation stopped: {rec['status']}")
            for i, t in enumerate(turns):
                a, b = check_turn(t, rec["mode"], lang, kind)
                rej += [f"turn {i}: {x}" for x in a]
                rev += [f"turn {i}: {x}" for x in b]
            verdict = "reject" if rej else ("review" if rev and rec["key"] not in accepted else "ok")
            entry = {"kind": kind, "key": rec["key"], "verdict": verdict, "reject": rej, "review": rev}
            if verdict != "ok":
                last = turns[-1]
                entry["answer"] = snippet(last.get("content"))
                entry["reasoning"] = snippet(last.get("reasoning"))
            report.append(entry)
    json.dump(report, open(args.output, "w"), ensure_ascii = False, indent = 1)
    for kind in ("chat", "image"):
        r = [e for e in report if e["kind"] == kind]
        if not r:
            continue
        counts = {v: sum(e["verdict"] == v for e in r) for v in ("ok", "review", "reject")}
        print(f" -- {kind}: {len(r)} records {counts}")
        reasons = {}
        for e in r:
            for x in e["reject"]:
                k = re.sub(r"turn \d+: |\{.*|\d+%.*|\d+ tokens", "", x).strip()
                reasons[k] = reasons.get(k, 0) + 1
        for k, v in sorted(reasons.items(), key = lambda kv: -kv[1]):
            print(f"      reject: {k} x{v}")
        for e in r:
            if e["verdict"] == "review":
                print(f"      review {e['key']}: {'; '.join(e['review'])[:160]}")
    print(f" -- report -> {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat")
    ap.add_argument("--images")
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--accept", default = "", help = "reviewed keys accepted by hand, comma-separated")
    main(ap.parse_args())
