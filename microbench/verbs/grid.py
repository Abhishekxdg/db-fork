"""The experiment grid of the branch-verb microbenchmark.

For each of the eight verbs, the axes its latency and storage cost can
depend on (what separates the systems: a change-proportional cost from a
state-proportional one, a pointer move from a row restore) and the values
each axis takes. One figure per (verb, axis) pair.

Every point varies one parameter and holds the rest at BASE, so the
curves of one verb's figures meet at the base point. The "scale" axis is
the size of the CH-benCHmark data (W warehouses); it needs its own data
load, so a run is one (backend, W): at W = BASE["scale"] it runs every
point, at any other W only the scale points.
"""

from dataclasses import dataclass, field

VERBS = ("branch", "commit", "delete", "diff", "merge", "rebase", "reset", "revert")

# Parameters every point starts from.
BASE = {
    "scale": 1,        # W warehouses of CH-benCHmark data
    "rows": 100,       # rows the verb carries (changed rows per side)
    "age": 0,          # branch: commits behind the head the source commit is (0 = head)
    "live": 1,         # branch: live branches already on the database
    "commits": 1,      # delete/rebase: commits on the branch
    "written": 100,    # delete: rows written on the branch before it is deleted
    "upstream": 100,   # rebase: rows the upstream moved by
    "conflict": 0.0,   # merge: share of each side's rows the other side also changed
    "distance": 100,   # reset: rows written after the target commit
}
# Deferred storage: updates written on the branch after a branch/commit.
FOLLOW_UP_ROWS = 1000
# Rows of ``stock`` in one warehouse (CHScale.items): the key space the
# verbs' updates are drawn from.
STOCK_ROWS = 100_000


@dataclass(frozen=True)
class Axis:
    verb: str
    name: str
    values: tuple
    label: str
    # Parameters held at a value other than BASE's for this axis.
    fixed: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.verb}:{self.name}"


# Counts (data size, branches, commits) step by powers of two; row counts
# span four orders of magnitude and step by decades.
SCALES = (1, 2, 4, 8)
ROWS = (1, 10, 100, 1_000, 10_000)
POW2 = (1, 2, 4, 8, 16, 32, 64, 128)

AXES = (
    Axis("branch", "scale", SCALES, "data size (warehouses)"),
    Axis("branch", "age", (0,) + POW2, "source commit age (commits behind head)"),
    Axis("branch", "live", POW2, "live branches"),
    Axis("commit", "rows", ROWS, "rows changed"),
    Axis("commit", "scale", SCALES, "data size (warehouses)"),
    Axis("delete", "scale", SCALES, "data size (warehouses)"),
    Axis("delete", "commits", (0,) + POW2, "commits on the branch"),
    Axis("delete", "written", (0, 100, 1_000, 10_000, 100_000), "rows written on the branch"),
    Axis("diff", "rows", ROWS, "rows changed"),
    Axis("diff", "scale", SCALES, "data size (warehouses)"),
    Axis("merge", "rows", ROWS, "rows changed per side"),
    Axis("merge", "scale", SCALES, "data size (warehouses)"),
    Axis("merge", "conflict", (0.0, 0.1, 0.25, 0.5, 1.0), "share of conflicting rows", fixed={"rows": 1_000}),
    Axis("rebase", "rows", ROWS, "rows changed on the branch"),
    Axis("rebase", "commits", POW2, "commits on the branch", fixed={"rows": 1_000}),
    Axis("rebase", "scale", SCALES, "data size (warehouses)"),
    Axis("reset", "distance", ROWS, "rows written past the target"),
    Axis("reset", "scale", SCALES, "data size (warehouses)"),
    Axis("revert", "rows", ROWS, "rows in the reverted commit"),
    Axis("revert", "scale", SCALES, "data size (warehouses)"),
)


@dataclass(frozen=True)
class Point:
    axis: Axis
    value: object

    @property
    def id(self) -> str:
        return f"{self.axis.verb}:{self.axis.name}={self.value}"

    @property
    def params(self) -> dict:
        p = dict(BASE)
        p.update(self.axis.fixed)
        p[self.axis.name] = self.value
        return p


def axes_for(verbs=None, axis_names=None):
    """The axes selected by verb and by axis name (None = all)."""
    out = []
    for a in AXES:
        if verbs and a.verb not in verbs:
            continue
        if axis_names and a.name not in axis_names and a.key not in axis_names:
            continue
        out.append(a)
    return out


def points_for_run(scale: int, verbs=None, axis_names=None) -> list:
    """The points a run at data size ``scale`` executes: every point of
    every non-scale axis when ``scale`` is the base size, and the one
    scale point of each scale axis that names this size."""
    pts = []
    for a in axes_for(verbs, axis_names):
        if a.name == "scale":
            if scale in a.values:
                pts.append(Point(a, scale))
        elif scale == BASE["scale"]:
            pts.extend(Point(a, v) for v in a.values)
    return pts


def key_ranges(rep: int, n: int, overlap: float = 0.0, stock_rows: int = STOCK_ROWS) -> tuple:
    """Two key ranges of ``n`` stock rows for one rep, as (start_a,
    start_b): side b's range starts ``(1 - overlap) * n`` after side a's,
    so the two share ``overlap * n`` rows (0: disjoint, 1: identical).
    Reps rotate through the key space so no two reps of a point rewrite
    the same rows. Ranges are clamped to the warehouse."""
    n = max(1, int(n))
    span = 2 * n
    if span >= stock_rows:
        start_a = 1
    else:
        slots = (stock_rows - span) // span + 1
        start_a = 1 + (rep % slots) * span
    start_b = start_a + int(round((1.0 - overlap) * n))
    start_b = min(start_b, stock_rows - n + 1)
    return start_a, start_b
