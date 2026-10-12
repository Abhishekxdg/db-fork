#!/usr/bin/env python3
"""
Plot per-operation latency of the microbenchmark, one bar per backend.

Reads every result parquet under the given directories (the files
runner2.py and scripts/run_single_thread_bench.sh write) and draws two
panels: the data operations (READ, INSERT, UPDATE, DELETE, RANGE_READ,
RANGE_UPDATE) and the branch operations (the nine verbs plus CONNECT).
Bars are median latency on a log axis with a whisker to the 95th
percentile. A verb a backend does not implement is marked "n/a".

Usage:
    python scripts/plotting/plot_micro_ops.py --data-dir run_stats/micro --outdir figures/
    python scripts/plotting/plot_micro_ops.py --data-dir a/ b/ --outdir figures/ --branches 16
"""

import argparse
import glob
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_OPS = ["READ", "INSERT", "UPDATE", "DELETE", "RANGE_READ", "RANGE_UPDATE"]
BRANCH_OPS = ["BRANCH", "CONNECT", "COMMIT", "DIFF", "LOG", "MERGE", "REBASE",
              "REVERT", "RESET", "DELETE_BRANCH"]
LABELS = {"BRANCH": "Branch", "CONNECT": "Connect", "COMMIT": "Commit", "DIFF": "Diff",
          "LOG": "Log", "MERGE": "Merge", "REBASE": "Rebase", "REVERT": "Revert",
          "RESET": "Reset", "DELETE_BRANCH": "Delete", "READ": "Read", "INSERT": "Insert",
          "UPDATE": "Update", "DELETE": "Delete", "RANGE_READ": "Range read",
          "RANGE_UPDATE": "Range update"}

BACKEND_COLORS = {
    "dolt": "#0072B2", "dolt_mysql": "#56B4E9", "neon": "#D55E00", "xata": "#CC79A7",
    "file_copy": "#E69F00", "seekdb": "#009E73", "matrixone": "#999999",
    "chronos": "#000000", "terminusdb": "#882255",
}


def _operation(row) -> str:
    """Benchmark operation a result row belongs to, or "" for rows that are
    not the measured operation (the EXEC summary row of a statement)."""
    name = row.op_name
    if name == "DELETE":
        return "DELETE_BRANCH"
    if name == "DELETE_ROWS":
        return "DELETE"
    if name in ("READ", "UPDATE"):
        # The range operations filter on a row-value comparison.
        return f"RANGE_{name}" if ">=" in (row.sql_query or "") else name
    if name in BRANCH_OPS or name == "INSERT":
        return name
    return ""


def load(data_dirs: list, branches: int = None) -> pd.DataFrame:
    backends, branch_counts = {}, {}
    for d in data_dirs:
        for path in glob.glob(os.path.join(d, "**", "*_summary.json"), recursive=True):
            with open(path) as f:
                summary = json.load(f)
            backends[summary["run_id"]] = summary["backend"].lower()
            branch_counts[summary["run_id"]] = summary.get("num_branches")
    frames = []
    for d in data_dirs:
        for path in glob.glob(os.path.join(d, "**", "*.parquet"), recursive=True):
            if path.endswith("_setup.parquet"):
                continue
            df = pd.read_parquet(path)
            if df.empty:
                continue
            run_id = df.run_id.iloc[0]
            if run_id not in backends:
                print(f"skipping {path}: no summary json names its backend")
                continue
            if branches is not None and branch_counts[run_id] != branches:
                continue
            df["backend"] = backends[run_id]
            df["operation"] = [_operation(r) for r in df.itertuples()]
            frames.append(df[df.operation != ""][["backend", "operation", "status", "latency"]])
    if not frames:
        raise SystemExit("no microbenchmark results found")
    return pd.concat(frames, ignore_index=True)


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (backend, operation), g in df.groupby(["backend", "operation"]):
        ok = g[g.status == "OK"]
        rows.append({
            "backend": backend, "operation": operation, "ok": len(ok),
            "unsupported": int((g.status == "UNSUPPORTED").sum()),
            "failed": int((g.status == "FAILED").sum()),
            "median_ms": ok.latency.median() * 1000 if len(ok) else np.nan,
            "p95_ms": ok.latency.quantile(0.95) * 1000 if len(ok) else np.nan,
        })
    return pd.DataFrame(rows)


def _panel(ax, stats: pd.DataFrame, operations: list, backends: list, title: str) -> None:
    width = 0.8 / len(backends)
    floor = 0.01   # ms: where a zero-cost bar is drawn on the log axis
    for i, backend in enumerate(backends):
        for j, operation in enumerate(operations):
            cell = stats[(stats.backend == backend) & (stats.operation == operation)]
            x = j + (i - (len(backends) - 1) / 2) * width
            if cell.empty:
                continue
            cell = cell.iloc[0]
            if not cell.ok:
                ax.text(x, floor * 1.3, "n/a" if cell.unsupported else "fail", rotation=90,
                        ha="center", va="bottom", fontsize=7, color="#666666")
                continue
            if cell.median_ms < floor:
                # Too fast for the axis (a stateless connection switch).
                ax.text(x, floor * 1.3, f"<{floor:g} ms", rotation=90, ha="center",
                        va="bottom", fontsize=7, color=BACKEND_COLORS.get(backend, "#444444"))
            median = max(cell.median_ms, floor)
            ax.bar(x, median, width * 0.92, color=BACKEND_COLORS.get(backend, "#444444"),
                   label=backend if j == 0 else None)
            ax.plot([x, x], [median, max(cell.p95_ms, median)], color="#333333", lw=0.8)
    ax.set_yscale("log")
    ax.set_ylim(bottom=floor)
    ax.set_xticks(range(len(operations)))
    ax.set_xticklabels([LABELS[o] for o in operations])
    ax.set_ylabel("Latency (ms), median and p95")
    ax.set_title(title)
    ax.grid(axis="y", which="major", alpha=0.3)
    ax.set_axisbelow(True)


def plot(stats: pd.DataFrame, outdir: str) -> str:
    backends = sorted(stats.backend.unique())
    fig, axes = plt.subplots(2, 1, figsize=(max(9, 1.1 * len(backends) + 8), 8))
    _panel(axes[0], stats, DATA_OPS, backends, "Data operations")
    _panel(axes[1], stats, BRANCH_OPS, backends, "Branch operations")
    handles, labels = axes[1].get_legend_handles_labels()
    if not handles:
        handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=min(len(backends), 9), frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "micro_ops_latency.png")
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", nargs="+", required=True,
                        help="Directories with microbenchmark parquet and summary files")
    parser.add_argument("--outdir", required=True, help="Directory for the figure and the table")
    parser.add_argument("--branches", type=int, default=None,
                        help="Only runs set up with this many branches (default: all)")
    args = parser.parse_args()

    stats = summarise(load(args.data_dir, args.branches))
    order = {op: i for i, op in enumerate(DATA_OPS + BRANCH_OPS)}
    stats = stats.sort_values(["backend", "operation"],
                              key=lambda s: s.map(order) if s.name == "operation" else s)
    os.makedirs(args.outdir, exist_ok=True)
    table = os.path.join(args.outdir, "micro_ops_latency.csv")
    stats.to_csv(table, index=False, float_format="%.3f")
    print(stats.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print(f"\nWrote {plot(stats, args.outdir)} and {table}")


if __name__ == "__main__":
    main()
