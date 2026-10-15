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
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
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


def _lat_ticks(lo, hi):
    """Doubling ticks in readable units: 1, 2, 4, ... 512 ms, then 1, 2, 4, ... s."""
    ticks = [2 ** k / 1000 for k in range(0, 10)] + [2 ** k for k in range(0, 8)]
    return [t for t in ticks if lo / 1.3 <= t <= hi * 1.3]


def _bin_fmt(v, _=None):
    a = abs(v); sign = "-" if v < 0 else ""
    for unit, div in (("GB", 2 ** 30), ("MB", 2 ** 20), ("KB", 2 ** 10)):
        if a >= div:
            x = a / div
            return f"{sign}{x:g} {unit}"
    return f"{sign}{a:g} B"


def _latency_axis(ax, lo, hi):
    ax.set_yscale("log", base=2)
    ax.set_yticks(_lat_ticks(lo, hi))
    ax.yaxis.set_major_formatter(FuncFormatter(_secs_fmt))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_ylim(lo / 1.4, hi * 1.4)


def _storage_axis2(ax, values):
    """Bytes on a base-2 scale: ticks every 4x from 1 KB (16 KB, 64 KB, 256 KB,
    1 MB, ...), mirrored below zero when something was freed."""
    mags = [abs(v) for v in values if v]
    top = max(mags) if mags else 2 ** 20
    lin = 2 ** 10
    pos = [lin * 4 ** k for k in range(0, 12) if lin * 4 ** k <= top * 4]
    if values and min(values) > 0:
        ax.set_yscale("log", base=2)
        ticks = [t for t in pos if t >= min(values) / 4]
        ax.set_ylim(min(values) / 1.5, top * 1.5)
    else:
        ax.set_yscale("symlog", base=2, linthresh=lin, linscale=0.4)
        neg = [-t for t in reversed(pos) if any(v <= -t / 4 for v in values)]
        ticks = neg + [0] + [t for t in pos if any(v >= t / 4 for v in values)]
        ticks = [t for t in ticks if abs(t) != lin]          # +-1 KB would sit on the 0 B label
        lo = min(values)
        ax.set_ylim(lo * 1.5 if lo < 0 else -lin, top * 1.5)
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


def _panel(ax, axis, points, key):
    """One panel: every system's median along the axis; returns the values drawn."""
    vals = []
    for sysname in ORDER:
        xs, med = _series_median(points.get(sysname, {}), axis, key)
        if len(xs) < 2:
            continue
        ax.plot(xs, med, "-", color=COLORS.get(sysname, "#333"), lw=0.8, marker=MARKERS.get(sysname, "o"),
                ms=7, mec="white", mew=0.6, label=sysname)
        vals += list(med)
    ax.set_xticks(range(len(axis.values)))
    ax.set_xticklabels([f"{v:g}" if isinstance(v, float) else f"{v:,}" for v in axis.values], fontsize=8.5)
    ax.set_xlim(-0.35, len(axis.values) - 0.65)
    ax.tick_params(axis="y", labelsize=8.5)
    ax.grid(color="#eee", lw=0.6); ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_linewidth(0.8); sp.set_color("#444")
    return vals


def _legend(fig, ax):
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), fontsize=10, frameon=False,
               bbox_to_anchor=(0.5, 1.0), handlelength=2.4, columnspacing=1.6)


def plot_composite(stories, points, outdir, name, with_storage):
    """Panels side by side, one per story; latency row and, with storage,
    a storage row; one legend for the figure; each panel titled with its
    letter and the operation's cost."""
    n = len(stories)
    nrow = 2 if with_storage else 1
    fig, axes = plt.subplots(nrow, n, figsize=(3.8 * n, 3.2 * nrow + 0.7), squeeze=False)
    for j, st in enumerate(stories):
        axis = next(a for a in AXES if a.verb == st["verb"] and a.name == st["axis"])
        ax_l = axes[0, j]
        lat = _panel(ax_l, axis, points, "latency")
        _latency_axis(ax_l, min(v for v in lat if v > 0), max(lat))
        ax_l.set_title(f"({chr(97 + j)}) {st['panel']}", loc="left", fontsize=11, color=INK,
                       fontweight="bold", pad=5)
        if with_storage:
            ax_s = axes[1, j]
            sto = _panel(ax_s, axis, points, "storage")
            _storage_axis2(ax_s, sto)
            ax_s.axhline(0, color="#ccc", lw=0.8)
            ax_s.set_xlabel(axis.label, fontsize=12, fontweight="bold")
            ax_l.tick_params(axis="x", labelbottom=False)
        else:
            ax_l.set_xlabel(axis.label, fontsize=12, fontweight="bold")
    axes[0, 0].set_ylabel("latency", fontsize=12, fontweight="bold")
    if with_storage:
        axes[1, 0].set_ylabel("storage added", fontsize=12, fontweight="bold")
    _legend(fig, axes[0, 0])
    fig.subplots_adjust(top=0.9 if with_storage else 0.82, bottom=0.14 if with_storage else 0.22,
                        left=0.07, right=0.99, wspace=0.32, hspace=0.12)
    out = os.path.join(outdir, name + ".png")
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


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
    print("wrote", plot_composite(ops, points, a.outdir, "micro_ops", with_storage=True))
    print("wrote", plot_composite(branch, points, a.outdir, "micro_branch", with_storage=False))


if __name__ == "__main__":
    main()
