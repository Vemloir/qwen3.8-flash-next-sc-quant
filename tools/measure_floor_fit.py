"""
Remove the noise-independent KLD floor from an sc_measure.py measurement before sc_optimize.py.

On Qwen3.8-Flash-Next (512-expert top-10 routing, QSA sparse attention) any perturbation of a
tensor flips discrete choices downstream: expert selections of tokens near a routing tie and the
attention indexer's token picks. That adds a KLD component that is almost independent of the
noise amplitude and grows with the number of modules after the target (~94% of the summed KLD at
the anchor level). The real quantized model pays it too, but no bit allocation removes it, so
feeding it to the optimizer as sensitivity makes early layers look far more valuable per bit than
they are.

Per target, the three measured levels are fitted as  kld(r) = F + S * r^2  (F, S >= 0, least
squares over r = rfn_actual), and each level's kld is replaced by S * r^2, so sc_optimize.py's
geometric-mean sensitivity recovers S exactly. The raw values are kept as kld_raw / floor_fit.
Targets whose fitted S is zero although their raw KLD is not get the 5th percentile of S among
targets of the same kind, so measurement noise in a difference of near-equal numbers cannot
demote a tensor to the bottom of the range. Targets with an all-zero measurement (e.g. the QSA
indexer on rows shorter than its token budget) stay zero and need a fixed bitrate in the recipe.

    python measure_floor_fit.py calib/measure.json calib/measure_fitted.json
"""
import json, sys
import numpy as np


def kind_of(key):
    return "experts" if key.endswith(".experts") else key.rsplit(".", 1)[-1]


def main(src, dst):
    d = json.load(open(src))
    fits = []
    for r in d["results"]:
        x = np.array([l["rfn_actual"] ** 2 for l in r["levels"]])
        y = np.array([l["kld"] for l in r["levels"]])
        if not y.any():
            fits.append((r, 0.0, 0.0, True))
            continue
        A = np.vstack([np.ones_like(x), x]).T
        (F, S), *_ = np.linalg.lstsq(A, y, rcond = None)
        if S < 0:
            F, S = float(y.mean()), 0.0
        if F < 0:
            F, S = 0.0, float(x @ y / (x @ x))
        fits.append((r, float(F), float(S), False))

    by_kind = {}
    for r, F, S, zero in fits:
        if S > 0:
            by_kind.setdefault(kind_of(r["key"]), []).append(S)
    floor_s = {k: float(np.percentile(v, 5)) for k, v in by_kind.items()}

    n_clamped, n_zero = 0, 0
    for r, F, S, zero in fits:
        if zero:
            n_zero += 1
            r["floor_fit"] = {"F": 0.0, "S": 0.0, "all_zero": True}
            continue
        if S == 0.0:
            S = floor_s.get(kind_of(r["key"]), 0.0)
            n_clamped += 1
        r["floor_fit"] = {"F": F, "S": S}
        for l in r["levels"]:
            l["kld_raw"] = l["kld"]
            l["kld"] = S * l["rfn_actual"] ** 2

    raw = sum(l["kld_raw"] for r in d["results"] for l in r["levels"][1:2] if "kld_raw" in l)
    smooth = sum(l["kld"] for r in d["results"] for l in r["levels"][1:2])
    d["floor_fit"] = {"model": "kld = F + S * rfn^2", "source": src,
                      "clamped_to_kind_p5": n_clamped, "all_zero": n_zero}
    json.dump(d, open(dst, "w"), indent = 1)
    print(f" -- {len(fits)} targets: {n_clamped} S clamped to their kind's 5th percentile, {n_zero} all-zero")
    print(f" -- summed kld at the 1.0x level: raw {raw:.4f}, smooth part {smooth:.4f}")
    print(f" -- saved {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
