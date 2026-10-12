"""Mock microbench rows for the local site.

Until a hosted system has a connector and real runs, leaderboard/mock/<system>.json
holds sample numbers for it. This module is the only code that reads those files:
the local site build appends its rows, which the page shows like any other row, and
the production build never calls it. A system's first result file, even an error
record, retires its mock.
"""

from __future__ import annotations

import json
from pathlib import Path


def entries(board: Path) -> list[dict]:
    """A row per mock whose system has no result file yet: its identity from
    system.json and the mock's numbers."""
    measured = {path.parts[-4] for path in board.glob("*/results/*/*.json")}
    return [{**json.loads((board / path.stem / "system.json").read_text()), **json.loads(path.read_text())}
            for path in sorted((board / "mock").glob("*.json")) if path.stem not in measured]
