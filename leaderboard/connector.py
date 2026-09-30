"""What a leaderboard system provides, and how the runner finds it.

A folder leaderboard/<name>/ with both system.json and connector.py is a runnable
system. A folder with only system.json is shown on the page but never run. The
runner imports nothing but these connector.py files, so it only loads code that
lives in this repository. Each defines CONNECTOR, an instance of a Connector.

main's microbench/runner2.py builds the database adapter from `backend`, so a
connector can only use a backend that main's WorkerContext already supports.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from pathlib import Path

from leaderboard.validate import BOARD
from microbench.runner2 import BackendInfo


class Connector(ABC):
    """One system's lifecycle. Everything provision creates is named after db, so delete can find it."""

    backend: int  # a microbench task2_pb2.Backend value
    needs_psql: bool
    version_sql: str
    # A hosted connector also sets keys, region, and service, and overrides preflight.

    @abstractmethod
    def scope(self) -> dict:
        """The deletion target, without secrets. Cleanup refuses a journal from another scope."""

    @abstractmethod
    def provision(self, db: str) -> BackendInfo:
        """Create an empty database named db."""

    @abstractmethod
    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        """Load the dataset into it."""

    @abstractmethod
    def delete(self, db: str) -> None:
        """Delete everything named after db."""

    def preflight(self) -> None:
        """A hosted connector checks that its keys reach the account before anything is created."""


def connector_names(board: Path = BOARD) -> list[str]:
    return sorted(path.parent.name for path in board.glob("*/connector.py") if (path.parent / "system.json").is_file())


def load_connector(name: str) -> Connector:
    if name not in connector_names():
        raise ValueError(f"no runnable system named {name!r}")
    return importlib.import_module(f"leaderboard.{name}.connector").CONNECTOR
