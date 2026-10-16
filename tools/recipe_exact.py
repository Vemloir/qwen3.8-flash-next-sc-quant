"""
Trim an sc_optimize.py recipe to an exact mean bitrate.

The optimizer allocates MoE experts per layer (one group per layer, ~2% of the budget each), so it
lands a few thousandths off the target. Experts of one layer must share one width (the CPU MoE
kernels reject mixed per-expert widths) and only integer widths or 1.5/2.5/3.5 exist, so the fine
adjustment is done on the small dense tensors instead:

  1. below target: promote the most sensitive experts layer from K to K + 0.5 (K in 1, 2, 3)
  2. above target: demote dense tensors (tie groups together) one bit at a time, cheapest
     predicted KLD increase per bit first, while the mean stays at or above the target

Predicted cost uses the floor-fitted sensitivities: kld = S * rfn(K)^2 with rfn(K) anchored on
the rfn probe and an amplitude ratio of 1.96 per bit (sc_optimize.py defaults).

    python recipe_exact.py --recipe in.yaml --measurement measure_fitted.json --rfn rfn.json \\
        --target 4.0 -o out.yaml
"""
import argparse, json, re

RATIO = 1.96
TIES = [("q_proj", "k_proj", "v_proj"), ("gate_proj", "up_proj"), ("in_proj_qkv", "in_proj_z")]


def load_recipe(path):
    head, tensors = [], {}
    for line in open(path):
        m = re.match(r"  (\S+): ([\d.]+)$", line)
        if m:
            tensors[m.group(1)] = float(m.group(2))
        elif not line.startswith("  "):
            head.append(line)
    return head, tensors


def main(args):
    head, rec = load_recipe(args.recipe)
    probe = {r["key"]: r for r in json.load(open(args.rfn))["results"]}
    meas = json.load(open(args.measurement))["results"]
    numel = {k: probe[k]["numel"] for k in rec}
    total = sum(numel.values())
    bits = lambda: sum(rec[k] * numel[k] for k in rec) / total
    start = bits()

    # sensitivity and anchor per measured target, mapped to its member tensors
    S, members = {}, {}
    for r in meas:
        if r["qbits_key"] != "bits":
            continue
        S[r["key"]] = r["floor_fit"]["S"]
        members[r["key"]] = r.get("members", [r["key"]])

    def rfn(key, K):
        a = probe[key]
        return a["rfn"] * RATIO ** (a["K"] - K)

    def cost(target, K):
        return sum(S[target] * rfn(m, K) ** 2 for m in members[target] if m in rec)

    log = []
    # 1. promote one experts layer by half a bit if below target
    if bits() < args.target:
        cands = []
        for t, ms in members.items():
            if t.endswith(".experts"):
                k = rec[ms[0]]
                if k in (1.0, 2.0, 3.0):
                    gain = cost(t, k) - cost(t, k + 0.5)
                    cands.append((gain / sum(numel[m] for m in ms), t, k))
        cands.sort(reverse = True)
        for _, t, k in cands:
            if bits() >= args.target:
                break
            for m in members[t]:
                rec[m] = k + 0.5
            log.append(f"promote {t}: {k} -> {k + 0.5}")

    # 2. demote dense tensors one bit at a time while staying at or above target
    def tie_group(key):
        base, last = key.rsplit(".", 1)
        for tie in TIES:
            if last in tie:
                return [f"{base}.{x}" for x in tie if f"{base}.{x}" in rec]
        return [key]

    owner = {m: t for t, ms in members.items() for m in ms}
    while True:
        best = None
        seen = set()
        for k in rec:
            if ".experts." in k or k in seen or owner.get(k) is None:
                continue
            grp = tie_group(k)
            seen.update(grp)
            K = rec[k]
            if K <= args.min_dense:
                continue
            n = sum(numel[m] for m in grp)
            if (bits() * total - n) / total < args.target:
                continue
            d_kld = sum(S[owner[m]] * (rfn(m, K - 1) ** 2 - rfn(m, K) ** 2) for m in grp)
            if best is None or d_kld / n < best[0]:
                best = (d_kld / n, grp, K)
        if best is None:
            break
        for m in best[1]:
            rec[m] = best[2] - 1
        log.append(f"demote {', '.join(best[1])}: {best[2]} -> {best[2] - 1}")

    end = bits()
    with open(args.output, "w") as f:
        for line in head:
            if line.startswith("achieved_bpw:"):
                line = f"achieved_bpw: {end:.6f}\n"
            f.write(line)
        for k, v in rec.items():
            f.write(f"  {k}: {int(v) if v == int(v) else v}\n")
    print(f" -- mean bitrate {start:.6f} -> {end:.6f} (target {args.target})")
    for l in log:
        print("    " + l)
    print(f" -- saved {args.output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required = True)
    ap.add_argument("--measurement", required = True)
    ap.add_argument("--rfn", required = True)
    ap.add_argument("--target", type = float, required = True)
    ap.add_argument("--min_dense", type = float, default = 6, help = "never demote dense tensors below this")
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
