"""
Bit allocation map of an exllamav3 recipe: per layer, the bits of each tensor group, as a heatmap (PNG) and a
Markdown table.

    PYTHONPATH=pylibs python tools/bit_map.py --recipe calib/recipe_v3.yaml -o docs/bit_allocation
"""
import argparse, re
from collections import defaultdict

# rows of the map: (label, key patterns)
GROUPS = [
    ("Experts gate/up", [r"mlp\.experts\.\d+\.(gate|up)_proj$"]),
    ("Experts down", [r"mlp\.experts\.\d+\.down_proj$"]),
    ("Shared expert", [r"mlp\.shared_expert\.(gate|up|down)_proj$"]),
    ("GDN qkv, z / attn q", [r"linear_attn\.in_proj_(qkv|z)$", r"self_attn\.q_proj$"]),
    ("Attn k, v", [r"self_attn\.(k|v)_proj$"]),
    ("GDN out / attn o", [r"linear_attn\.out_proj$", r"self_attn\.o_proj$"]),
    ("Indexer", [r"self_attn\.indexer\.index_qk_proj$"]),
]
# ordinal blue ramp of the reference palette (light surface), five bands far enough apart to tell; the exact value
# is printed in every cell
BANDS = [(3.5, "#86b6ef", "<= 3.5"), (4, "#5598e7", "4"), (5, "#2a78d6", "5"), (6, "#1c5cab", "6"), (8, "#0d366b", "7-8")]


def color(b):
    return next(c for top, c, _ in BANDS if b <= top)


def load(path):
    head, rec = {}, {}
    for line in open(path):
        m = re.match(r"  (\S+): ([\d.]+)$", line)
        if m:
            rec[m.group(1)] = float(m.group(2))
        else:
            m = re.match(r"(\w+): (.*)$", line)
            if m:
                head[m.group(1)] = m.group(2)
    return head, rec


def table(rec):
    grid = defaultdict(dict)
    for key, bits in rec.items():
        m = re.search(r"layers\.(\d+)\.(.*)$", key)
        if not m:
            continue
        layer, rest = int(m.group(1)), m.group(2)
        for label, pats in GROUPS:
            if any(re.search(p, rest) for p in pats):
                grid[label].setdefault(layer, set()).add(bits)
    out = {}
    for label, _ in GROUPS:
        out[label] = {}
        for layer, s in grid[label].items():
            assert len(s) == 1, (label, layer, s)
            out[label][layer] = s.pop()
    return out


def fmt(b):
    return str(int(b)) if float(b).is_integer() else str(b)


def main(args):
    head, rec = load(args.recipe)
    grid = table(rec)
    layers = sorted({l for g in grid.values() for l in g})

    # Markdown
    lines = [f"# Bit allocation", "",
             f"Recipe `{args.recipe.split('/')[-1]}`: {head.get('achieved_bpw', '?')} bits per weight over the "
             f"budgeted tensors, head (`lm_head`) {head.get('head_bits', '?')} bits (outside the budget). Every expert "
             "of a layer has the same width per projection. `-`: the layer has no such tensor (attention layers are "
             "every fourth layer, 3, 7, ..., 47; the others are Gated DeltaNet).", "",
             "| Layer | " + " | ".join(l for l, _ in GROUPS) + " |",
             "|---|" + "---|" * len(GROUPS)]
    for l in layers:
        lines.append(f"| {l} | " + " | ".join(fmt(grid[g][l]) if l in grid[g] else "-" for g, _ in GROUPS) + " |")
    open(args.output + ".md", "w").write("\n".join(lines) + "\n")

    # Heatmap: layers on x, tensor groups on y
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    surface, ink, ink2 = "#fcfcfb", "#0b0b0b", "#52514e"
    fig_w, cell = 15.0, 15.0 / (len(layers) + 9)
    fig, ax = plt.subplots(figsize = (fig_w, cell * (len(GROUPS) + 4)), dpi = 160)
    fig.patch.set_facecolor(surface)
    ax.set_facecolor(surface)
    gap = 0.06
    for yi, (g, _) in enumerate(GROUPS):
        for xi, l in enumerate(layers):
            b = grid[g].get(l)
            if b is None:
                continue
            ax.add_patch(Rectangle((xi + gap / 2, yi + gap / 2), 1 - gap, 1 - gap, facecolor = color(b), linewidth = 0))
            ax.text(xi + 0.5, yi + 0.5, fmt(b), ha = "center", va = "center", fontsize = 6.5,
                    color = "#ffffff" if b > 3.5 else ink)
    ax.set_xlim(0, len(layers))
    ax.set_ylim(len(GROUPS), 0)
    ax.set_xticks([i + 0.5 for i in range(len(layers))], [str(l) for l in layers], fontsize = 6.5, color = ink2)
    ax.set_yticks([i + 0.5 for i in range(len(GROUPS))], [g for g, _ in GROUPS], fontsize = 8, color = ink)
    ax.tick_params(length = 0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xlabel("Layer", fontsize = 8, color = ink2)
    ax.set_title(f"Bits per weight by layer and tensor group  ·  4.05 bpw overall ({float(head.get('achieved_bpw', 0)):.4f}), "
                 f"head {head.get('head_bits', '?')} bits", fontsize = 10, color = ink, loc = "left", pad = 22)
    # legend: the bands
    x0 = 0
    for _, c, label in BANDS:
        ax.add_patch(Rectangle((x0, -0.9), 0.8, 0.55, facecolor = c, linewidth = 0, clip_on = False))
        ax.text(x0 + 1.0, -0.62, f"{label} bits", va = "center", fontsize = 7, color = ink2, clip_on = False)
        x0 += 4.6
    fig.tight_layout()
    fig.savefig(args.output + ".png", facecolor = surface)
    print(f" -- {args.output}.md, {args.output}.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required = True)
    ap.add_argument("-o", "--output", required = True)
    main(ap.parse_args())
