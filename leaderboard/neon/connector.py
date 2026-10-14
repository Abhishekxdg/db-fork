from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import neon
from dblib.neon import NeonToolSuite
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


class Neon(Connector):
    """Neon serverless Postgres: backend neon."""

    backend = tp.Backend.NEON
    needs_psql = True
    version_sql = "SELECT current_setting('server_version')"
    keys = ("NEON_API_KEY_ORG",)
    region = neon.NEON_REGION
    service = {
        "region": neon.NEON_REGION,
        "pg_version": int(neon.NEON_PG_VERSION),
        "endpoint_type": "read_write",
        "cpu_millis": int(round(neon.NEON_COMPUTE_CU * 1000)),
    }

    def _sync_env(self) -> None:
        neon.API_KEY = os.environ.get("NEON_API_KEY_ORG", neon.API_KEY)

    def scope(self) -> dict:
        return {"provider": "neon", "region": self.region, "pg_version": int(neon.NEON_PG_VERSION)}

    def preflight(self) -> None:
        self._sync_env()
        neon.api_request("GET", "projects?limit=1")

    def provision(self, db: str) -> BackendInfo:
        self._sync_env()
        project = NeonToolSuite.create_neon_project(f"project_{db}")
        project_id = project["project"]["id"]
        default_uri = project["connection_uris"][0]["connection_uri"] if project.get("connection_uris") else ""
        branch_id = project["branch"]["id"]
        branch_name = project["branch"]["name"]
        conn = psycopg2.connect(default_uri)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with closing(conn), conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {neon._q(db)}")
            cur.execute(f"CREATE DATABASE {neon._q(db)}")
        return BackendInfo(
            default_uri=default_uri,
            default_branch_id=branch_id,
            default_branch_name=branch_name,
            root_branch_name=branch_name,
            neon_project_id=project_id,
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        self._sync_env()
        uri = NeonToolSuite._get_neon_connection_uri(info.neon_project_id, info.default_branch_id, db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        self._sync_env()
        target = f"project_{db}"
        for proj in neon.list_projects():
            if proj.get("name") == target:
                NeonToolSuite.delete_project(proj["id"])

    def exists(self, db: str) -> bool:
        self._sync_env()
        target = f"project_{db}"
        return any(proj.get("name") == target for proj in neon.list_projects())

    def version(self) -> str:
        return str(neon.NEON_PG_VERSION)


CONNECTOR = Neon()
