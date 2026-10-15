"""One figure per (verb, axis) of the branch-verb microbenchmark: latency
on top, storage below, one line per system.

    python scripts/plotting/plot_micro_verbs.py --run-dir run_stats/micro_verbs_<date> \\
        --outdir figures/micro_verbs [--verbs branch,merge] [--axes rows]

Reads every micro_verbs_<backend>_w<W>_stats.json under --run-dir (all
backends, all data sizes). Latency is the median with a p10-p90 band
(log scale). Storage is the median bytes the verb added (negative:
freed), symlog scale. The deferred delta the driver records for branch
and commit (after 1,000 follow-up updates) is not drawn: on the
PostgreSQL-based systems its value depends on page reuse at the time of
the point, so it belongs in the text. A system with no line on a figure
is listed under it: unsupported, failed or not run.
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from plot_macro_cost import COLORS, NAMES  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from microbench.verbs.grid import AXES, BASE  # noqa: E402

ORDER = ["Doltgres", "Dolt", "Chronos", "MatrixOne", "file_copy", "SeekDB", "Neon"]
INK, MUTED = "#1f1f1f", "#6b6b6b"


def load(run_dir):
    """{system: {point_id: summary}} over every stats file, plus the
    control per (system, scale)."""
    points, control = {}, {}
    # Oldest file first, so a later measurement of the same point (a
    # supplement, a re-measurement) replaces the earlier one.
    for f in sorted(glob.glob(os.path.join(run_dir, "*", "micro_verbs_*_stats.json")), key=os.path.getmtime):
        d = json.load(open(f))
        sysname = NAMES.get(d["backend"], d["backend"])
        for p in d.get("points", []):
            if p["verb"] == "control":
                control[(sysname, d["scale"])] = (p.get("deferred") or {}).get("p50")
            else:
                points.setdefault(sysname, {})[p["id"]] = p
    return points, control


def _bytes_fmt(v, _=None):
    a = abs(v)
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if a >= div:
            x = v / div
            return f"{x:.0f} {unit}" if abs(x) >= 10 or x == int(x) else f"{x:.1f} {unit}"
    return f"{v:.0f} B"


def _secs_fmt(v, _=None):
    return f"{v * 1000:g} ms" if v < 1 else f"{v:g} s"


def _storage_axis(ax, values):
    """Symmetric log axis for bytes added or freed: linear below the
    smallest magnitude seen (at least 1 KB), decades above it, ticks at
    powers of ten on both sides of zero."""
    mags = [abs(v) for v in values if v]
    lin = max(1024.0, min(mags) if mags else 1024.0)
    if values and min(values) > 0:
        # Nothing freed and nothing at zero: a plain log axis.
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.set_ylim(min(values) / 3, max(values) * 3)
        return
    ax.set_yscale("symlog", linthresh=lin, linscale=0.5)
    top = max(mags) if mags else lin
    decades = [10 ** k for k in range(int(np.floor(np.log10(lin))), int(np.ceil(np.log10(top))) + 1)]
    ticks = [-d for d in reversed(decades) if any(v <= -d / 10 for v in values)] + [0] + \
            [d for d in decades if any(v >= d / 10 for v in values)]
    if any(v <= 0 for v in values):
        ticks = [t for t in ticks if abs(t) != decades[0]]   # +-smallest decade would sit on 0 B
    ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    # No empty half-panel below zero unless something was freed.
    lo, hi = (min(values), max(values)) if values else (0, lin)
    ax.set_ylim(bottom=-lin if lo >= 0 else lo * 3, top=hi * 3 if hi > 0 else lin)


def _series(points, axis, key, which="latency"):
    """x positions, medians, (lo, hi) for one system along one axis."""
    xs, med, lo, hi = [], [], [], []
    for i, v in enumerate(axis.values):
        p = points.get(f"{axis.verb}:{axis.name}={v}")
        q = (p or {}).get(key) or {}
        if p and p["status"] == "ok" and q:
            xs.append(i); med.append(q["p50"]); lo.append(q["p10"]); hi.append(q["p90"])
    return np.array(xs), np.array(med), np.array(lo), np.array(hi)


def plot_axis(axis, points, control, outdir):
    has_storage = axis.verb != "diff"
    fig, axes = plt.subplots(2 if has_storage else 1, 1, figsize=(4.2, 5.2 if has_storage else 2.9),
                             sharex=True, squeeze=False)
    ax_l = axes[0, 0]
    ax_s = axes[1, 0] if has_storage else None
    missing = []
    storage_vals, lat_vals = [], []
    for s in ORDER:
        pts = points.get(s, {})
        xs, med, lo, hi = _series(pts, axis, "latency")
        if len(xs) == 0:
            state = {pts[k]["status"] for k in pts if k.startswith(f"{axis.verb}:{axis.name}=")}
            missing.append(f"{s}: {', '.join(sorted(state)) if state else 'not run'}")
            continue
        c = COLORS.get(s, "#333")
        ax_l.plot(xs, med, "-o", color=c, ms=4, lw=1.6, label=s)
        ax_l.fill_between(xs, lo, hi, color=c, alpha=0.15, lw=0)
        lat_vals += list(lo) + list(hi)
        if ax_s is not None:
            xs2, med2, lo2, hi2 = _series(pts, axis, "storage")
            if len(xs2):
                # Median only: on the server-scope readings (SeekDB,
                # MatrixOne) background compaction makes the spread meaningless.
                ax_s.plot(xs2, med2, "-o", color=c, ms=4, lw=1.6)
                storage_vals += list(med2)
    ax_l.set_yscale("log")
    if lat_vals:
        lo_v, hi_v = min(v for v in lat_vals if v > 0), max(lat_vals)
        if hi_v / lo_v < 10:              # keep at least a decade in view
            ax_l.set_ylim(lo_v / 3, hi_v * 3)
    ax_l.yaxis.set_major_formatter(FuncFormatter(_secs_fmt))
    ax_l.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax_l.set_ylabel("latency", fontsize=8)
    ax_l.set_title(f"{axis.verb}: {axis.label}", fontsize=9.5, loc="left", color=INK)
    if ax_s is not None:
        _storage_axis(ax_s, storage_vals)
        ax_s.axhline(0, color="#ccc", lw=0.8)
        ax_s.set_ylabel("storage added", fontsize=8)
        ax_s.set_xlabel(axis.label, fontsize=8)
    else:
        ax_l.set_xlabel(axis.label, fontsize=8)
    for ax in axes.flat:
        ax.set_xticks(range(len(axis.values)))
        ax.set_xticklabels([f"{v:g}" if isinstance(v, float) else f"{v:,}" for v in axis.values], fontsize=7.5)
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(color="#eee", lw=0.6); ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    handles, labels = ax_l.get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, fontsize=6.5, frameon=False, ncol=4, loc="lower center",
                   bbox_to_anchor=(0.5, 1.0))
    notes = ["latency: median, band p10-p90; storage: median of the reps"]
    if missing:
        notes.append("no line: " + "; ".join(missing))
    fig.text(0.01, -0.01, "\n".join(notes), fontsize=6.3, color=MUTED, va="top")
    fig.tight_layout()
    out = os.path.join(outdir, f"{axis.verb}_{axis.name}.png")
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--verbs", help="comma-separated subset")
    ap.add_argument("--axes", help="comma-separated axis names")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    points, control = load(a.run_dir)
    if not points:
        print("no stats files under", a.run_dir)
        return
    verbs = a.verbs.split(",") if a.verbs else None
    names = a.axes.split(",") if a.axes else None
    for axis in AXES:
        if (verbs and axis.verb not in verbs) or (names and axis.name not in names):
            continue
        print("wrote", plot_axis(axis, points, control, a.outdir))


if __name__ == "__main__":
    main()
