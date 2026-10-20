"""Two composite figures from the branch-verb microbenchmark for the
paper's RQ2 text: micro_ops.png, four operations side by side (commit vs
rows changed, merge vs data size, rebase vs commits, delete vs commits)
with latency over storage; micro_branch.png, branch creation against live
branches and against the age of the source commit. One legend per figure
(marker and colour per system), panel letters only, medians of the timed
reps, base-2 axes; the captions carry the explanation.

    python scripts/plotting/plot_micro_stories.py --run-dir run_stats/micro_verbs_<date> \\
        --outdir figures/micro_verbs_<date>/stories

  story_merge_scale.png    merge against data size: pays for the state
                           or for the change (storage: file_copy's clones
                           pay for pages the merge reads)
  story_rebase_commits.png rebase against commits replayed: the native
                           rebase is the one that scales
  story_branch_age.png     branch against the age of the source commit:
                           branching from history is not free everywhere
  story_branch_live.png    branch against live branches: flat everywhere,
                           quota handled by suspension
  story_commit_rows.png    commit against rows changed: every commit is a
                           marker, flat for all seven
  story_delete_commits.png delete against commits on the branch: who frees
                           what, and when
"""

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt
matplotlib.rcParams["pdf.fonttype"] = 42  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from plot_macro_cost import COLORS  # noqa: E402
from plot_micro_verbs import INK, MUTED, _secs_fmt, load  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from microbench.verbs.grid import AXES  # noqa: E402

ORDER = ["Doltgres", "Dolt", "Chronos", "MatrixOne", "file_copy", "SeekDB", "Neon"]
# One marker shape per system, repeated beside its label, so lines stay
# tellable apart in print and for colour-blind readers.
MARKERS = {"Doltgres": "o", "Dolt": "s", "Chronos": "^", "MatrixOne": "D", "file_copy": "v",
           "SeekDB": "P", "Neon": "X"}

STORIES = [
    {
        "file": "story_merge_scale", "verb": "merge", "axis": "scale", "storage": True,
        "panel": "Merge cost",
        "title": "A 100-row merge against the size of the data",
        "finding": "Flat where the engine merges trees or per table; 4-5x from W=1 to 8 where the adapter\n"
                   "compares whole tables. file_copy's clones also pay, in storage, for every page the merge reads.",
        "model": {"Doltgres": "cell merge", "Dolt": "cell merge", "Chronos": "per row changed",
                  "MatrixOne": "native, per table", "file_copy": "whole tables, hashed",
                  "SeekDB": "whole tables, joined", "Neon": "whole tables + temp branch"},
    },
    {
        "file": "story_rebase_commits", "verb": "rebase", "axis": "commits", "storage": True,
        "panel": "Rebase cost",
        "title": "Rebasing 1,000 rows carried by 1 to 128 commits",
        "finding": "The only native rebase is the only one that scales: Dolt replays commit by commit (x27-37),\n"
                   "every simulated rebase applies the net change once and is flat.",
        "model": {"Doltgres": "native, replays each commit", "Dolt": "native, replays each commit",
                  "Chronos": "net change", "MatrixOne": "re-fork + net change", "file_copy": "re-clone + net change",
                  "SeekDB": "net change", "Neon": "temp branch + net change"},
    },
    {
        "file": "story_commit_rows", "verb": "commit", "axis": "rows", "storage": True,
        "panel": "Commit cost",
        "title": "Committing 1 to 10,000 changed rows",
        "finding": "Every commit is a marker, flat from 1 to 10k rows on all seven systems; the 13x spread\n"
                   "between them is what the marker is: a pointer, a snapshot, an LSN, an SCN, a checkpoint, a clone.",
        "model": {"Doltgres": "commit object; chunks already written", "Dolt": "commit object; chunks already written",
                  "Chronos": "checkpoint + schema fingerprint", "MatrixOne": "snapshot", "file_copy": "clone of the database",
                  "SeekDB": "SCN row", "Neon": "LSN row"},
    },
    {
        "file": "story_delete_commits", "verb": "delete", "axis": "commits", "storage": True,
        "panel": "Delete cost",
        "title": "Deleting a branch that holds 0 to 128 commits",
        "finding": "file_copy drops one snapshot database per commit (x68) and, with Neon, is the only system that\n"
                   "frees space at once; the others free nothing until GC, and MatrixOne's delete adds a megabyte.",
        "model": {"Doltgres": "drops a ref", "Dolt": "drops a ref", "Chronos": "drops the interval",
                  "MatrixOne": "drops the database; snapshots stay", "file_copy": "drops branch + one db per commit",
                  "SeekDB": "drops the fork", "Neon": "API delete"},
    },
    {
        "file": "story_delete_parallel", "verb": "delete", "axis": "parallel", "storage": False,
        "panel": "Delete cost",
        "title": "Deleting 1 to 32 branches at once, one client each (wall-clock of the batch)",
        "finding": "Serialised on Dolt, Doltgres, SeekDB and MatrixOne (k deletes take k times one); file_copy and\n"
                   "Chronos overlap partly; Neon deletes 8 at once in the time of one, then saturates.",
        "model": {"Doltgres": "drops a ref", "Dolt": "drops a ref", "Chronos": "drops the interval",
                  "MatrixOne": "drops the database; snapshots stay", "file_copy": "drops branch + one db per commit",
                  "SeekDB": "drops the fork", "Neon": "API delete"},
    },
    {
        "file": "story_branch_live", "verb": "branch", "axis": "live", "storage": False,
        "panel": "Branch cost",
        "title": "Creating a branch from the head, with 1 to 128 branches already live",
        "finding": "Flat on every system, Neon included: the adapter keeps the project under its quota of 20 active\n"
                   "computes by suspending idle ones, invisible next to a compute start. Only Chronos drifts (x2.7):\n"
                   "every fork copies the parent's log, which grows with the forks before it.",
        "model": {"Doltgres": "pointer", "Dolt": "pointer", "Chronos": "checkpoint + log copy",
                  "MatrixOne": "clone per table", "file_copy": "two reflink clones", "SeekDB": "fork",
                  "Neon": "branch + compute start"},
    },
    {
        "file": "story_branch_age", "verb": "branch", "axis": "age", "storage": False,
        "panel": "Branch cost",
        "title": "Branching from a commit 0 to 128 commits behind the head",
        "finding": "Free from history on five systems; SeekDB restores every table from flashback (x9),\n"
                   "and file_copy gets faster, because a fork from a snapshot is one clone instead of two.",
        "model": {"Doltgres": "pointer", "Dolt": "pointer", "Chronos": "checkpoint", "MatrixOne": "clone per table",
                  "file_copy": "reflink clone(s)", "SeekDB": "fork + flashback restore", "Neon": "branch at LSN"},
    },
]


def _series_median(points, axis, key):
    """x positions and the median over the timed reps of one point (latency
    of the OK reps, or the storage delta), for one system along one axis.
    Medians, because single reps catch compute wake-ups and background
    compaction that a mean would take at face value."""
    field = "latency" if key == "latency" else "storage_delta"
    xs, out = [], []
    for i, v in enumerate(axis.values):
        p = points.get(f"{axis.verb}:{axis.name}={v}")
        if not (p and p["status"] == "ok"):
            continue
        vals = [r[field] for r in p["reps"] if field in r and (key != "latency" or r.get("status") == "OK")]
        if vals:
            xs.append(i); out.append(float(np.median(vals)))
    return np.array(xs), np.array(out)


MAX_TICKS = 6


def _bracket(cands, lo, hi, max_n=MAX_TICKS):
    """Ticks from the power-of-two candidates ``cands`` (ascending) that
    bracket [lo, hi]: the lowest at or below lo, the highest at or above hi,
    a uniform step 2^m between them, at most max_n of them; among the
    steps that fit, the smallest, and among its phases the one that
    wastes the least range beyond the data."""
    i_lo = max(i for i, c in enumerate(cands) if c <= lo) if cands[0] <= lo else 0
    i_hi = min(i for i, c in enumerate(cands) if c >= hi) if cands[-1] >= hi else len(cands) - 1
    for m in range(1, len(cands)):
        best = None
        for ph in range(m):
            first = i_lo - ((i_lo - ph) % m)
            last = i_hi + ((ph - i_hi) % m)
            if first < 0 or last >= len(cands):
                continue
            n = (last - first) // m + 1
            if n <= max_n:
                waste = (i_lo - first) + (last - i_hi)
                if best is None or waste < best[0]:
                    best = (waste, first, last)
        if best:
            return [cands[i] for i in range(best[1], best[2] + 1, m)]
    return [cands[i_lo], cands[i_hi]]


LAT_CANDS = [2 ** k / 1000 for k in range(-2, 10)] + [2 ** k for k in range(0, 12)]  # ms then s


def _lat_ticks(lo, hi):
    """Power-of-two ticks in readable units (1, 2, 4, ... 512 ms, then 1, 2,
    4, ... s) bracketing the data with a uniform step and few labels."""
    return _bracket(LAT_CANDS, lo, hi)


def _bin_fmt(v, _=None):
    a = abs(v); sign = "-" if v < 0 else ""
    for unit, div in (("GB", 2 ** 30), ("MB", 2 ** 20), ("KB", 2 ** 10)):
        if a >= div:
            x = a / div
            return f"{sign}{x:g} {unit}"
    return f"{sign}{a:g} B"


def _latency_axis(ax, lo, hi):
    ticks = _lat_ticks(lo, hi)
    ax.set_yscale("log", base=2)
    ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(FuncFormatter(_secs_fmt))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_ylim(ticks[0] / 1.25, ticks[-1] * 1.25)


BYTE_CANDS = [2 ** k for k in range(0, 41)]


def _storage_axis2(ax, values):
    """Bytes on a base-2 scale with labels bracketing the data: a log axis
    when everything is positive, else symlog (linear within 1 KB of zero)
    with the same power-of-two step above and below zero."""
    lin = 2 ** 10
    pos_max = max([v for v in values if v > 0], default=0)
    neg_max = max([-v for v in values if v < 0], default=0)
    if values and min(values) > 0:
        ticks = _bracket(BYTE_CANDS, min(values), max(values))
        ax.set_yscale("log", base=2)
        ax.set_ylim(ticks[0] / 1.25, ticks[-1] * 1.25)
    else:
        ax.set_yscale("symlog", base=2, linthresh=lin, linscale=0.4)
        # Labels every 2^m from the smallest power of two at or above each
        # side's extreme down to 2 KB (1 KB would sit on the 0 B label), the
        # smallest m that keeps at most MAX_TICKS labels including 0.
        def side(extreme, m):
            if extreme <= 0:
                return []
            top = 2 ** math.ceil(math.log2(max(extreme, 2 * lin)))
            out = []
            while top >= 2 * lin:
                out.append(top); top //= 2 ** m
            return sorted(out)
        for m in range(1, 40):
            pos, neg = side(pos_max, m), side(neg_max, m)
            if len(pos) + len(neg) + 1 <= MAX_TICKS:
                break
        ticks = [-t for t in reversed(neg)] + [0] + pos
        ax.set_ylim(-neg[-1] * 1.25 if neg else -lin, (pos[-1] if pos else lin) * 1.25)
    ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(FuncFormatter(_bin_fmt))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())


def _spread(ys, min_gap):
    """Nudge label positions (in a monotone transformed space) so no two
    are closer than min_gap, keeping their order."""
    order = np.argsort(ys)
    out = np.array(ys, dtype=float)
    for k in range(1, len(order)):
        i, j = order[k - 1], order[k]
        if out[j] - out[i] < min_gap:
            out[j] = out[i] + min_gap
    return out


# Font sizes in points for the figures at their final size in the paper
# (7 in across both columns, 3.35 in in one column).
F = {"tick": 6.5, "label": 8, "title": 8, "legend": 6.5}


def _xfmt(v):
    if isinstance(v, float):
        return f"{v:g}"
    return f"{v // 1000}k" if v >= 1000 else str(v)


def _panel(ax, axis, points, key, xtick=None):
    """One panel: every system's median along the axis; returns the values drawn."""
    vals = []
    for sysname in ORDER:
        xs, med = _series_median(points.get(sysname, {}), axis, key)
        if len(xs) < 2:
            continue
        ax.plot(xs, med, "-", color=COLORS.get(sysname, "#333"), lw=0.7, marker=MARKERS.get(sysname, "o"),
                ms=4.2, mec="white", mew=0.5, label=sysname)
        vals += list(med)
    ax.set_xticks(range(len(axis.values)))
    ax.set_xticklabels([_xfmt(v) for v in axis.values], fontsize=xtick or F["tick"])
    ax.set_xlim(-0.35, len(axis.values) - 0.65)
    ax.tick_params(axis="both", labelsize=F["tick"], length=2.5, width=0.5, pad=2)
    ax.grid(color="#eee", lw=0.5); ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_linewidth(0.6); sp.set_color("#444")
    return vals


def _legend(fig, ax, ncol, fontsize=None, handlelength=1.6, columnspacing=1.0):
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=ncol, fontsize=fontsize or F["legend"], frameon=False,
               bbox_to_anchor=(0.5, 1.0), handlelength=handlelength, columnspacing=columnspacing,
               handletextpad=0.4, markerscale=0.85)


def _save(fig, outdir, name):
    outs = []
    for ext, kw in (("pdf", {}), ("png", {"dpi": 300})):
        out = os.path.join(outdir, f"{name}.{ext}")
        fig.savefig(out, bbox_inches="tight", pad_inches=0.02, **kw)
        outs.append(out)
    plt.close(fig)
    return outs


def plot_ops(stories, points, outdir, name, width=7.0):
    """Operations side by side at the paper's text width: latency row over
    storage row, one legend, each panel titled with its letter and the
    operation's cost."""
    n = len(stories)
    fig, axes = plt.subplots(2, n, figsize=(width, 3.7), squeeze=False)
    for j, st in enumerate(stories):
        axis = next(a for a in AXES if a.verb == st["verb"] and a.name == st["axis"])
        ax_l, ax_s = axes[0, j], axes[1, j]
        lat = _panel(ax_l, axis, points, "latency")
        _latency_axis(ax_l, min(v for v in lat if v > 0), max(lat))
        ax_l.set_title(f"({chr(97 + j)}) {st['panel']}", loc="left", fontsize=F["title"], color=INK,
                       fontweight="bold", pad=3)
        ax_l.tick_params(axis="x", labelbottom=False)
        sto = _panel(ax_s, axis, points, "storage")
        _storage_axis2(ax_s, sto)
        ax_s.axhline(0, color="#ccc", lw=0.6)
        ax_s.set_xlabel(axis.label, fontsize=F["label"], fontweight="bold", labelpad=2)
    axes[0, 0].set_ylabel("latency", fontsize=F["label"], fontweight="bold")
    axes[1, 0].set_ylabel("storage added", fontsize=F["label"], fontweight="bold")
    _legend(fig, axes[0, 0], ncol=n and len(ORDER))
    fig.subplots_adjust(top=0.88, bottom=0.1, left=0.08, right=0.995, wspace=0.42, hspace=0.12)
    return _save(fig, outdir, name)


BRANCH_XLABEL = {"age": "commits behind HEAD", "parallel": "branches deleted at once"}


def plot_branch(stories, points, outdir, name, width=3.35, ylabel="latency", left=0.16, ylabelpad=4):
    """Branch panels side by side at the paper's column width, sharing the
    latency axis (labelled once, on the left)."""
    n = len(stories)
    # Width per x position, so the points are spaced alike in both panels.
    counts = [len(next(a for a in AXES if a.verb == st["verb"] and a.name == st["axis"]).values) for st in stories]
    fig, axes = plt.subplots(1, n, figsize=(width, 2.15), squeeze=False, sharey=True,
                             gridspec_kw={"width_ratios": counts})
    lo, hi = float("inf"), 0.0
    for j, st in enumerate(stories):
        axis = next(a for a in AXES if a.verb == st["verb"] and a.name == st["axis"])
        ax = axes[0, j]
        lat = _panel(ax, axis, points, "latency", xtick=6)   # 8-9 labels in 1.3 in
        lo, hi = min(lo, min(v for v in lat if v > 0)), max(hi, max(lat))
        ax.set_title((f"({chr(97 + j)}) " if n > 1 else "") + st["panel"], loc="left", fontsize=F["title"],
                     color=INK, fontweight="bold", pad=3)
        ax.set_xlabel(BRANCH_XLABEL.get(axis.name, axis.label), fontsize=F["label"], fontweight="bold", labelpad=2)
        if j:
            ax.tick_params(axis="y", length=0)
    _latency_axis(axes[0, 0], lo, hi)
    axes[0, 0].set_ylabel(ylabel, fontsize=F["label"], fontweight="bold", labelpad=ylabelpad)
    _legend(fig, axes[0, 0], ncol=len(ORDER), fontsize=5.5, handlelength=1.2, columnspacing=0.6)
    fig.subplots_adjust(top=0.8, bottom=0.17, left=left, right=0.985, wspace=0.1)
    return _save(fig, outdir, name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    points, _ = load(a.run_dir)
    by = {st["file"]: st for st in STORIES}
    ops = [by[k] for k in ("story_commit_rows", "story_merge_scale", "story_rebase_commits", "story_delete_commits")]
    branch = [by[k] for k in ("story_branch_live", "story_branch_age")]
    # k branches deleted at once by k clients: the point's latency is the
    # batch's wall-clock, so the panel reads as "time to delete k branches".
    parallel = [by["story_delete_parallel"]]
    for out in (plot_ops(ops, points, a.outdir, "micro_ops") + plot_branch(branch, points, a.outdir, "micro_branch")
                + plot_branch(parallel, points, a.outdir, "micro_delete_parallel", width=2.0,
                              ylabel="batch wall-clock", left=0.3, ylabelpad=8)):
        print("wrote", out)


if __name__ == "__main__":
    main()
