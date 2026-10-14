"""Bar version of the cost panels: one row per scenario, three columns
(time in branch operations, time in ordinary data operations, end-to-end
elapsed), systems ordered by branch-creation latency in every subplot.

    python scripts/plotting/plot_cost_bars.py --run-dir run_stats/macro_20261012 \
        --out figures/macro_20261012/cost_bars.png [--scenarios rl_env multi_agent dev_agent data_agent] [--log]
"""

import argparse
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from plot_cost_panels import load
from plot_headline import COLORS

TITLES = {"rl_env": "S1 RL env (data-heavy)", "multi_agent": "S3 multi-agent (branch churn)",
          "dev_agent": "S4 dev agent (reconciliation)", "data_agent": "S6 data agent (bulk data)",
          "context_mgmt": "S2 context management", "ops_agent": "S5 ops agent"}
PANELS = [("branch_s", "branch ops: worker-seconds (ops)", "#e8853a"),
          ("data_s", "data ops: worker-seconds (ops)", "#9a9a9a"),
          ("elapsed", "end-to-end elapsed: wall-clock s", "#3b5b7a")]


def _k(n):
    return f"{n/1000:.0f}k" if n >= 10000 else f"{n:,}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--scenarios", nargs="*", default=["rl_env", "multi_agent", "dev_agent", "data_agent"])
    ap.add_argument("--log", action="store_true")
    a = ap.parse_args()
    t, create_ms = load(a.run_dir)
    systems = sorted(create_ms, key=create_ms.get)
    n, m = len(systems), len(a.scenarios)
    fig, axes = plt.subplots(m, 3, figsize=(13.5, 2.05 * m + 0.8), sharey=True, gridspec_kw={"wspace": 0.08, "hspace": 0.55})
    for i, sc in enumerate(a.scenarios):
        d = t[t.scenario == sc].set_index("backend").reindex(systems)
        for j, (col, title, color) in enumerate(PANELS):
            ax = axes[i, j]
            vals = d[col].values
            colors = [("#b00020" if (col == "elapsed" and f > 0) else color) for f in d.fails.values]
            ax.barh(range(n), vals, color=colors, height=0.68)
            xmax = vals.max()
            counts = d["branch_n"].values if col == "branch_s" else d["data_n"].values if col == "data_s" else None
            for k, (v, f) in enumerate(zip(vals, d.fails.values)):
                lab = f"{v:,.0f}" + (f"  ✗{int(f)}" if (col == "elapsed" and f > 0) else "")
                if counts is not None:
                    lab += f"  ({_k(counts[k])} ops)"
                ax.text(v * (1.06 if a.log else 1) + (0 if a.log else xmax * 0.015), k, lab, va="center", fontsize=7.5,
                        color="#b00020" if (col == "elapsed" and f > 0) else "#333", fontweight="bold" if (col == "elapsed" and f > 0) else "normal")
            if a.log:
                ax.set_xscale("log"); ax.set_xlim(max(1, vals.min() / 3), xmax * 6)
            else:
                ax.set_xlim(0, xmax * (1.5 if col != "elapsed" else 1.32))
            ax.tick_params(axis="x", labelsize=7); ax.grid(axis="x", color="#eee"); ax.set_axisbelow(True)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            if i == 0:
                ax.set_title(title, fontsize=10, loc="left")
            if j == 0:
                th = int(d.threads.median())
                ax.set_ylabel(f"{TITLES.get(sc, sc)}\n{th} worker{'s' if th != 1 else ''} at peak", fontsize=9.5, rotation=0, ha="right", va="center", labelpad=8, fontweight="bold")
        axes[i, 0].set_ylim(n - 0.5, -0.5)
    axes[0, 0].set_yticks(range(n))
    axes[0, 0].set_yticklabels([f"{b}  {create_ms[b]:,.0f} ms" for b in systems], fontsize=8.5)
    for ax_row in axes:
        for lab, b in zip(ax_row[0].get_yticklabels(), systems):
            lab.set_color(COLORS[b]); lab.set_fontsize(8.5)
        ax_row[0].tick_params(axis="y", length=0)
    fig.text(0.01, 0.005, "Systems ordered by branch-creation latency (pooled median beside the name). Worker-seconds = sum of every agent-side operation's "
             "latency across the scenario's concurrent workers, so a column can exceed the wall-clock elapsed; the spine's background load is excluded. "
             "Red = run with invariant failures (count). One run per cell.", fontsize=7.5, color="#555")
    fig.savefig(a.out, dpi=150, bbox_inches="tight"); print("wrote", a.out)


if __name__ == "__main__":
    main()
