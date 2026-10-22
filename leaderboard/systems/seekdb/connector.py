from __future__ import annotations

from contextlib import closing
from pathlib import Path

from dblib import seekdb
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo


class SeekDB(Connector):
    """SeekDB (OceanBase) over the MySQL protocol: backend seekdb."""

    backend = tp.Backend.SEEKDB
    needs_psql = False
    version_sql = "SELECT version()"
    data_dir = Path(seekdb.SEEKDB_DATA_DIR).expanduser()

    def scope(self) -> dict:
        return {"host": seekdb.SEEKDB_HOST, "port": seekdb.SEEKDB_PORT,
                "user": seekdb.SEEKDB_USER, "data_dir": str(self.data_dir.resolve())}

    def provision(self, db: str) -> BackendInfo:
        with closing(seekdb.connect()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {seekdb._quote(db)}")
        return BackendInfo(default_branch_name=seekdb.MAIN_BRANCH, default_branch_id=db)

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        seekdb.load_sql_dump(db, str(dump))

    def delete(self, db: str) -> None:
        seekdb.drop_database(db)
        if self.exists(db):
            raise RuntimeError(f"SeekDB database '{db}' still exists after deletion")

    def exists(self, db: str) -> bool:
        with closing(seekdb.connect()) as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            return cur.fetchone() is not None or bool(seekdb._branch_databases(cur, db))

    def version(self) -> str:
        with closing(seekdb.connect()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            return str(cur.fetchone()[0])


CONNECTOR = SeekDB()
