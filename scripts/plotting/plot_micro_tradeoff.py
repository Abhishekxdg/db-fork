"""Latency against storage for the RQ2 operations, one panel each.

Every system draws one path per panel: its median latency over its median
storage delta at each value of the varied condition, markers growing with
the condition. A path that stays put is an operation whose cost does not
depend on the condition; a path that climbs pays in time, one that moves
right pays in storage.

Three figures: ``micro_tradeoff`` (commit: rows changed, merge: data size,
rebase and delete: commits on the branch) at the paper's text width,
``micro_branch_tradeoff`` (branch: live branches, branch: commits behind
the head) at its column width sharing one latency axis, and
``micro_tradeoff_all``, the six panels in two rows of three at the text width.

    python scripts/plotting/plot_micro_tradeoff.py --run-dir run_stats_final_v2_micro \\
        --outdir figures/final_v2_micro/stories
"""

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
matplotlib.rcParams["pdf.fonttype"] = 42
import numpy as np  # noqa: E402
from matplotlib.legend_handler import HandlerTuple  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.offsetbox import AnchoredOffsetbox, HPacker, TextArea, VPacker  # noqa: E402
from matplotlib.ticker import FuncFormatter, NullLocator  # noqa: E402

from plot_micro_stories import F, MARKERS, ORDER, _bin_fmt, _bracket, _save, _xfmt  # noqa: E402
from plot_micro_verbs import _secs_fmt, load  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from microbench.verbs.grid import AXES  # noqa: E402

# The paper's hand-made figures (figures/*_wu.svg): their palette, greys,
# fonts and axis style, which every figure here follows.
WU_COLORS = {"Doltgres": "#1f5fb4", "Dolt": "#62aeea", "Chronos": "#d29400", "MatrixOne": "#e0432c",
             "file_copy": "#5a5852", "SeekDB": "#9a5ad8", "Neon": "#11a37a"}
WU_INK, WU_NOTE, WU_TICK, WU_AXIS, WU_GRID = "#0b0b0b", "#52514e", "#6f6d68", "#8a8882", "#ecebe7"
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["Helvetica Neue", "Helvetica", "Arial", "Liberation Sans",
                                          "TeX Gyre Heros", "Nimbus Sans", "DejaVu Sans"]

# "cond" names the condition under the panel title; "short" is the form
# that fits a panel of the six-across figure.
PANELS = [
    {"verb": "commit", "axis": "rows", "op": "Commit", "cond": "rows changed", "short": "rows changed"},
    {"verb": "merge", "axis": "scale", "op": "Merge", "cond": "warehouses", "short": "warehouses"},
    {"verb": "rebase", "axis": "commits", "op": "Rebase", "cond": "commits on the branch", "short": "commits"},
    {"verb": "delete", "axis": "commits", "op": "Delete", "cond": "commits on the branch", "short": "commits"},
]
BRANCH_PANELS = [
    {"verb": "branch", "axis": "live", "op": "Branch", "cond": "live branches", "short": "live branches"},
    {"verb": "branch", "axis": "age", "op": "Branch", "cond": "commits behind head", "short": "behind head"},
]
# Marker diameter (pt) from the first to the last value of the condition.
MS_MIN, MS_MAX = 2.6, 7.5
SIZE_KEY = "marker grows with the condition"
MAX_TICKS = 4      # latency labels
_SCALE = [1.0]     # font scale of the figure being drawn (appendix figures use a larger one)


def _fs(key):
    return F[key] * _SCALE[0]
MAX_XTICKS = 3     # storage labels, the panels are narrow


def _axis(spec):
    return next(a for a in AXES if a.verb == spec["verb"] and a.name == spec["axis"])


def _path(points, axis):
    """[(x index, storage median, latency median)] of one system along
    the axis, medians over the timed OK reps (as in plot_micro_stories)."""
    out = []
    for i, v in enumerate(axis.values):
        p = points.get(f"{axis.verb}:{axis.name}={v}")
        if not (p and p["status"] == "ok"):
            continue
        lat = [r["latency"] for r in p["reps"] if r.get("status") == "OK" and "latency" in r]
        sto = [r["storage_delta"] for r in p["reps"] if "storage_delta" in r]
        if lat and sto:
            out.append((i, float(np.median(sto)), float(np.median(lat))))
    return out


def _sizes(n, idx):
    """Marker diameters growing with the condition's rank."""
    if n <= 1:
        return [MS_MAX]
    return [MS_MIN + (MS_MAX - MS_MIN) * i / (n - 1) for i in idx]


BYTE_CANDS = [2 ** k for k in range(0, 41)]


def _byte_fmt(v, _=None):
    """Bytes as in the hand-made figures: "0", "4 KB", "\u221216 MB"."""
    if v == 0:
        return "0"
    return _bin_fmt(v).replace("-", "\u2212")


def _storage_xaxis(ax, values, max_ticks=MAX_XTICKS):
    """Bytes on a base-2 x axis: log when every value is positive, else
    symlog (linear within 1 KB of zero) with power-of-two labels on both
    sides, few enough to read in a narrow panel."""
    lin = 2 ** 10
    pos = [v for v in values if v > 0]
    neg = [-v for v in values if v < 0]
    if values and min(values) > 0:
        ticks = _bracket(BYTE_CANDS, min(values), max(values), max_ticks)
        ax.set_xscale("log", base=2)
        ax.set_xlim(ticks[0] / 1.4, ticks[-1] * 1.4)
    else:
        ax.set_xscale("symlog", base=2, linthresh=lin, linscale=0.35)

        def side(extreme, m):
            if extreme <= 0:
                return []
            top = 2 ** math.ceil(math.log2(max(extreme, 2 * lin)))
            out = []
            while top >= 2 * lin:
                out.append(top)
                top //= 2 ** m
            return sorted(out)

        for m in range(1, 40):
            p, n = side(max(pos, default=0), m), side(max(neg, default=0), m)
            if len(p) + len(n) + 1 <= min(max_ticks, 3):   # the extremes and zero read best
                break
        ticks = [-t for t in reversed(n)] + [0] + p
        ax.set_xlim(-n[-1] * 1.6 if n else -lin, (p[-1] if p else lin) * 1.6)
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(FuncFormatter(_byte_fmt))
    ax.xaxis.set_minor_locator(NullLocator())
    return ticks


def _latency_yaxis(ax, lo, hi):
    ticks = _bracket([2 ** k / 1000 for k in range(-2, 10)] + [2 ** k for k in range(0, 12)], lo, hi, MAX_TICKS + 1)
    ax.set_yscale("log", base=2)
    ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(FuncFormatter(_secs_fmt))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylim(ticks[0] / 1.3, ticks[-1] * 1.3)


LAT_CANDS = [2 ** k / 1000 for k in range(-2, 10)] + [2 ** k for k in range(0, 12)]


def _latency_yaxis_tight(ax, lo, hi, max_n=6):
    """Power-of-two latency ticks anchored at the first candidate above the
    data's maximum, stepping down by a uniform factor until at most max_n
    ticks cover the data; the limits hug the data, not the ticks, so a
    shared axis wastes no height at the top."""
    # Decades, as the hand-made figures label latency (10 ms, 100 ms, 1 s).
    lo_e, hi_e = math.floor(math.log10(lo / 1.6)), math.ceil(math.log10(hi * 1.3))
    ticks = [10.0 ** k for k in range(lo_e, hi_e + 1) if lo / 1.6 <= 10.0 ** k <= hi * 1.3]
    ax.set_yscale("log", base=10)
    ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(FuncFormatter(_secs_fmt))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylim(lo / 1.6, hi * 1.7)


def _draw(ax, axis, points):
    """Every system's path along the axis; returns the storage and latency
    values drawn, for the axes."""
    n = len(axis.values)
    xs_all, ys_all = [], []
    paths = {}
    for sysname in ORDER:
        path = _path(points.get(sysname, {}), axis)
        if len(path) < 2:
            continue
        idx, xs, ys = zip(*path)
        paths[sysname] = (idx, xs, ys)
        color = WU_COLORS.get(sysname, "#333")
        ax.plot(xs, ys, "-", color=color, lw=0.85, alpha=0.9, zorder=2)
        for x, y, ms in zip(xs, ys, _sizes(n, idx)):
            ax.plot([x], [y], linestyle="none", marker=MARKERS[sysname], ms=ms, color=color,
                    mec="white", mew=0.6, zorder=3, clip_on=False)
        xs_all += xs
        ys_all += ys
    return xs_all, ys_all, paths


FIRST_OFFSETS = [(-7, 0), (-7, -6), (-7, 6), (0, -9), (7, 0)]
LAST_OFFSETS = [(7, 0), (7, 6), (7, -6), (0, 9), (-7, 0)]


def _note_ends(ax, axis, paths):
    """Write the first and last value of the condition beside the two ends
    of the path that moves most (as the hand-made figures do), so the
    reader sees which way the marker grows. Each note goes on the side of
    its marker that is farthest from every other marker in the panel; call
    after the axes are scaled, the choice is made in display space."""
    def extent(p):
        idx, xs, ys = p
        dx = math.log2(max(abs(xs[-1]), 1024) / max(abs(xs[0]), 1024)) if xs[0] * xs[-1] > 0 else 0
        return abs(dx) + abs(math.log2(ys[-1] / ys[0]))
    if not paths:
        return
    name = max(paths, key=lambda s: extent(paths[s]))
    idx, xs, ys = paths[name]
    pts = np.array([ax.transData.transform((x, y)) for s in paths for x, y in zip(*paths[s][1:])])
    px = ax.figure.dpi / 72.0
    kw = dict(fontsize=_fs("tick"), color=WU_NOTE, va="center", zorder=4, textcoords="offset points",
              bbox=dict(boxstyle="square,pad=0.05", fc="white", ec="none", alpha=0.8))
    for (x, y), label, offsets in ((( xs[0], ys[0]), _xfmt(axis.values[idx[0]]), FIRST_OFFSETS),
                                   ((xs[-1], ys[-1]), _xfmt(axis.values[idx[-1]]), LAST_OFFSETS)):
        here = ax.transData.transform((x, y))
        others = pts[np.linalg.norm(pts - here, axis=1) > 0.5]
        box = ax.bbox
        def anchor(off):
            # The note's centre: the offset plus half a label further out.
            return here + np.array(off) * px + np.sign(off) * np.array([9.0, 3.0]) * px
        half_w = 0.3 * len(label) * _fs("tick") * px      # about half the label's width in pixels
        def inside(off):
            cx, cy = anchor(off)
            return box.x0 + half_w < cx < box.x1 - half_w and box.y0 + 4 < cy < box.y1 - 4
        def room(off):
            return np.min(np.linalg.norm(others - anchor(off), axis=1)) if len(others) else 1e9
        candidates = [o for o in offsets if inside(o)] or list(offsets)
        dx, dy = max(candidates, key=room)
        ax.annotate(label, (x, y), xytext=(dx, dy), ha="left" if dx > 0 else "right" if dx < 0 else "center", **kw)


def _frees_adds_note(ax, xs_all, ys_all):
    """"<- frees | adds ->" beside the zero line, at the top of the panel
    unless markers sit there, then at the bottom, else not at all."""
    pts = np.array([ax.transData.transform((x, y)) for x, y in zip(xs_all, ys_all)])
    x0 = ax.transData.transform((0, ys_all[0]))[0]
    box = ax.bbox
    for yfrac, va in ((0.985, "top"), (0.02, "bottom")):
        lo_y = box.y1 - 0.22 * box.height if va == "top" else box.y0
        hi_y = box.y1 if va == "top" else box.y0 + 0.22 * box.height
        near = (np.abs(pts[:, 0] - x0) < 0.45 * box.width) & (pts[:, 1] > lo_y) & (pts[:, 1] < hi_y)
        if not near.any():
            kw = dict(fontsize=_fs("tick"), color=WU_NOTE, transform=ax.get_xaxis_transform(), va=va, zorder=4)
            ax.text(0, yfrac, "\u2190 frees\u2009", ha="right", **kw)
            ax.text(0, yfrac, "\u2009adds \u2192", ha="left", **kw)
            return


def _decorate(ax, xs_all, ys_all, lat_lo, lat_hi, tight_y=False, xticks_max=MAX_XTICKS, frees_note=True):
    _storage_xaxis(ax, xs_all, xticks_max)
    (_latency_yaxis_tight if tight_y else _latency_yaxis)(ax, lat_lo, lat_hi)
    if min(xs_all) < 0:
        ax.axvline(0, color=WU_AXIS, lw=0.6, zorder=1)
        if frees_note:
            _frees_adds_note(ax, xs_all, ys_all)
    ax.tick_params(axis="both", labelsize=_fs("tick"), length=2.5, width=0.5, pad=2, colors=WU_TICK)
    ax.grid(color=WU_GRID, lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_linewidth(0.6)
        ax.spines[side].set_color(WU_AXIS)


def _legend(fig, fontsize, handlelength, columnspacing, key_handlelength, key_y=None, y=1.0):
    """Marker and colour per system, then the size key (four circles growing
    from the smallest to the largest marker, spaced so each is readable).
    On one row when ``key_y`` is None: the two legends are drawn, measured
    and centred together; else the key goes on its own row at ``key_y``."""
    systems = [Line2D([], [], color=WU_COLORS[s], marker=MARKERS[s], ms=4.5, mec="white", mew=0.6, lw=0.85,
                      label=s) for s in ORDER]
    key = tuple(Line2D([], [], linestyle="none", marker="o", color=WU_TICK, ms=ms)
                for ms in np.linspace(MS_MIN, MS_MAX, 4))
    hmap = {tuple: HandlerTuple(ndivide=4, pad=0.35)}
    common = dict(frameon=False, handletextpad=0.5, fontsize=fontsize)
    if key_y is not None:
        fig.legend(systems, ORDER, ncol=len(systems), loc="upper center", bbox_to_anchor=(0.5, y),
                   handlelength=handlelength, columnspacing=columnspacing, **common)
        fig.legend([key], [SIZE_KEY], ncol=1, loc="upper center", bbox_to_anchor=(0.5, key_y),
                   handlelength=key_handlelength, handler_map=hmap, **common)
        return
    l1 = fig.legend(systems, ORDER, ncol=len(systems), loc="upper left", bbox_to_anchor=(0.0, y),
                    handlelength=handlelength, columnspacing=columnspacing, **common)
    l2 = fig.legend([key], [SIZE_KEY], ncol=1, loc="upper left", bbox_to_anchor=(0.5, y),
                    handlelength=key_handlelength, handler_map=hmap, **common)
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    w1 = l1.get_window_extent(r).width / fig.bbox.width
    w2 = l2.get_window_extent(r).width / fig.bbox.width
    gap = 0.01
    x0 = (1.0 - (w1 + gap + w2)) / 2
    l1.set_bbox_to_anchor((x0, y), transform=fig.transFigure)
    l2.set_bbox_to_anchor((x0 + w1 + gap, y), transform=fig.transFigure)


def _heading(ax, title, note, stacked=False):
    """"(a) Commit" in bold with the condition in grey on the same line
    (or, ``stacked``, on the line below), above the panel's top-left
    corner, as in the hand-made figures."""
    children = [TextArea(title, textprops=dict(fontsize=_fs("title"), fontweight="bold", color=WU_INK)),
                TextArea(note, textprops=dict(fontsize=6.3 * _SCALE[0], color=WU_NOTE))]
    box = (VPacker(children=children, align="left", pad=0, sep=1.5) if stacked
           else HPacker(children=children, align="baseline", pad=0, sep=4))
    ax.add_artist(AnchoredOffsetbox(loc="lower left", child=box, pad=0, borderpad=0.25, frameon=False,
                                    bbox_to_anchor=(0.0, 1.0), bbox_transform=ax.transAxes))


def _figure(points, outdir, panels, name, width, height, sharey, legend, margins, cond_key="cond",
            xlabel_once=False, xtick_size=6, tight_y=False, ncols=None, xticks_max=MAX_XTICKS,
            stacked_heading=False, notes=True, edge_labels_inward=False, fontscale=1.0):
    """A grid of panels, ``ncols`` per row (one row by default), lettered
    in reading order. ``xlabel_once``: a single storage label under the
    grid instead of one per panel; ``tight_y``: the shared latency axis
    hugs the data; ``xticks_max``: storage labels per panel."""
    _SCALE[0] = fontscale
    ncols = ncols or len(panels)
    nrows = math.ceil(len(panels) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(width, height), squeeze=False, sharey=sharey)
    flat = list(axes.flat)
    drawn = []
    for ax, spec in zip(flat, panels):
        axis = _axis(spec)
        xs, ys, paths = _draw(ax, axis, points)
        drawn.append((ax, spec, axis, xs, ys, paths))
    for ax in flat[len(panels):]:
        ax.set_visible(False)
    if sharey:
        shared = (min(min(d[4]) for d in drawn), max(max(d[4]) for d in drawn))
    for j, (ax, spec, axis, xs, ys, paths) in enumerate(drawn):
        lo, hi = shared if sharey else (min(ys), max(ys))
        _decorate(ax, xs, ys, lo, hi, tight_y=tight_y, xticks_max=xticks_max, frees_note=notes)
        if notes:
            _note_ends(ax, axis, paths)
        first, last = _xfmt(axis.values[0]), _xfmt(axis.values[-1])
        _heading(ax, f"({chr(97 + j)}) {spec['op']}", f"{spec[cond_key]} {first}\u2009\u2192\u2009{last}",
                 stacked=stacked_heading)
        if not xlabel_once:
            ax.set_xlabel("Storage Overhead", fontsize=_fs("label"), fontweight="bold", labelpad=2, color=WU_INK)
        ax.tick_params(axis="x", labelsize=xtick_size * fontscale)
        if j % ncols == 0:
            ax.set_ylabel("Latency", fontsize=_fs("label"), fontweight="bold", color=WU_INK)
        elif sharey:
            ax.tick_params(axis="y", length=0)
    if edge_labels_inward:
        # Keep a panel's outermost storage labels inside its own width, so
        # narrow gaps between panels stay clear.
        for ax, *_ in drawn:
            ticks = ax.xaxis.get_major_ticks()
            if len(ticks) >= 2:
                ticks[0].label1.set_ha("left")
                ticks[-1].label1.set_ha("right")
    _legend(fig, **legend)
    fig.subplots_adjust(**margins)
    if xlabel_once:
        x_mid = (margins.get("left", 0.1) + margins.get("right", 0.99)) / 2
        fig.text(x_mid, 0.008, "Storage Overhead", ha="center", va="bottom", fontsize=_fs("label"),
                 fontweight="bold", color=WU_INK)
    outs = _save(fig, outdir, name)
    _SCALE[0] = 1.0
    return outs


def plot(points, outdir, name="micro_tradeoff", width=7.0):
    """Commit, merge, rebase and delete side by side at the text width."""
    return _figure(points, outdir, PANELS, name, width, 2.3, sharey=False,
                   legend=dict(fontsize=F["legend"], handlelength=1.6, columnspacing=0.7, key_handlelength=4.2),
                   margins=dict(top=0.84, bottom=0.2, left=0.075, right=0.995, wspace=0.42), cond_key="short")


def plot_branch(points, outdir, name="micro_branch_tradeoff", width=3.35):
    """Branch against live branches and against the source commit's age,
    at the column width, one latency axis for both panels."""
    return _figure(points, outdir, BRANCH_PANELS, name, width, 2.2, sharey=True,
                   legend=dict(fontsize=5.5, key_y=0.95, handlelength=1.2, columnspacing=0.6,
                               key_handlelength=6.0),
                   margins=dict(top=0.78, bottom=0.2, left=0.165, right=0.985, wspace=0.12), cond_key="short")


def plot_all(points, outdir, name="micro_tradeoff_all", width=7.0):
    """All six panels in two rows of three at the text width (branch twice,
    then commit; merge, rebase, delete), wide and short, one latency axis."""
    return _figure(points, outdir, BRANCH_PANELS + PANELS, name, width, 2.7, sharey=True,
                   legend=dict(fontsize=F["legend"], handlelength=1.6, columnspacing=0.7, key_handlelength=4.2, y=1.015),
                   margins=dict(top=0.875, bottom=0.125, left=0.072, right=0.995, wspace=0.14, hspace=0.38),
                   cond_key="cond", xlabel_once=True, xtick_size=6.3, tight_y=True, ncols=3, xticks_max=4)


def plot_row(points, outdir, name="micro_tradeoff_row", width=7.0):
    """All six panels in one row at the text width, wider than tall,
    sharing one latency axis; the condition goes under the operation to fit."""
    left, right, wspace = 0.055, 0.997, 0.1
    panel_w = (right - left) * width / (6 + 5 * wspace)     # inches
    panel_h = 0.72                                           # shorter than wide
    header, footer = 0.50, 0.27                              # legend + two-line heading; ticks + label
    height = panel_h + header + footer
    return _figure(points, outdir, BRANCH_PANELS + PANELS, name, width, height, sharey=True,
                   legend=dict(fontsize=F["legend"], handlelength=1.6, columnspacing=0.7, key_handlelength=4.2, y=1.0),
                   margins=dict(top=1 - header / height, bottom=footer / height, left=left, right=right, wspace=wspace),
                   cond_key="short", xlabel_once=True, xtick_size=5.6, tight_y=True, xticks_max=3,
                   stacked_heading=True, notes=False, edge_labels_inward=True)


# Appendix figures, in the main figure's panel style: rows of three
# panels of its width (branch, merge, rebase) and rows of four (commit
# with diff, reset with revert, delete), lettered across the row in the
# order of the appendix captions and the grid table's figure column.
APPENDIX = [
    ("micro_all_branch", [("branch", "scale", "warehouses"), ("branch", "age", "commits behind the head"),
                          ("branch", "live", "live branches")]),
    ("micro_all_merge", [("merge", "rows", "rows changed per side"), ("merge", "scale", "warehouses"),
                         ("merge", "conflict", "share of conflicting rows")]),
    ("micro_all_rebase", [("rebase", "rows", "rows changed"), ("rebase", "commits", "commits on the branch"),
                          ("rebase", "scale", "warehouses")]),
    ("micro_all_commit", [("commit", "rows", "rows changed"), ("commit", "scale", "warehouses"),
                          ("diff", "rows", "rows changed"), ("diff", "scale", "warehouses")]),
    ("micro_all_reset", [("reset", "distance", "rows past target"), ("reset", "scale", "warehouses"),
                         ("revert", "rows", "rows reverted"), ("revert", "scale", "warehouses")]),
    ("micro_all_delete", [("delete", "scale", "warehouses"), ("delete", "commits", "commits"),
                          ("delete", "written", "rows written"), ("delete", "parallel", "deleted at once")]),
]
APPENDIX_PANEL_H = 1.0    # inches; the main figure's panels are 0.85


def plot_appendix(points, outdir, width=7.0):
    """The appendix figures: one row each at the text width, panels as in
    the main figure but taller."""
    outs = []
    for name, specs in APPENDIX:
        panels = [{"verb": v, "axis": a, "op": v.capitalize(), "cond": c, "short": c} for v, a, c in specs]
        n = len(panels)
        left, right = 0.072, 0.995
        wspace = 0.14 if n == 3 else 0.2
        header, footer = 0.34, 0.3
        height = APPENDIX_PANEL_H + header + footer
        outs += _figure(points, outdir, panels, name, width, height, sharey=True,
                        legend=dict(fontsize=F["legend"], handlelength=1.6, columnspacing=0.7, key_handlelength=4.2,
                                    y=1.015),
                        margins=dict(top=1 - header / height, bottom=footer / height, left=left, right=right,
                                     wspace=wspace),
                        cond_key="cond", xlabel_once=True, xtick_size=6.3, tight_y=True,
                        xticks_max=(4 if n == 3 else 3), notes=True, edge_labels_inward=True)
    return outs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    points, _ = load(a.run_dir)
    for out in (plot(points, a.outdir) + plot_branch(points, a.outdir) + plot_all(points, a.outdir)
                + plot_row(points, a.outdir) + plot_appendix(points, a.outdir)):
        print("wrote", out)


if __name__ == "__main__":
    main()
