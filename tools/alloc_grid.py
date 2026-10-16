"""
Expert bit allocation over all layers from split_grid.json: one (Kg, Ku, Kd) per layer from the K grid, the total
expert bits fixed (default: as recipe tb400, 43 layers at 4/4/4 and 5 at 3.5/3.5/3.5), minimizing the sum over
layers of w_l * err_l(Kg, Ku, Kd). Exact, by dynamic programming over half-bit units (multiple-choice knapsack).

Weights: "rel" (default) weighs every layer's relative expert error equally; "base" weighs it by the layer's 4/4/4
error, which favours layers whose experts are already the least accurate. Neither knows how much a layer matters
to the model's output, so the result is a proposal for qbench, not a final answer.

    python alloc_grid.py [--weights rel|base] [--budget_bits 568.5]
"""
import argparse, itertools, json, os

Q = os.path.expanduser("~/lm-inference/sc-quant")


def main(args):
    d = json.load(open(args.grid))
    K = d["K"]
    n = len(K)
    layers = sorted(int(k) for k in d if k.isdigit())
    i4 = K.index(4)
    opts = {}
    for l in layers:
        e = d[str(l)]["err"]
        w = 1.0 if args.weights == "rel" else e[i4][i4][i4]
        base = e[i4][i4][i4]
        opts[l] = [(round(2 * (K[a] + K[b] + K[c])), w * e[a][b][c] / base, (K[a], K[b], K[c]))
                   for a, b, c in itertools.product(range(n), repeat = 3)
                   if not args.gate_eq_up or a == b]
    budget = round(2 * (args.budget_bits if args.budget_bits else 12 * len(layers) - 1.5 * 5 * len(layers) / 48))
    INF = float("inf")
    # dp[b] = (cost, choices) over the layers so far using exactly b half-bits
    dp = {0: (0.0, [])}
    for l in layers:
        nd = {}
        for b, (c, ch) in dp.items():
            for hb, cost, split in opts[l]:
                nb = b + hb
                if nb > budget:
                    continue
                v = c + cost
                if v < nd.get(nb, (INF,))[0]:
                    nd[nb] = (v, ch + [split])
        dp = nd
    cost, choice = min((dp[b] for b in dp if b <= budget), key = lambda t: t[0])
    used = sum(sum(s) for s in choice)
    uniform = {l: (4, 4, 4) if l < 43 else (3.5, 3.5, 3.5) for l in layers}

    def f(k):
        return str(int(k)) if float(k).is_integer() else str(k)

    def err_of(l, s):
        e = d[str(l)]["err"]
        return e[K.index(s[0])][K.index(s[1])][K.index(s[2])] / e[i4][i4][i4]

    print(f" -- {len(layers)} layers, expert bits {used:g} of budget {budget / 2:g} (avg {used / (3 * len(layers)):.3f}), weights {args.weights}")
    tb = sum(err_of(l, uniform[l]) for l in layers)
    new = sum(err_of(l, s) for l, s in zip(layers, choice))
    print(f" -- sum of relative expert errors: tb400 split {tb:.2f}, allocated {new:.2f} ({(new / tb - 1) * 100:+.1f}%)")
    for l, s in zip(layers, choice):
        mark = "" if s == uniform[l] else f"   (tb400 {'/'.join(f(k) for k in uniform[l])}, {(err_of(l, s) / err_of(l, uniform[l]) - 1) * 100:+.0f}%)"
        print(f"    layer {l:2}: {'/'.join(f(k) for k in s):>11}{mark}")
    if args.out:
        json.dump({str(l): list(s) for l, s in zip(layers, choice)}, open(args.out, "w"), indent = 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default = f"{Q}/eval/split_grid.json")
    ap.add_argument("--weights", choices = ["rel", "base"], default = "rel")
    ap.add_argument("--budget_bits", type = float, default = None, help = "total expert bits over all layers (sum of Kg+Ku+Kd)")
    ap.add_argument("--gate_eq_up", action = "store_true", help = "only splits with Kg == Ku")
    ap.add_argument("--out", default = None)
    main(ap.parse_args())
