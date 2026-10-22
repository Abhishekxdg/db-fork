"""What a leaderboard system provides, and how the runner finds it.

A folder leaderboard/systems/<name>/ with both system.json and connector.py is a
runnable system. A folder without connector.py is never run. The local site can
still show it from mock data (leaderboard.mock). The runner imports nothing but
these connector.py files, so it only loads code that lives in this repository.
Each defines CONNECTOR, an instance of a Connector.

main's microbench/runner2.py builds the database adapter from `backend`, so a
connector can only use a backend that main's WorkerContext already supports.
"""

from __future__ import annotations

import importlib
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

from microbench.runner2 import BackendInfo

BOARD = Path(__file__).resolve().parent
SYSTEM_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_]*")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
IDENTITY_FIELDS = ("system", "proprietary", "hosted", "tuned", "tags")
SYSTEM_FIELDS = (*IDENTITY_FIELDS, "requirements", "credentials")


class Connector(ABC):
    """One system's lifecycle. Everything provision creates is named after db, so delete can find it."""

    backend: int  # a microbench task2_pb2.Backend value
    needs_psql: bool
    version_sql: str
    # A hosted connector also sets region and service, and overrides preflight.

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

    @abstractmethod
    def exists(self, db: str) -> bool:
        """Whether anything named after db is still on the server or its disk."""

    @abstractmethod
    def version(self) -> str:
        """The running server's version, from version_sql."""

    def preflight(self) -> None:
        """A hosted connector checks account access before anything is created."""


def system_dir(name: str, board: Path = BOARD) -> Path:
    return board / "systems" / name


def parse_system(raw: str | bytes, source: Path) -> dict:
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{source}: invalid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{source}: metadata must be a JSON object")
    if set(data) != set(SYSTEM_FIELDS):
        raise ValueError(f"{source}: metadata fields must be {list(SYSTEM_FIELDS)}")
    for field in ("system", "requirements"):
        value = data[field]
        if not isinstance(value, str) or not value.strip() or not value.isprintable():
            raise ValueError(f"{source}: {field} must be non-empty printable text")
    for field in ("proprietary", "hosted", "tuned"):
        if data[field] not in ("yes", "no"):
            raise ValueError(f"{source}: {field} must be 'yes' or 'no'")
    tags = data["tags"]
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag.strip() or not tag.isprintable() for tag in tags):
        raise ValueError(f"{source}: tags must be a non-empty list of printable text")
    credentials = data["credentials"]
    if (not isinstance(credentials, list)
            or any(not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name) for name in credentials)
            or len(credentials) != len(set(credentials))):
        raise ValueError(f"{source}: credentials must be unique environment variable names")
    if data["hosted"] == "no" and credentials:
        raise ValueError(f"{source}: a self-hosted system cannot require service credentials")
    return data


def load_system(name: str, board: Path = BOARD) -> dict:
    if not SYSTEM_KEY_RE.fullmatch(name):
        raise ValueError(f"invalid system key {name!r}")
    path = system_dir(name, board) / "system.json"
    try:
        return parse_system(path.read_bytes(), path)
    except OSError as error:
        raise ValueError(f"{path}: cannot read metadata: {error}") from None


def system_catalog(board: Path = BOARD, *, runnable: bool = False) -> dict[str, dict]:
    paths = sorted((board / "systems").glob("*/system.json"))
    if runnable:
        paths = [path for path in paths if (path.parent / "connector.py").is_file()]
    return {path.parent.name: load_system(path.parent.name, board) for path in paths}


def system_identity(meta: dict) -> dict:
    return {field: meta[field] for field in IDENTITY_FIELDS}


def connector_names(board: Path = BOARD) -> list[str]:
    return list(system_catalog(board, runnable=True))


def load_connector(name: str) -> Connector:
    if name not in connector_names():
        raise ValueError(f"no runnable system named {name!r}")
    return importlib.import_module(f"leaderboard.systems.{name}.connector").CONNECTOR
