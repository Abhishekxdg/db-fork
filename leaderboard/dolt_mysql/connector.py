from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

from dblib import dolt_mysql
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo


class Dolt(Connector):
    """Dolt over the MySQL protocol: backend dolt_mysql."""

    backend = tp.Backend.DOLT_MYSQL
    needs_psql = False
    version_sql = "SELECT dolt_version()"
    data_dir = Path(dolt_mysql.DOLT_MYSQL_DATA_DIR)

    def scope(self) -> dict:
        return {"host": dolt_mysql.DOLT_MYSQL_HOST, "port": dolt_mysql.DOLT_MYSQL_PORT,
                "user": dolt_mysql.DOLT_MYSQL_USER, "data_dir": str(self.data_dir.resolve())}

    def provision(self, db: str) -> BackendInfo:
        with closing(dolt_mysql.connect()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {db}")
        return BackendInfo(default_branch_name="main")

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        dolt_mysql.load_sql_dump(db, str(dump))
        with closing(dolt_mysql.connect(db)) as conn, conn.cursor() as cur:
            cur.execute("CALL DOLT_COMMIT('-Am', 'Load SQL schema')")

    def delete(self, db: str) -> None:
        dolt_mysql.drop_database(db)
        shutil.rmtree(self.data_dir / ".dolt_dropped_databases" / db, ignore_errors=True)

    def exists(self, db: str) -> bool:
        with closing(dolt_mysql.connect()) as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            listed = cur.fetchone() is not None
        return listed or (self.data_dir / db).exists()

    def version(self) -> str:
        with closing(dolt_mysql.connect()) as conn, conn.cursor() as cur:
            cur.execute(self.version_sql)
            return str(cur.fetchone()[0])


CONNECTOR = Dolt()
