from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib import xata
from dblib.xata import XataToolSuite
from leaderboard.connector import Connector
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo
from util.import_db import load_sql_file


class Xata(Connector):
    """Xata branching Postgres: backend xata."""

    backend = tp.Backend.XATA
    needs_psql = True
    version_sql = "SELECT current_setting('server_version')"
    keys = ("XATA_API_KEY", "XATA_ORGANIZATION_ID")
    region = "us-east-1"
    service = {
        "region": "us-east-1",
        "instance_type": "xata.medium",
        "image": "postgres:18.0",
        "replicas": 0,
        "scale_to_zero_minutes": 30,
    }

    def _sync_env(self) -> None:
        xata.API_KEY = os.environ.get("XATA_API_KEY", xata.API_KEY)
        xata.XATA_ORGANIZATION_ID = os.environ.get("XATA_ORGANIZATION_ID", xata.XATA_ORGANIZATION_ID)
        xata.XATA_API_BASE_URL = f"https://api.xata.tech/organizations/{xata.XATA_ORGANIZATION_ID}/"

    def scope(self) -> dict:
        org = os.environ.get("XATA_ORGANIZATION_ID", xata.XATA_ORGANIZATION_ID or "")
        key = os.environ.get("XATA_API_KEY", xata.API_KEY or "")
        account = hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""
        return {"provider": "xata", "organization_id": org, "account": account, "region": self.region}

    def preflight(self) -> None:
        self._sync_env()
        XataToolSuite._request("GET", "projects")

    def provision(self, db: str) -> BackendInfo:
        self._sync_env()
        project_id, branch_id, branch_name, default_uri = XataToolSuite.create_xata_project(f"project_{db}")
        conn = psycopg2.connect(default_uri)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with closing(conn), conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db}"')
            cur.execute(f'CREATE DATABASE "{db}"')
        return BackendInfo(
            default_uri=default_uri,
            default_branch_id=branch_id,
            default_branch_name=branch_name,
            root_branch_name=branch_name,
            xata_project_id=project_id,
        )

    def load(self, info: BackendInfo, db: str, dump: Path) -> None:
        self._sync_env()
        uri = XataToolSuite._get_xata_connection_uri(info.xata_project_id, info.default_branch_id, db)
        load_sql_file(uri, dump)

    def delete(self, db: str) -> None:
        self._sync_env()
        target = f"project_{db}"
        resp = XataToolSuite._request("GET", "projects")
        for proj in resp.get("projects", []):
            if proj.get("name") == target:
                XataToolSuite._request("DELETE", f"projects/{proj['id']}")
        if self.exists(db):
            raise RuntimeError(f"Xata project '{target}' still exists after deletion")

    def exists(self, db: str) -> bool:
        self._sync_env()
        target = f"project_{db}"
        resp = XataToolSuite._request("GET", "projects")
        return any(proj.get("name") == target for proj in resp.get("projects", []))

    def version(self) -> str:
        return "18.0"


CONNECTOR = Xata()
