from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import dolt as dolt_pg
from dblib.dolt import DOLT_DATA_DIR, DoltToolSuite, commit_dolt_schema
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


class Doltgres(Connector):
    """Doltgres over the Postgres protocol: backend dolt."""

    backend = tp.Backend.DOLT
    needs_psql = True
    version_sql = "SELECT dolt_version()"
    data_dir = Path(DOLT_DATA_DIR).expanduser()

    def scope(self) -> dict:
        return {"host": dolt_pg.DOLT_HOST, "port": dolt_pg.DOLT_PORT, "user": dolt_pg.DOLT_USER,
                "data_dir": str(self.data_dir.resolve())}

    def _admin(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(DoltToolSuite.get_default_connection_uri())
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def provision(self, db: str) -> BackendInfo:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {db}")
        return BackendInfo(default_branch_name="main")

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        uri = DoltToolSuite.get_initial_connection_uri(db)
        load_sql_file(uri, dump)
        commit_dolt_schema(uri)

    def delete(self, db: str) -> None:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
        shutil.rmtree(self.data_dir / ".dolt_dropped_databases" / db, ignore_errors=True)


CONNECTOR = Doltgres()
