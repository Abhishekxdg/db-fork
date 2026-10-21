from __future__ import annotations

from contextlib import closing
import importlib
from importlib import metadata
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import chronos, file_copy
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


def _ensure_chronos_loaded() -> type:
    if not getattr(chronos, "CHRONOS_AVAILABLE", False):
        importlib.reload(chronos)
    if not getattr(chronos, "CHRONOS_AVAILABLE", False):
        raise RuntimeError(
            "Chronos library (chronos-core) is not installed; run leaderboard/chronos/install or 'uv sync --frozen --extra chronos'"
        )
    return chronos.ChronosToolSuite


class Chronos(Connector):
    """Chronos bolt-on interval branching over PostgreSQL: backend chronos."""

    backend = tp.Backend.CHRONOS
    needs_psql = True
    version_sql = "SELECT current_setting('server_version')"
    data_dir = Path(os.environ.get("CHRONOS_DATA_DIR") or file_copy.PGSQL_DATA_DIR or "~/pg_chronos/data").expanduser()

    def scope(self) -> dict:
        return {"host": chronos.PGSQL_HOST, "port": chronos.PGSQL_PORT,
                "user": chronos.PGSQL_USER, "data_dir": str(self.data_dir.resolve())}

    def _admin(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(chronos.ChronosToolSuite.get_default_connection_uri())
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def provision(self, db: str) -> BackendInfo:
        suite_cls = _ensure_chronos_loaded()
        info = suite_cls.ChronosInfo(db)
        return BackendInfo(
            default_uri=suite_cls.get_default_connection_uri(),
            default_branch_name=chronos.MAIN_BRANCH,
            root_branch_name=chronos.MAIN_BRANCH,
            chronos_info=info,
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        suite_cls = _ensure_chronos_loaded()
        uri = suite_cls.get_initial_connection_uri(db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        chronos.ChronosToolSuite.cleanup(type("_Info", (), {"db_name": db})())
        if self.exists(db):
            raise RuntimeError(f"Chronos database '{db}' still exists after deletion")

    def exists(self, db: str) -> bool:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db,))
            return cur.fetchone() is not None

    def version(self) -> str:
        _ensure_chronos_loaded()
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            pg_ver = str(cur.fetchone()[0])
        for pkg in ("chronos-core", "chronos-db", "chronos"):
            try:
                return f"{metadata.version(pkg)} (pg {pg_ver})"
            except metadata.PackageNotFoundError:
                continue
        return pg_ver


CONNECTOR = Chronos()
