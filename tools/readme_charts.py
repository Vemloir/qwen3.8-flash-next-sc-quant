"""
Charts for the model card from results/kld_by_source.json (written for the evaluation dataset):

  kld_overall.png     mean KL divergence vs BF16, this quant, turboderp's SC 4.18 bpw (when present in the results)
                      and turboderp's 4.05 bpw, on the two independent traces
  kld_by_source.png   relative difference per source / language against each of them, with 95% paired-bootstrap
                      intervals
  kld_hist.png        distribution of per-token KL divergence (from results/per_token_kld.safetensors), log x axis

    PYTHONPATH=pylibs python tools/readme_charts.py --results hf/dataset/results/kld_by_source.json \
        --per_token hf/dataset/results/per_token_kld.safetensors -o hf/assets [--theme light]
"""
import argparse, json, os

# surface, ink, secondary ink, grid, axis, series 1-3 (categorical slots of the reference palette, stepped per mode)
THEMES = {"dark": ("#1a1a19", "#f0efec", "#c3c2b7", "#2f2f2c", "#55544f", "#3987e5", "#d95926", "#199e70"),
          "light": ("#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834", "#1baf7a")}
SURFACE, INK, INK2, GRID, AXIS, OURS, REF, REF_SC = THEMES["dark"]

# the quants compared, in plot order: results key suffix, color, label; turboderp's SC 4.18 only when present
def quants(res):
    q = [("sc_4.05", OURS, "SC2 4.05 bpw (this quant)")]
    if "kld_turboderp_sc_4.18" in res["nemotron"]["all"]:
        q.append(("turboderp_sc_4.18", REF_SC, "turboderp SC 4.18 bpw"))
    return q + [("turboderp_4.05", REF, "turboderp 4.05 bpw")]


# comparisons drawn in kld_by_source.png: tag of the rel_diff / ci95 fields, color of the reference quant, name
def comparisons(res):
    c = [("", REF, "turboderp 4.05 bpw")]
    if "rel_diff_vs_turboderp_sc_4.18" in res["nemotron"]["all"]:
        c.insert(0, ("_vs_turboderp_sc_4.18", REF_SC, "turboderp SC 4.18 bpw"))
    return c

NAMES = {
    "nemotron": {"tool_calling": "Agentic tool calling", "knowledge_agent": "Knowledge-base agent",
                 "swe_opencode": "SWE (OpenCode)", "chat": "Chat", "multi_turn_if": "Multi-turn instructions",
                 "math_proofs": "Math proofs", "all": "All (48 rows)"},
    "multilingual": {"ru": "Russian", "de": "German", "es": "Spanish", "fr": "French", "it": "Italian",
                     "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "pt": "Portuguese", "hi": "Hindi",
                     "all": "All (52 rows)"},
}
TITLES = {"nemotron": "Nemotron 3.5 Super conversations", "multilingual": "Multilingual"}


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors = INK2, labelsize = 8, length = 0)


def overall(res, out, plt):
    q = quants(res)
    fig, ax = plt.subplots(figsize = (6.4, 3.2), dpi = 170)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    groups = ["nemotron", "multilingual"]
    w = 0.78 / len(q)
    top = max(res[g]["all"][f"kld_{k}"] for g in groups for k, _, _ in q)
    for i, g in enumerate(groups):
        a = res[g]["all"]
        for j, (k, c, lab) in enumerate(q):
            v = a[f"kld_{k}"]
            x = i + (j - (len(q) - 1) / 2) * (w + 0.02)
            ax.bar(x, v, width = w, color = c, label = lab if i == 0 else None, edgecolor = SURFACE, linewidth = 2)
            ax.text(x, v, f"{v:.5f}", ha = "center", va = "bottom", fontsize = 7, color = INK)
        diffs = [f"{a['rel_diff' + tag] * 100:+.1f}% vs {name.replace(' bpw', '')}" for tag, _, name in comparisons(res)]
        ax.text(i, top * 1.2, "\n".join(diffs), ha = "center", va = "bottom", fontsize = 7.5, color = INK,
                fontweight = "bold", linespacing = 1.4)
    ax.set_xticks(range(len(groups)), [TITLES[g] for g in groups], fontsize = 8.5, color = INK)
    ax.set_yticks([])
    ax.set_ylim(0, top * 1.62)
    ax.set_title("Mean KL divergence from BF16 (lower is closer)", fontsize = 10, color = INK, loc = "left", pad = 22)
    ax.legend(frameon = False, fontsize = 7.5, labelcolor = INK2, loc = "lower left", bbox_to_anchor = (-0.01, 1.0),
              ncol = len(q), borderaxespad = 0, handlelength = 1.2, columnspacing = 1.2)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "kld_overall.png"), facecolor = SURFACE)


def by_source(res, out, plt):
    order = {"nemotron": ["all", "math_proofs", "swe_opencode", "knowledge_agent", "chat", "tool_calling", "multi_turn_if"],
             "multilingual": ["all", "ru", "pt", "ko", "hi", "it", "fr", "zh", "de", "es", "ja"]}
    comps = comparisons(res)
    h = 0.8 / len(comps)
    fig, axes = plt.subplots(1, 2, figsize = (10.5, 2.2 + 0.42 * len(comps) * 11 / 2), dpi = 170,
                             gridspec_kw = {"width_ratios": [7, 11]})
    fig.patch.set_facecolor(SURFACE)
    lo_all = min(v["ci95" + t][0] for g in order for v in res[g].values() for t, _, _ in comps) * 100
    hi_all = max(v["ci95" + t][1] for g in order for v in res[g].values() for t, _, _ in comps) * 100
    for ax, g in zip(axes, order):
        style(ax)
        keys = [k for k in order[g] if k in res[g]]
        ys = list(range(len(keys)))[::-1]
        for y, k in zip(ys, keys):
            v = res[g][k]
            for j, (tag, c, name) in enumerate(comps):
                yy = y + ((len(comps) - 1) / 2 - j) * h
                d, (lo, hi) = v["rel_diff" + tag] * 100, (v["ci95" + tag][0] * 100, v["ci95" + tag][1] * 100)
                ax.barh(yy, d, height = h * 0.88, color = c, label = f"against {name}" if (y, ax) == (ys[0], axes[0]) else None)
                ax.plot([lo, hi], [yy, yy], color = INK2, linewidth = 1)
                # value label beside the interval, on the side away from zero when the whole interval is above it
                lx, ha = (hi + 1.2, "left") if lo > 0 else (min(lo, d) - 1.2, "right")
                ax.text(lx, yy, f"{d:+.1f}%", ha = ha, va = "center", fontsize = 6.5, color = INK,
                        fontweight = "bold" if k == "all" else "normal")
        ax.axvline(0, color = AXIS, linewidth = 1)
        ax.set_yticks(ys, [NAMES[g][k] for k in keys], fontsize = 8, color = INK)
        for t, k in zip(ax.get_yticklabels(), keys):
            if k == "all":
                t.set_fontweight("bold")
        ax.set_xlim(lo_all - 14, max(hi_all, 0) + 3)   # room for the value label left of the widest interval
        ax.xaxis.grid(True, color = GRID, linewidth = 0.6)
        ax.set_axisbelow(True)
        ax.set_xlabel("KL divergence of this quant against each reference (%)", fontsize = 8, color = INK2)
        ax.set_title(TITLES[g], fontsize = 9.5, color = INK, loc = "left")
    fig.suptitle("How much closer to BF16, by source  ·  bars: mean, lines: 95% interval  ·  left of 0 = better",
                 fontsize = 10, color = INK, x = 0.01, ha = "left")
    fig.tight_layout(rect = (0, 0, 1, 0.94))
    fig.legend(frameon = False, fontsize = 8, labelcolor = INK2, loc = "upper left", bbox_to_anchor = (0.005, 0.965),
               ncol = len(comps), handlelength = 1.2)
    fig.savefig(os.path.join(out, "kld_by_source.png"), facecolor = SURFACE)


def hist(per_token, res, out, plt):
    """
    Per-token KLD histograms, one panel per trace: 120 bins uniform on a log axis over the p1-p99 range of both quants
    (exact zeros and the lowest 1% fall outside), lightly smoothed, medians marked
    """
    import numpy as np
    from safetensors.numpy import load_file
    kl = load_file(per_token)
    groups = ["nemotron", "multilingual"]
    fig, axes = plt.subplots(1, 2, figsize = (10.5, 4.0), dpi = 170, sharey = False)
    fig.patch.set_facecolor(SURFACE)
    series = [(k, c, lab) for k, c, lab in quants(res) if f"nemotron.{k}" in kl]
    for ax, g in zip(axes, groups):
        style(ax)
        ax.spines["left"].set_visible(False)
        data = {k: kl[f"{g}.{k}"].astype(np.float64) for k, _, _ in series}
        both = np.concatenate([d[d > 0] for d in data.values()])
        lo, hi = np.percentile(both, [1, 99])
        edges = np.logspace(np.log10(lo), np.log10(hi), 121)
        mid = np.sqrt(edges[:-1] * edges[1:])
        top = 0
        for k, c, lab in series:
            h, _ = np.histogram(data[k], bins = edges)
            h = np.convolve(h, np.ones(3) / 3, mode = "same")
            top = max(top, h.max())
            med = np.median(data[k])
            ax.plot(mid, h, color = c, linewidth = 2, solid_capstyle = "round",
                    label = f"{lab}, median {med:.2e}".replace("e-0", "e-"))
            ax.axvline(med, color = c, linewidth = 1, linestyle = (0, (3, 3)))
        ax.set_xscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(0, top * 1.3)
        ax.set_yticks([])
        ax.xaxis.grid(True, color = GRID, linewidth = 0.6)
        ax.set_axisbelow(True)
        ax.set_xlabel("per-token KL divergence from BF16 (log scale)", fontsize = 8, color = INK2)
        a = res[g]["all"]
        ax.set_title(f"{TITLES[g]}  ·  {a['scored_tokens']:,} tokens", fontsize = 9.5, color = INK, loc = "left")
    for ax in axes:
        ax.legend(frameon = False, fontsize = 7, labelcolor = INK2, loc = "upper left")
    fig.suptitle("Distribution of per-token error  ·  further left = closer to BF16  ·  dashed: medians",
                 fontsize = 10, color = INK, x = 0.01, ha = "left")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "kld_hist.png"), facecolor = SURFACE)


def main(args):
    global SURFACE, INK, INK2, GRID, AXIS, OURS, REF, REF_SC
    SURFACE, INK, INK2, GRID, AXIS, OURS, REF, REF_SC = THEMES[args.theme]
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    res = json.load(open(args.results))
    os.makedirs(args.output, exist_ok = True)
    overall(res, args.output, plt)
    by_source(res, args.output, plt)
    if args.per_token:
        hist(args.per_token, res, args.output, plt)
    print(" -- charts in", args.output)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required = True)
    ap.add_argument("--per_token", help = "per_token_kld.safetensors, for kld_hist.png")
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--theme", choices = THEMES, default = "dark")
    main(ap.parse_args())
