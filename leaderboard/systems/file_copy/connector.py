from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import file_copy
from dblib.file_copy import FileCopyToolSuite
from leaderboard import run as micro
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class FileCopy(Connector):
    """PostgreSQL CREATE DATABASE ... STRATEGY = FILE_COPY: backend file_copy."""

    backend = tp.Backend.FILE_COPY
    needs_psql = True
    version_sql = "SELECT current_setting('server_version')"
    data_dir = Path(file_copy.PGSQL_DATA_DIR or "~/pg_file_copy/data").expanduser()
    prev_method_file: Path | None = None

    def _prev_method_path(self) -> Path:
        if self.prev_method_file is not None:
            return self.prev_method_file
        return micro.RUNS / "file_copy.prev_method"

    def scope(self) -> dict:
        return {"host": file_copy.PGSQL_HOST, "port": file_copy.PGSQL_PORT,
                "user": file_copy.PGSQL_USER, "data_dir": str(self.data_dir.resolve())}

    def _admin(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(FileCopyToolSuite.get_default_connection_uri())
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def _record_prev_method(self, method: str | None) -> None:
        path = self._prev_method_path()
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if not existing:
                raise RuntimeError(f"persisted file_copy_method file {path} is empty")
            self._prev_method = existing
            return
        if not method or not str(method).strip():
            raise RuntimeError("cannot persist empty file_copy_method")
        clean = str(method).strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".prev_method.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{clean}\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        self._prev_method = clean

    def _read_prev_method(self) -> str | None:
        path = self._prev_method_path()
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if not existing:
                raise RuntimeError(f"persisted file_copy_method file {path} is empty")
            self._prev_method = existing
            return existing
        prev = getattr(self, "_prev_method", None)
        return prev if prev else None

    def capture_prev_method(self) -> None:
        """Persist the server's pre-benchmark file_copy_method if no recovery file exists yet."""
        path = self._prev_method_path()
        if path.exists():
            self._read_prev_method()
            return
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute("SHOW file_copy_method;")
            row = cur.fetchone()
            if not row or not isinstance(row[0], str) or not row[0].strip():
                raise RuntimeError("SHOW file_copy_method returned no value")
            self._record_prev_method(row[0])

    def provision(self, db: str) -> BackendInfo:
        self.capture_prev_method()
        info = FileCopyToolSuite.FileCopyInfo(db)
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {_quote(db)}")
        return BackendInfo(
            default_uri=FileCopyToolSuite.get_default_connection_uri(),
            default_branch_name=db,
            root_branch_name=db,
            file_copy_info=info,
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        uri = FileCopyToolSuite.get_initial_connection_uri(db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        prev = self._read_prev_method()
        restored = False
        try:
            file_copy.drop_database(db)
        finally:
            if prev:
                with closing(self._admin()) as conn, conn.cursor() as cur:
                    cur.execute("ALTER SYSTEM SET file_copy_method = %s;", (prev,))
                    cur.execute("SELECT pg_reload_conf();")
                    restored = True
        if self.exists(db):
            raise RuntimeError(f"FileCopy database '{db}' still exists after deletion")
        if restored:
            self._prev_method_path().unlink(missing_ok=True)
            self._prev_method = None

    def exists(self, db: str) -> bool:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s OR datname LIKE %s",
                (db, db.replace("_", r"\_") + r"\_\_%"),
            )
            return cur.fetchone() is not None

    def version(self) -> str:
        with closing(self._admin()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            return str(cur.fetchone()[0])


CONNECTOR = FileCopy()
