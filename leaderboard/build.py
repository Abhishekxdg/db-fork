"""Collect the newest result per system and machine for the page.

leaderboard.site writes the result as data.js into the phase's generated directory;
`python -m leaderboard.build` delegates to it and builds the development site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from leaderboard.validate import BOARD, validate


def newest(board: Path, sources: set[str] | None = None) -> list[Path]:
    """For each system, machine, and execution profile, the file in the newest date directory,
    among every result file or only the given board-relative sources."""
    found: dict[tuple[str, str, str], Path] = {}
    for path in sorted(board.glob("*/results/*/*.json")):  # YYYYMMDD directories sort by date
        if sources is None or path.relative_to(board).as_posix() in sources:
            profile = "local_single_host"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("execution_profile"), str):
                    profile = raw["execution_profile"]
            except Exception:
                pass
            found[path.parts[-4], path.stem, profile] = path
    return list(found.values())


def build(board: Path = BOARD, sources: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Entries to rank, and excluded entries with their reasons. A newest file that
    is an error or invalid excludes its system and machine; no older file stands in.
    With sources, only those files compete."""
    entries, excluded = [], []
    for path in newest(board, sources):
        source = path.relative_to(board).as_posix()
        if problems := validate(path, board):
            reason = "invalid: " + "; ".join(problems)
        elif "error" in (data := json.loads(path.read_text())):
            reason = data["error"]
        else:
            entries.append({**data, "source": source})
            continue
        excluded.append({"system": path.parts[-4], "machine": path.stem, "source": source, "reason": reason})
    return entries, excluded


def main() -> int:
    """The historical entry point builds the development site through the assembler."""
    from leaderboard import site  # the assembler imports this module
    print("leaderboard.build: assembling the development site (python -m leaderboard.site --phase local)", file=sys.stderr)
    return site.main(["--phase", "local"])


if __name__ == "__main__":
    sys.exit(main())
