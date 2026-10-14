"""Plain PostgreSQL backend where a branch is a database copy.

    branch   CREATE DATABASE <name> TEMPLATE <parent> STRATEGY = FILE_COPY
    connect  open a connection to the branch's database
    delete   DROP DATABASE <name>

Postgres has no commits and no cross-database queries, so everything else
is unsupported. CREATE DATABASE ... TEMPLATE needs no open connections on
the template, so branching from the connected database first moves the
connection to the neutral "postgres" database.
"""

import os
import threading
import weakref

from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import psycopg2
from psycopg2.extensions import connection as _pgconn
from dblib.db_api import DBToolSuite, Ref
import dblib.result_collector as rc
import dblib.util as dbutil

PGSQL_USER = os.environ.get("PGSQL_USER", os.environ.get("USER", "postgres"))
PGSQL_PASSWORD = os.environ.get("PGSQL_PASSWORD", "")
PGSQL_HOST = os.environ.get("PGSQL_HOST", "localhost")
PGSQL_PORT = int(os.environ.get("PGSQL_PORT", "5433"))
PGSQL_DATA_DIR = os.environ.get("PGSQL_DATA_DIR", "")

MAIN_BRANCH = "main"
_BRANCH_SEP = "__"


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def branch_db_name(db_name: str, branch_name: str) -> str:
    """Database that holds branch_name of the root database db_name."""
    if not branch_name or branch_name in (MAIN_BRANCH, db_name):
        return db_name
    if branch_name.startswith(db_name + _BRANCH_SEP):
        return branch_name
    return f"{db_name}{_BRANCH_SEP}{branch_name}"


def branch_databases(cur, db_name: str) -> list[str]:
    """All branch databases forked from db_name."""
    prefix = db_name + _BRANCH_SEP
    cur.execute(
        "SELECT datname FROM pg_catalog.pg_database WHERE datname LIKE %s",
        (prefix + "%",),
    )
    return [name for (name,) in cur.fetchall() if name.startswith(prefix)]


def drop_database(db_name: str) -> None:
    """Drop db_name and every branch database created from it."""
    conn = psycopg2.connect(FileCopyToolSuite.get_default_connection_uri())
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            for branch_db in branch_databases(cur, db_name):
                cur.execute(f"DROP DATABASE IF EXISTS {_quote(branch_db)} WITH (FORCE);")
            cur.execute(f"DROP DATABASE IF EXISTS {_quote(db_name)} WITH (FORCE);")
        print(f"Database '{db_name}' deleted successfully.")
    finally:
        conn.close()


class FileCopyToolSuite(DBToolSuite):
    BACKEND_NAME = "file_copy"
    SUPPORTS_COMMIT_REFS = False
    SUPPORTS_MULTI_REF_EXEC = False
    _SUITES: "weakref.WeakSet[FileCopyToolSuite]" = weakref.WeakSet()
    _SUITES_LOCK = threading.Lock()

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return dbutil.format_db_uri(PGSQL_USER, PGSQL_PASSWORD, PGSQL_HOST, PGSQL_PORT, "postgres")

    @classmethod
    def get_branch_uri(cls, branch_name) -> str:
        return dbutil.format_db_uri(PGSQL_USER, PGSQL_PASSWORD, PGSQL_HOST, PGSQL_PORT, branch_name)

    @classmethod
    def get_initial_connection_uri(cls, db_name: str) -> str:
        return cls.get_branch_uri(db_name)

    @classmethod
    def init_for_bench(
        cls,
        collector: rc.ResultCollector,
        db_name: str,
        default_branch_name: str,
        shared_branches: set,
        shared_branches_lock: threading.Lock,
        create_db_lock: threading.Lock,
        measure_storage: bool = False,
    ):
        target_branch = default_branch_name or db_name
        conn = psycopg2.connect(cls.get_branch_uri(branch_db_name(db_name, target_branch)))
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return cls(
            connection=conn,
            collector=collector,
            db_name=db_name,
            default_branch_name=target_branch,
            shared_branches=shared_branches,
            shared_branches_lock=shared_branches_lock,
            create_db_lock=create_db_lock,
            measure_storage=measure_storage,
        )

    def __init__(
        self,
        connection: _pgconn,
        collector: rc.ResultCollector,
        db_name: str,
        default_branch_name: str,
        shared_branches: set,
        shared_branches_lock: threading.Lock,
        create_db_lock: threading.Lock,
        measure_storage: bool = False,
    ):
        super().__init__(connection, collector, measure_storage)
        self.db_name = db_name
        self.default_branch = default_branch_name
        # Branch databases known across all workers (for storage accounting
        # and cleanup), and a lock serialising CREATE DATABASE.
        self.shared_branches = shared_branches
        self._shared_branches_lock = shared_branches_lock
        self._create_db_lock = create_db_lock
        self._on_neutral = False
        with self._shared_branches_lock:
            shared_branches.add(default_branch_name or db_name)
        self._current_ref = Ref(default_branch_name)
        with self._SUITES_LOCK:
            self._SUITES.add(self)

    def _db_for_branch(self, branch_name: str) -> str:
        if branch_name == self.default_branch:
            return self.db_name
        return branch_db_name(self.db_name, branch_name)

    def _connect_to(self, uri: str) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = psycopg2.connect(uri)
        self.conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        self._on_neutral = False

    def _go_neutral(self, clear_ref: bool = False) -> None:
        """Move the connection off any branch database."""
        self._connect_to(self.get_default_connection_uri())
        self._on_neutral = True
        if clear_ref:
            self._current_ref = None

    @classmethod
    def _detach_suites_on(cls, target_db: str, clear_ref: bool = False) -> None:
        with cls._SUITES_LOCK:
            suites = list(cls._SUITES)
        for s in suites:
            try:
                if (
                    s._current_ref is not None
                    and s._db_for_branch(s._current_ref.branch) == target_db
                    and (clear_ref or not getattr(s, "_on_neutral", False))
                ):
                    s._go_neutral(clear_ref=clear_ref)
            except Exception:
                pass

    def _connect(self, ref: Ref, exec_id: int, timed: bool, label: str, fallback: bool):
        if getattr(self, "_on_neutral", False) and self._current_ref == ref:
            self._connect_impl(ref)
            return None
        return super()._connect(ref, exec_id, timed, label, fallback)

    def list_branches(self) -> list:
        with self._shared_branches_lock:
            return list(self.shared_branches)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _storage_bytes(self) -> int:
        """Physical storage of all branch databases.

        With PGSQL_DATA_DIR set (an isolated volume, see
        db_setup/setup_pg_volume.sh) this is the volume's usage, which is
        right under copy-on-write. Otherwise it sums st_blocks over the
        per-database directories, which is accurate without CoW.
        """
        if PGSQL_DATA_DIR:
            return dbutil.get_volume_usage_bytes(PGSQL_DATA_DIR)
        with self._shared_branches_lock:
            branch_names = [self._db_for_branch(b) for b in self.shared_branches]
        if not branch_names or not self.conn:
            return 0
        with self.conn.cursor() as cur:
            cur.execute("SHOW data_directory;")
            pg_data_dir = cur.fetchone()[0]
            cur.execute("SELECT oid FROM pg_database WHERE datname = ANY(%s);", (branch_names,))
            oids = [str(row[0]) for row in cur.fetchall()]
        base_dir = os.path.join(pg_data_dir, "base")
        return sum(dbutil.get_directory_size_bytes(os.path.join(base_dir, oid)) for oid in oids)

    def _connect_impl(self, ref: Ref) -> None:
        self._connect_to(self.get_branch_uri(self._db_for_branch(ref.branch)))

    def _branch_impl(self, name: str, from_ref: Ref) -> None:
        parent_db = self._db_for_branch(from_ref.branch)
        target_db = self._db_for_branch(name)
        temp_conn = None
        try:
            with self._create_db_lock:
                self._detach_suites_on(parent_db, clear_ref=False)
                temp_conn = psycopg2.connect(self.get_default_connection_uri())
                temp_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                with temp_conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (parent_db,),
                    )
                    cur.execute(
                        f"CREATE DATABASE {_quote(target_db)} TEMPLATE {_quote(parent_db)} STRATEGY = FILE_COPY"
                    )
        except psycopg2.errors.DuplicateDatabase as e:
            raise Exception(f"Cannot create branch {name}, already exists: {e}")
        finally:
            if temp_conn:
                temp_conn.close()
        with self._shared_branches_lock:
            self.shared_branches.add(name)

    def _delete_impl(self, ref: Ref) -> None:
        target_db = self._db_for_branch(ref.branch)
        with self._create_db_lock:
            self._detach_suites_on(target_db, clear_ref=True)
            temp_conn = psycopg2.connect(self.get_default_connection_uri())
            temp_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            try:
                with temp_conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE {_quote(target_db)} WITH (FORCE);")
            finally:
                temp_conn.close()
        with self._shared_branches_lock:
            self.shared_branches.discard(ref.branch)

    # ------------------------------------------------------------------
    # Async: a psycopg pool on the branch the suite is on when opened; a
    # script on another branch gets its own connection.
    # ------------------------------------------------------------------

    async def open_async_pool(self, size: int) -> None:
        from psycopg_pool import AsyncConnectionPool

        if self._current_ref is None:
            raise ValueError("Not connected to a branch")
        self._pool_branch = self._current_ref.branch
        self.async_pool = AsyncConnectionPool(
            self.get_branch_uri(self._db_for_branch(self._pool_branch)),
            min_size=size, max_size=size, kwargs={"autocommit": True}, open=False,
        )
        await self.async_pool.open(wait=True)

    async def _connect_impl_async(self, conn, ref: Ref):
        if ref.branch == self._pool_branch:
            return conn
        import psycopg

        return await psycopg.AsyncConnection.connect(
            self.get_branch_uri(self._db_for_branch(ref.branch)), autocommit=True
        )

    # ------------------------------------------------------------------
    # Cleanup helpers used by the runners
    # ------------------------------------------------------------------

    @classmethod
    def cleanup(cls, info):
        try:
            info.change_file_copy_method(info.prev_method)
            drop_database(info.db_name)
        except Exception as e:
            print(f"Error deleting database: {e}")

    class FileCopyInfo:
        """Branch names and locks shared by every worker's suite."""

        def __init__(self, db_name: str):
            self.branches = set()
            self.db_name = db_name
            self.prev_method = ""
            self.branches_lock = threading.Lock()
            self.create_db_lock = threading.Lock()
            self.change_file_copy_method("clone")

        def change_file_copy_method(self, method: str) -> None:
            """Set file_copy_method server-wide, remembering the old value."""
            conn = None
            cur = None
            try:
                conn = psycopg2.connect(FileCopyToolSuite.get_default_connection_uri())
                conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                cur = conn.cursor()
                cur.execute("SHOW file_copy_method;")
                self.prev_method = cur.fetchone()[0]
                cur.execute(f"ALTER SYSTEM SET file_copy_method = '{method}';")
                cur.execute("SELECT pg_reload_conf();")
                print(f"Changed file copy method to {method}")
            except Exception as e:
                print(f"Error changing file copy method: {e}")
            finally:
                if cur:
                    cur.close()
                if conn:
                    conn.close()
