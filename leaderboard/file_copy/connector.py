from __future__ import annotations

from contextlib import closing
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import file_copy
from dblib.file_copy import FileCopyToolSuite
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


class FileCopy(Connector):
    """PostgreSQL CREATE DATABASE ... STRATEGY = FILE_COPY: backend file_copy."""

    backend = tp.Backend.FILE_COPY
    needs_psql = True
    version_sql = "SELECT current_setting('server_version')"
    data_dir = Path(file_copy.PGSQL_DATA_DIR or "~/pg_file_copy/data").expanduser()

    def scope(self) -> dict:
        return {"host": file_copy.PGSQL_HOST, "port": file_copy.PGSQL_PORT,
                "user": file_copy.PGSQL_USER, "data_dir": str(self.data_dir.resolve())}

    def _admin(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(FileCopyToolSuite.get_default_connection_uri())
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def provision(self, db: str) -> BackendInfo:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {file_copy._quote(db)}")
        return BackendInfo(
            default_uri=FileCopyToolSuite.get_default_connection_uri(),
            default_branch_name=db,
            root_branch_name=db,
            file_copy_info=FileCopyToolSuite.FileCopyInfo(db),
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        uri = FileCopyToolSuite.get_initial_connection_uri(db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        file_copy.drop_database(db)

    def exists(self, db: str) -> bool:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db,))
            return cur.fetchone() is not None or bool(file_copy.branch_databases(cur, db))

    def version(self) -> str:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            return str(cur.fetchone()[0])


CONNECTOR = FileCopy()
