"""Every (operation, condition) pair of the branch-verb microbenchmark, one
figure per operation, for the paper's appendix: a latency row over a
storage row, one panel per condition, in the style of the RQ2 story
figures (same markers, colours, base-2 axes and fonts, rendered at the
size they take in the paper: 3.35 in for two conditions, 7 in for more).

    python scripts/plotting/plot_micro_all.py --run-dir run_stats/micro_verbs_<date> \\
        --outdir figures/micro_verbs_<date>/all
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from plot_micro_stories import (F, ORDER, _panel, _latency_axis,  # noqa: E402
                                _storage_axis2, _legend, _save)
from plot_micro_verbs import INK, load  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from microbench.verbs.grid import AXES, VERBS  # noqa: E402

# Panel titles name the operation's cost, as in the RQ2 figures; the x
# label names the condition.
# X labels where the grid's own label is too long for the panel: by
# (verb, condition) for the narrow two-panel figures, by condition otherwise.
XLABEL = {"age": "commits behind HEAD", "written": "rows written", "parallel": "branches deleted at once"}
NARROW_XLABEL = {"scale": "data size (W)", "distance": "rows past the target",
                 ("revert", "rows"): "rows in the commit"}


def _has_data(points, axis):
    return any(points.get(s, {}).get(f"{axis.verb}:{axis.name}={v}", {}).get("status") == "ok"
               for s in ORDER for v in axis.values)


# Pairs shown elsewhere in the paper (the parallel deletion of RQ3).
SKIP = {("delete", "parallel")}


def plot_verb(verb, points, outdir):
    axes_ = [a for a in AXES if a.verb == verb and (a.verb, a.name) not in SKIP and _has_data(points, a)]
    n = len(axes_)
    if not n:
        return []
    narrow = n <= 2
    width, height = (3.35, 3.4) if narrow else (7.0, 3.7)
    fig, axs = plt.subplots(2, n, figsize=(width, height), squeeze=False)
    for j, axis in enumerate(axes_):
        ax_l, ax_s = axs[0, j], axs[1, j]
        xt = (5.5 if n >= 4 else 6) if len(axis.values) > 6 else None
        lat = _panel(ax_l, axis, points, "latency", xtick=xt)
        pos = [v for v in lat if v > 0]
        if pos:
            _latency_axis(ax_l, min(pos), max(lat))
        ax_l.set_title(f"({chr(97 + j)}) {verb.capitalize()} cost", loc="left",
                       fontsize=F["title"], color=INK, fontweight="bold", pad=3)
        ax_l.tick_params(axis="x", labelbottom=False)
        sto = _panel(ax_s, axis, points, "storage", xtick=xt)
        if sto:
            _storage_axis2(ax_s, sto)
            ax_s.axhline(0, color="#ccc", lw=0.6)
        else:
            ax_s.text(0.5, 0.5, "not measured", ha="center", va="center", transform=ax_s.transAxes,
                      fontsize=F["tick"], color="#888")
            ax_s.set_yticks([])
        label = XLABEL.get(axis.name, axis.label)
        if narrow:
            label = NARROW_XLABEL.get((verb, axis.name), NARROW_XLABEL.get(axis.name, label))
        ax_s.set_xlabel(label, fontsize=F["label"], fontweight="bold", labelpad=2)
    axs[0, 0].set_ylabel("latency", fontsize=F["label"], fontweight="bold")
    axs[1, 0].set_ylabel("storage added", fontsize=F["label"], fontweight="bold")
    if narrow:
        _legend(fig, axs[0, 0], ncol=len(ORDER), fontsize=5.5, handlelength=1.2, columnspacing=0.6)
        fig.subplots_adjust(top=0.87, bottom=0.1, left=0.17, right=0.985, wspace=0.5, hspace=0.12)
    else:
        _legend(fig, axs[0, 0], ncol=len(ORDER))
        fig.subplots_adjust(top=0.88, bottom=0.1, left=0.08, right=0.995, wspace=0.42, hspace=0.12)
    return _save(fig, outdir, f"micro_all_{verb}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    points, _ = load(a.run_dir)
    for verb in VERBS:
        for out in plot_verb(verb, points, a.outdir):
            print("wrote", out)


if __name__ == "__main__":
    main()
