from __future__ import annotations

from contextlib import closing
from importlib import metadata
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import chronos, file_copy
from dblib.chronos import ChronosToolSuite
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


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
        conn = psycopg2.connect(ChronosToolSuite.get_default_connection_uri())
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def provision(self, db: str) -> BackendInfo:
        info = ChronosToolSuite.ChronosInfo(db)
        return BackendInfo(
            default_uri=ChronosToolSuite.get_default_connection_uri(),
            default_branch_name=chronos.MAIN_BRANCH,
            root_branch_name=chronos.MAIN_BRANCH,
            chronos_info=info,
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        uri = ChronosToolSuite.get_initial_connection_uri(db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        ChronosToolSuite.cleanup(type("_Info", (), {"db_name": db})())
        if self.exists(db):
            raise RuntimeError(f"Chronos database '{db}' still exists after deletion")

    def exists(self, db: str) -> bool:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db,))
            return cur.fetchone() is not None

    def version(self) -> str:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            pg_ver = str(cur.fetchone()[0])
        for pkg in ("chronos-db", "chronos-core", "chronos"):
            try:
                return f"{metadata.version(pkg)} (pg {pg_ver})"
            except metadata.PackageNotFoundError:
                continue
        return pg_ver


CONNECTOR = Chronos()
