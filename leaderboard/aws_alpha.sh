#!/usr/bin/env bash
# Manage the collapsible personal-AWS 'alpha' deployment phase for the BranchBench leaderboard.
#
# Usage:
#   ./leaderboard/aws_alpha.sh up [--region us-east-1] [--instance-type i4i.2xlarge] [--spot]
#   ./leaderboard/aws_alpha.sh run [--preset mini] [--systems "dolt dolt_mysql"] [--collapse-after]
#   ./leaderboard/aws_alpha.sh deploy-site
#   ./leaderboard/aws_alpha.sh status
#   ./leaderboard/aws_alpha.sh collapse

set -euo pipefail

BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$BOARD_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  exec uv run --frozen python -m leaderboard.aws_alpha "$@"
else
  PYTHON="${PYTHON:-python3}"
fi

exec "$PYTHON" -m leaderboard.aws_alpha "$@"
