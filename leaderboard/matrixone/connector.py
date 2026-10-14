from __future__ import annotations

from contextlib import closing
from pathlib import Path

from dblib import matrixone
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo


class MatrixOne(Connector):
    """MatrixOne over the MySQL protocol: backend matrixone."""

    backend = tp.Backend.MATRIXONE
    needs_psql = False
    version_sql = "SELECT version()"
    data_dir = Path(matrixone.MO_DATA_DIR).expanduser()

    def scope(self) -> dict:
        return {"host": matrixone.MO_HOST, "port": matrixone.MO_PORT,
                "user": matrixone.MO_USER, "data_dir": str(self.data_dir.resolve())}

    def provision(self, db: str) -> BackendInfo:
        with closing(matrixone.connect()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {matrixone._quote(db)}")
        return BackendInfo(default_branch_name=matrixone.MAIN_BRANCH, default_branch_id=db)

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        matrixone.load_sql_dump(db, str(dump))

    def delete(self, db: str) -> None:
        matrixone.drop_database(db)
        if self.exists(db):
            raise RuntimeError(f"MatrixOne database or snapshot for '{db}' still exists after deletion")

    def exists(self, db: str) -> bool:
        with closing(matrixone.connect()) as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            if cur.fetchone() is not None or matrixone._branch_databases(cur, db):
                return True
            prefix = matrixone.snapshot_prefix(db)
            cur.execute("SHOW SNAPSHOTS")
            return any(str(r[0]).startswith(prefix) for r in cur.fetchall())

    def version(self) -> str:
        with closing(matrixone.connect()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            return str(cur.fetchone()[0])


CONNECTOR = MatrixOne()
