#!/bin/bash
# Run the BranchBench leaderboard sequentially on a single AWS worker VM (economic 1-VM model).
#
# Usage from repository root:
#   ./leaderboard/aws_worker.sh --machine aws-i4i-2xlarge [options]
#
# Options:
#   --machine <label>            Machine label recorded in result files (default: $machine)
#   --preset <mini|full>         Macrobench preset: mini or full (default: full)
#   --lane <all|micro|macro>     Which benchmark lanes to execute (default: all)
#   --systems "<sys1 sys2>"      Space- or comma-separated systems (default: all local systems,
#                                plus hosted systems when --client-location is given)
#   --client-location <loc>      Client location for hosted systems (e.g., "US East (N. Virginia)")
#   --data-mount <path>          Base directory on an XFS (reflink=1) volume for database storage
#   --batch-deadline-min <min>   Wall-clock timeout in minutes for the entire batch (default: 720)
#   --poweroff                   Power off the VM (sudo -n shutdown -h now) when the batch completes
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
    exec uv run --frozen python -m leaderboard.aws_worker "$@"
else
    exec "${PYTHON:-python3}" -m leaderboard.aws_worker "$@"
fi
