"""
Charts for the model card from results/kld_by_source.json (written for the evaluation dataset):

  kld_overall.png     mean KL divergence vs BF16, this quant and turboderp's 4.05 bpw, on the two independent traces
  kld_by_source.png   relative difference per source / language with 95% paired-bootstrap intervals

    PYTHONPATH=pylibs python tools/readme_charts.py --results hf/dataset/results/kld_by_source.json -o hf/assets
"""
import argparse, json, os

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
OURS, REF = "#2a78d6", "#eb6834"          # categorical slots 1 and 2 of the reference palette

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
    fig, ax = plt.subplots(figsize = (6.4, 3.0), dpi = 170)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    groups = ["nemotron", "multilingual"]
    w = 0.34
    for i, g in enumerate(groups):
        a = res[g]["all"]
        for j, (v, c, lab) in enumerate(((a["kld_sc_4.05"], OURS, "SC 4.05 bpw (this quant)"),
                                         (a["kld_turboderp_4.05"], REF, "turboderp 4.05 bpw"))):
            x = i + (j - 0.5) * (w + 0.03)
            ax.bar(x, v, width = w, color = c, label = lab if i == 0 else None, edgecolor = SURFACE, linewidth = 2)
            ax.text(x, v, f"{v:.5f}", ha = "center", va = "bottom", fontsize = 7.5, color = INK)
        d = a["rel_diff"] * 100
        ax.text(i, max(a["kld_sc_4.05"], a["kld_turboderp_4.05"]) * 1.17, f"{d:+.1f}%", ha = "center",
                fontsize = 9, color = INK, fontweight = "bold")
    ax.set_xticks(range(len(groups)), [TITLES[g] for g in groups], fontsize = 8.5, color = INK)
    ax.set_yticks([])
    ax.set_ylim(0, max(res[g]["all"]["kld_turboderp_4.05"] for g in groups) * 1.32)
    ax.set_title("Mean KL divergence from BF16 (lower is closer)", fontsize = 10, color = INK, loc = "left")
    ax.legend(frameon = False, fontsize = 8, labelcolor = INK2, loc = "upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "kld_overall.png"), facecolor = SURFACE)


def by_source(res, out, plt):
    order = {"nemotron": ["all", "math_proofs", "swe_opencode", "knowledge_agent", "chat", "tool_calling", "multi_turn_if"],
             "multilingual": ["all", "ru", "pt", "ko", "hi", "it", "fr", "zh", "de", "es", "ja"]}
    fig, axes = plt.subplots(1, 2, figsize = (10.5, 4.6), dpi = 170, gridspec_kw = {"width_ratios": [7, 11]})
    fig.patch.set_facecolor(SURFACE)
    lo_all = min(v["ci95"][0] for g in order for k, v in res[g].items()) * 100
    hi_all = max(v["ci95"][1] for g in order for k, v in res[g].items()) * 100
    for ax, g in zip(axes, order):
        style(ax)
        keys = [k for k in order[g] if k in res[g]]
        ys = list(range(len(keys)))[::-1]
        for y, k in zip(ys, keys):
            v = res[g][k]
            d, (lo, hi) = v["rel_diff"] * 100, (v["ci95"][0] * 100, v["ci95"][1] * 100)
            ax.barh(y, d, height = 0.62, color = OURS, alpha = 1.0 if k == "all" else 0.85)
            ax.plot([lo, hi], [y, y], color = INK2, linewidth = 1.2)
            ax.text(min(lo, d) - 1.2, y, f"{d:+.1f}%", ha = "right", va = "center", fontsize = 7.5, color = INK,
                    fontweight = "bold" if k == "all" else "normal")
        ax.axvline(0, color = AXIS, linewidth = 1)
        ax.set_yticks(ys, [NAMES[g][k] for k in keys], fontsize = 8, color = INK)
        for t, k in zip(ax.get_yticklabels(), keys):
            if k == "all":
                t.set_fontweight("bold")
        ax.set_xlim(lo_all - 9, max(hi_all, 0) + 3)
        ax.xaxis.grid(True, color = GRID, linewidth = 0.6)
        ax.set_axisbelow(True)
        ax.set_xlabel("KL divergence vs turboderp 4.05 bpw (%)", fontsize = 8, color = INK2)
        ax.set_title(TITLES[g], fontsize = 9.5, color = INK, loc = "left")
    fig.suptitle("How much closer to BF16, by source  ·  bars: mean, lines: 95% interval  ·  left of 0 = better",
                 fontsize = 10, color = INK, x = 0.01, ha = "left")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "kld_by_source.png"), facecolor = SURFACE)


def main(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    res = json.load(open(args.results))
    os.makedirs(args.output, exist_ok = True)
    overall(res, args.output, plt)
    by_source(res, args.output, plt)
    print(" -- charts in", args.output)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required = True)
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
