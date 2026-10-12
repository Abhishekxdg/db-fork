"""Chronos backend (https://github.com/mitdbg/chronos, arXiv:2609.14889).

Chronos is a bolt-on branching layer: a Python library (``chronos-core``)
over an ordinary PostgreSQL database. Branches live in metadata tables and
every row version carries a visibility interval, so a fork copies nothing.
There is no wire protocol of its own and no SQL functions for branching:
SQL goes through a ``BranchSession`` checked out on one branch, which
rewrites it for that branch, and the verbs are methods of a
``ChronosBranchContext``.

Each suite owns one context (Chronos wants one per concurrent worker) and
hands the harness a small DB-API facade (``self.conn``) over the session of
the ref it is connected to:

    connect    ctx.checkout(branch); a commit ref is a read-only checkpoint
               session (ctx.checkout_checkpoint)
    branch     ctx.create_branch; from a commit ref
               ctx.create_branch_from_checkpoint
    commit     ctx.create_checkpoint (the checkpoint id is the commit id; the
               message is its metadata)
    log        ctx.list_checkpoints on the branch, then on the branches it
               was forked from or merged (lineage kept in branch and
               checkpoint metadata, since checkpoints belong to one branch)
    diff       ctx.diff (row-level; a commit ref is materialised as a
               throwaway branch from the checkpoint)
    merge      ctx.merge_apply: first with the snapshot-isolation policy (the
               native one-pass merge); on a write-write conflict again with
               target_wins ("ours") or source_wins ("theirs"), the conflicts
               counted from ctx.merge_preview. A checkpoint records the merge
    rebase     the same merge with the upstream as the source and the branch
               as the target
    reset      ctx.diff between the branch and the commit, applied to the
               branch with the session's upsert_rows/delete_keys
    revert     ctx.diff between the commit and its predecessor, applied the
               same way, then a checkpoint
    delete     ctx.delete_branch, which removes the branch together with
               every branch forked from it

What the facade translates, because Chronos accepts a subset of SQL:

* ``%s`` parameters become the named parameters Chronos takes;
* ``CREATE TABLE`` is rewritten to the form its DDL parser accepts (primary
  key inline on the columns, no NOT NULL). Chronos matches rows by primary
  key, so a table created without one (CH's ``history``) gets a hidden
  ``_bb_rowid`` key that the facade fills on INSERT (and ``SELECT *`` shows);
* a SELECT whose unnamed expressions would share a column name gets
  aliases, because rows come back as dicts keyed by column name;
* ``BETWEEN`` in the WHERE of an UPDATE or DELETE becomes two comparisons
  (the write path of Chronos 0.2 does not take it);
* timestamps, dates and numeric expressions come back as text and are
  parsed to the types the PostgreSQL drivers return;
* BEGIN/COMMIT/ROLLBACK drive ``session.transaction()``; CHECKPOINT, VACUUM
  and ANALYZE go straight to PostgreSQL.

Everything else is passed as is, and a statement Chronos rejects (a view, a
foreign key, INSERT ... SELECT, ...) is recorded as FAILED with its reason.

Schema branching and merge exclude each other in Chronos 0.2: with
``CHRONOS_SCHEMA_BRANCHING=1`` (the default here) branches can create, alter
and drop tables, but merge (and so rebase) is unsupported; with ``=0`` merge
and rebase work, and DDL is limited to CREATE TABLE on the default branch.
"""

import datetime
import os
import re
import threading
import time
import uuid
from decimal import Decimal

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib.db_api import DBToolSuite, Ref, UnsupportedOperation
import dblib.result_collector as rc
import dblib.util as dbutil

CHRONOS_USER = os.environ.get("CHRONOS_USER", "postgres")
CHRONOS_PASSWORD = os.environ.get("CHRONOS_PASSWORD", "password")
CHRONOS_HOST = os.environ.get("CHRONOS_HOST", "localhost")
CHRONOS_PORT = int(os.environ.get("CHRONOS_PORT", "5432"))

# Branch-local DDL (on) or merge/rebase (off); see the module docstring.
CHRONOS_SCHEMA_BRANCHING = os.environ.get(
    "CHRONOS_SCHEMA_BRANCHING", "1").lower() not in ("0", "false", "no", "off")

# Chronos' calls cannot be cancelled from the client, so the harness' stall
# watchdog cannot stop one that waits on a lock (a fork of a branch that
# transactions keep busy can wait indefinitely). The server gives up the wait
# instead, after as long as the watchdog's default limit; 0 = wait forever.
CHRONOS_LOCK_TIMEOUT_SEC = int(os.environ.get("CHRONOS_LOCK_TIMEOUT_SEC", "600"))

# Branch every database starts with; Chronos does not let it be deleted.
MAIN_BRANCH = "main"

# Prefix of the throwaway branches that materialise a commit ref.
_TEMP_PREFIX = "_bbtmp_"

# Hidden key of the tables the workload creates without a primary key.
ROWID_COLUMN = "_bb_rowid"
# Those tables, per database: {db_name: {table}}. Process-wide, so every
# worker's suite sees a table another suite created.
_KEYLESS: dict = {}
# Where each branch came from, per database: {db_name: {branch: metadata}}.
# The same lineage is in the branch's Chronos metadata; this copy outlives
# the branch, whose checkpoints log() and revert() may still be asked about.
_LINEAGE: dict = {}
_INSERT_RE = re.compile(r"\s*INSERT\s+INTO\s+\"?(\w+)", re.I)

# Statements that are not branch data and go straight to PostgreSQL.
_ADMIN_HEADS = ("CHECKPOINT", "VACUUM", "ANALYZE")
_READ_HEADS = ("SELECT", "WITH", "VALUES", "SHOW", "EXPLAIN")

_PARAM_RE = re.compile(r"%%|%s|%\((\w+)\)s")
_CREATE_TABLE_RE = re.compile(r"\s*CREATE\s+TABLE\b", re.I)
_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\Z")

# Translated statements kept per suite (the workload's texts are few).
_CACHE_MAX = 4096

# A fork, checkpoint or merge puts a barrier on its branch while the other
# contexts' sessions on it drain; an operation that meets the barrier fails
# with this marker and is meant to be tried again.
_BUSY_MARKER = "chronos_session_barrier_in_progress"
_BUSY_WAIT_SEC = 60.0

# Closing a context (and waiting for its background work) waits for Chronos'
# interval GC thread; if that thread has died, the wait never returns
# (0.2.0a1). Such a call is left behind after this long.
_GC_WAIT_SEC = 30.0


def _chronos():
    """The chronos-core API, imported on first use so the other backends
    run without it installed."""
    try:
        from chronos_core import branching
    except ImportError as e:
        raise RuntimeError(
            "the chronos backend needs chronos-core "
            "(pip install 'git+https://github.com/mitdbg/chronos#subdirectory=packages/chronos-core')"
        ) from e
    return branching


def connect(db_name: str = "postgres"):
    """Open an autocommit psycopg2 connection to the PostgreSQL server
    Chronos runs on."""
    conn = psycopg2.connect(
        host=CHRONOS_HOST, port=CHRONOS_PORT, user=CHRONOS_USER,
        password=CHRONOS_PASSWORD, dbname=db_name,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def setup_database(db_name: str) -> None:
    """(Re)create db_name, empty. Chronos creates its metadata tables on
    the first connection."""
    drop_database(db_name, quiet=True)
    _KEYLESS.pop(db_name, None)
    _LINEAGE.pop(db_name, None)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}";')
            if CHRONOS_LOCK_TIMEOUT_SEC > 0:
                cur.execute(f'ALTER DATABASE "{db_name}" SET lock_timeout = %s;',
                            (f"{CHRONOS_LOCK_TIMEOUT_SEC}s",))
        print("Database created successfully.")
    finally:
        conn.close()


def drop_database(db_name: str, quiet: bool = False) -> None:
    """Drop db_name with its branches. FORCE, because every Chronos context
    holds several connections (session, native store, epoch heartbeat, GC)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);')
        if not quiet:
            print(f"Database '{db_name}' deleted successfully.")
    finally:
        conn.close()


def adopt_tables(db_name: str) -> list:
    """Register every table of db_name that has a primary key with Chronos
    (after a SQL dump was loaded with psql). Returns the tables without a
    primary key, which Chronos cannot branch."""
    conn = connect(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, "
                "ARRAY(SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = c.oid AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND c.relname NOT LIKE '\\_chronos\\_%' ORDER BY c.relname")
            tables = cur.fetchall()
    finally:
        conn.close()
    ctx = _chronos().ChronosBranchContext.connect(
        ChronosToolSuite.get_initial_connection_uri(db_name),
        enable_schema_branching=CHRONOS_SCHEMA_BRANCHING)
    skipped = []
    try:
        for table, pk in tables:
            if pk:
                ctx.register_table(table, list(pk))
            else:
                skipped.append(table)
    finally:
        ctx.close()
    if skipped:
        print(f"Warning: Chronos cannot branch tables without a primary key: {skipped}")
    return skipped


def _head(sql: str) -> str:
    """First keyword of a statement, upper-cased."""
    m = re.match(r"\s*(?:--[^\n]*\n\s*)*([A-Za-z]+)", sql)
    return m.group(1).upper() if m else ""


def _bind(query: str, vars):
    """DB-API (format/pyformat) parameters -> the named ones Chronos takes."""
    if vars is None:
        return query, None
    if isinstance(vars, dict):
        return _PARAM_RE.sub(
            lambda m: "%" if m.group(0) == "%%" else f":{m.group(1)}", query), dict(vars)
    count = iter(range(len(vars) + 1))

    def repl(m):
        if m.group(0) == "%%":
            return "%"
        return f":p{next(count)}"
    return _PARAM_RE.sub(repl, query), {f"p{i}": v for i, v in enumerate(vars)}


def _coerce(value, expression: bool):
    """Chronos returns timestamps, dates and the numeric results of
    expressions as text; give them the types psycopg2 would. ``expression``
    is False for a plain column, whose text is left alone unless it has the
    shape of a timestamp or a date."""
    if type(value) is not str:
        return value
    n = len(value)
    if n >= 10 and value[4] == "-" and value[7] == "-":
        try:
            if n == 10:
                return datetime.date.fromisoformat(value)
            if n >= 19 and value[10] == " " and value[13] == ":":
                return datetime.datetime.fromisoformat(value)
        except ValueError:
            return value
    if expression and _NUMERIC_RE.match(value):
        return Decimal(value)
    return value


class _SelectPlan:
    """A SELECT as it is sent, and which of its columns are expressions."""

    __slots__ = ("sql", "expressions")

    def __init__(self, sql: str, expressions=None):
        self.sql = sql
        self.expressions = expressions   # [bool] per column, or None if unknown


def _plan_select(sql: str) -> _SelectPlan:
    """Rows come back as dicts keyed by column name, so two unnamed
    expressions (MIN(a), MIN(b)) would collapse into one key: alias them.
    The statement is only regenerated when that is needed."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return _SelectPlan(sql)
    if not isinstance(tree, exp.Select):
        return _SelectPlan(sql)
    flags, unnamed, names = [], [], []
    for e in tree.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and e.is_star):
            return _SelectPlan(sql)
        plain = isinstance(e, exp.Column)
        flags.append(not plain)
        if plain or isinstance(e, exp.Alias):
            names.append(e.alias_or_name.lower())
        else:
            unnamed.append(e)
    if len(unnamed) < 2 and len(set(names)) == len(names):
        return _SelectPlan(sql, flags)
    try:
        for i, e in enumerate(list(tree.expressions)):
            if e in unnamed or names.count(e.alias_or_name.lower()) > 1:
                inner = e.this if isinstance(e, exp.Alias) else e
                e.replace(exp.alias_(inner.copy(), f"_c{i}", quoted=True))
        return _SelectPlan(_named(tree.sql(dialect="postgres")), flags)
    except Exception:
        return _SelectPlan(sql, flags)


class _TableDDL:
    """A CREATE TABLE as Chronos' DDL parser accepts it (``chronos``:
    PRIMARY KEY on the columns instead of a table constraint, no NOT NULL)
    and as PostgreSQL does (``postgres``), with the table and its key."""

    __slots__ = ("chronos", "postgres", "table", "pk", "keyless")

    def __init__(self, sql: str):
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="postgres")
        schema = tree.this
        if not isinstance(tree, exp.Create) or not isinstance(schema, exp.Schema):
            raise RuntimeError("unsupported CREATE TABLE form for Chronos")
        self.table = schema.this.name
        pk = []
        for e in schema.expressions:
            if isinstance(e, exp.PrimaryKey):
                pk = [c.name for c in e.expressions]
        for col in schema.find_all(exp.ColumnDef):
            if col.name not in pk and any(
                    isinstance(c.kind, exp.PrimaryKeyColumnConstraint)
                    for c in col.args.get("constraints") or []):
                pk.append(col.name)
        # Chronos matches rows by primary key; a table without one gets a
        # hidden key that the facade fills on INSERT.
        self.keyless = not pk
        if self.keyless:
            pk = [ROWID_COLUMN]
            schema.append("expressions", exp.ColumnDef(
                this=exp.to_identifier(ROWID_COLUMN), kind=exp.DataType.build("BIGINT"),
                constraints=[exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint())]))
        self.pk = pk
        self.postgres = tree.sql(dialect="postgres")
        for e in list(schema.expressions):
            if isinstance(e, exp.PrimaryKey):
                e.pop()
        for col in schema.find_all(exp.ColumnDef):
            keep = [c for c in col.args.get("constraints") or []
                    if not isinstance(c.kind, exp.NotNullColumnConstraint)]
            if col.name in pk and not any(
                    isinstance(c.kind, exp.PrimaryKeyColumnConstraint) for c in keep):
                keep.append(exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint()))
            col.set("constraints", keep)
        self.chronos = tree.sql(dialect="postgres")


def _keyed_insert(sql: str):
    """(statement, rows) for an INSERT into a table with the hidden key:
    the key column added, with one :_bb_rowid<i> parameter per row."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="postgres")
    schema, values = tree.this, tree.expression
    if not isinstance(schema, exp.Schema) or not isinstance(values, exp.Values):
        raise RuntimeError("an INSERT into a table without a primary key needs "
                           "a column list and VALUES on Chronos")
    schema.append("expressions", exp.to_identifier(ROWID_COLUMN))
    for i, row in enumerate(values.expressions):
        row.append("expressions", exp.Placeholder(this=f"{ROWID_COLUMN}{i}"))
    return _named(tree.sql(dialect="postgres")), len(values.expressions)


def _expand_between(sql: str) -> str:
    """UPDATE/DELETE ... WHERE x BETWEEN a AND b as two comparisons:
    Chronos' write path does not take BETWEEN (its reads do)."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="postgres")

        def expand(node):
            if isinstance(node, exp.Between):
                return exp.paren(exp.and_(
                    exp.GTE(this=node.this.copy(), expression=node.args["low"].copy()),
                    exp.LTE(this=node.this.copy(), expression=node.args["high"].copy())))
            return node
        return _named(tree.transform(expand).sql(dialect="postgres"))
    except Exception:
        return sql


def _named(sql: str) -> str:
    """sqlglot writes named parameters as %(name)s; Chronos wants :name."""
    return re.sub(r"%\((\w+)\)s", r":\1", sql)


class _Cursor:
    """The part of a DB-API cursor the harness uses."""

    def __init__(self, suite):
        self._suite = suite
        self.description = None
        self.rowcount = -1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def execute(self, query: str, vars=None):
        self.description, self._rows, self.rowcount = self._suite._run(query, vars)

    def fetchall(self) -> list:
        rows, self._rows = self._rows or [], []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        self._rows = []


class _Connection:
    """What the harness sees as ``suite.conn``: cursors that run on the
    Chronos session of the ref the suite is connected to."""

    def __init__(self, suite):
        self._suite = suite

    def cursor(self) -> _Cursor:
        return _Cursor(self._suite)

    def close(self):
        self._suite._close_context()


class ChronosToolSuite(DBToolSuite):
    BACKEND_NAME = "chronos"
    SUPPORTS_COMMIT_REFS = True
    SUPPORTS_MULTI_REF_EXEC = False
    IMPLEMENTATION = {
        "log": "composed",
        "rebase": "composed",
        "revert": "simulated",
        "reset": "simulated",
    }
    IMPLEMENTATION_NOTES = {
        "branch": "create_branch (metadata-only fork); create_branch_from_checkpoint for a commit ref",
        "commit": "create_checkpoint; the message is checkpoint metadata",
        "diff": "ChronosBranchContext.diff (rows); a commit ref through a throwaway branch",
        "log": "list_checkpoints along the fork/merge lineage kept in branch and checkpoint metadata",
        "merge": "merge_apply: snapshot_isolation, then target_wins/source_wins on a write-write "
                 "conflict (conflicts from merge_preview); a checkpoint records the merge; "
                 "unsupported with CHRONOS_SCHEMA_BRANCHING=1",
        "rebase": "merge_apply of the upstream into the branch; unsupported with "
                  "CHRONOS_SCHEMA_BRANCHING=1",
        "revert": "diff of the commit against its predecessor, applied with upsert_rows/delete_keys",
        "reset": "diff of the branch against the commit, applied with upsert_rows/delete_keys",
        "delete": "delete_branch, which also removes the branches forked from the deleted one",
        "commit_refs": "read-only checkpoint sessions (checkout_checkpoint)",
    }

    # Behaviour seen during the run, for the e2e stats.
    OBSERVED: dict = {}

    @classmethod
    def supports(cls, verb: str) -> bool:
        if verb in ("merge", "rebase") and CHRONOS_SCHEMA_BRANCHING:
            return False   # Chronos does not merge schema-branched tables
        return super().supports(verb)

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return cls.get_initial_connection_uri("postgres")

    @classmethod
    def get_initial_connection_uri(cls, db_name: str) -> str:
        return dbutil.format_db_uri(
            CHRONOS_USER, CHRONOS_PASSWORD, CHRONOS_HOST, CHRONOS_PORT, db_name
        )

    @classmethod
    def init_for_bench(
        cls,
        collector: rc.ResultCollector,
        db_name: str,
        default_branch_name: str = MAIN_BRANCH,
        measure_storage: bool = False,
    ):
        return cls(collector, db_name, default_branch_name, measure_storage)

    def __init__(
        self,
        collector: rc.ResultCollector,
        db_name: str,
        default_branch_name: str = MAIN_BRANCH,
        measure_storage: bool = False,
    ):
        super().__init__(_Connection(self), collector, measure_storage)
        self.db_name = db_name
        self.default_branch = default_branch_name
        self._api = _chronos()
        self._ctx = self._api.ChronosBranchContext.connect(
            self.get_initial_connection_uri(db_name),
            enable_schema_branching=CHRONOS_SCHEMA_BRANCHING,
        )
        self._session = None
        self._tx = None            # the open session.transaction(), if any
        self._binds: dict = {}     # statement -> its translation
        self._selects: dict = {}   # translated SELECT -> _SelectPlan
        self._inserts: dict = {}   # INSERT into a keyless table -> (statement, rows)
        self._keyless: set = _KEYLESS.setdefault(db_name, set())
        self._lineage: dict = _LINEAGE.setdefault(db_name, {})
        # Throwaway branches that materialise a commit: commit id -> branch.
        self._temp_branches: dict = {}
        # Plain PostgreSQL connection for sizes and server commands.
        self._raw = None
        self._raw_lock = threading.Lock()
        self._connect_impl(Ref(default_branch_name))
        self._current_ref = Ref(default_branch_name)

    # ------------------------------------------------------------------
    # Statements (the DB-API facade ends up here)
    # ------------------------------------------------------------------

    def _run(self, query: str, vars) -> tuple:
        """Run one statement on the current session; returns
        (description, rows, rowcount)."""
        head = _head(query)
        if head in ("BEGIN", "START"):
            self._begin()
            return None, None, 0
        if head in ("COMMIT", "END"):
            self._end(commit=True)
            return None, None, 0
        if head == "ROLLBACK":
            self._end(commit=False)
            return None, None, 0
        if head in _ADMIN_HEADS:
            self._raw_execute(query)
            return None, None, 0
        if self._session is None:
            raise RuntimeError("Chronos suite is not connected to a ref")
        key = (query, type(vars) is dict, len(vars) if vars else 0)
        sql = self._binds.get(key)
        if sql is None:
            sql = _bind(query, vars)[0]
            if head in ("UPDATE", "DELETE") and "BETWEEN" in sql.upper():
                sql = _expand_between(sql)
            self._remember(self._binds, key, sql)
        params = None
        if vars is not None:
            params = dict(vars) if isinstance(vars, dict) else {
                f"p{i}": v for i, v in enumerate(vars)}
        if head in _READ_HEADS:
            plan = self._selects.get(sql)
            if plan is None:
                plan = self._remember(self._selects, sql, _plan_select(sql))
            rows = self._statement(self._session.query, plan.sql, params)
            flags = plan.expressions
            if flags is None or (rows and len(rows[0]) != len(flags)):
                out = [tuple(_coerce(v, False) for v in r.values()) for r in rows]
            else:
                out = [tuple(_coerce(v, f) for v, f in zip(r.values(), flags)) for r in rows]
            names = list(rows[0]) if rows else ["?column?"]
            return [(n, None, None, None, None, None, None) for n in names], out, len(out)
        if head == "CREATE" and _CREATE_TABLE_RE.match(query):
            return None, None, self._create_table(sql, params)
        if head == "INSERT" and self._keyless:
            m = _INSERT_RE.match(sql)
            if m and m.group(1).lower() in self._keyless:
                return None, None, self._insert_keyless(sql, params)
        return None, None, self._statement(self._session.execute, sql, params).rowcount

    def _statement(self, fn, sql: str, params):
        """Run one statement; outside a transaction it waits out a barrier
        (inside one the failure has already aborted the transaction)."""
        if self._tx is not None:
            return fn(sql, params)
        return self._busy(fn, sql, params)

    def _insert_keyless(self, sql: str, params) -> int:
        plan = self._inserts.get(sql)
        if plan is None:
            plan = self._remember(self._inserts, sql, _keyed_insert(sql))
        keyed, rows = plan
        params = dict(params or {})
        for i in range(rows):
            params[f"{ROWID_COLUMN}{i}"] = uuid.uuid4().int >> 66   # 62 random bits
        return self._statement(self._session.execute, keyed, params).rowcount

    @staticmethod
    def _remember(cache: dict, key, value):
        if len(cache) >= _CACHE_MAX:
            cache.clear()
        cache[key] = value
        return value

    def _create_table(self, sql: str, params) -> int:
        ddl = _TableDDL(sql)
        if CHRONOS_SCHEMA_BRANCHING:
            rowcount = self._statement(self._session.execute, ddl.chronos, params).rowcount
            if ddl.keyless:
                self._keyless.add(ddl.table.lower())
            return rowcount
        # Without schema branching a table is created in PostgreSQL and
        # registered, which makes it visible on every branch.
        if self._current_ref is None or self._current_ref.branch != self.default_branch:
            raise RuntimeError(
                "CREATE TABLE on a branch needs CHRONOS_SCHEMA_BRANCHING=1 "
                "(without it Chronos tables exist on every branch)")
        self._raw_execute(ddl.postgres)
        self._ctx.register_table(ddl.table, ddl.pk)
        if ddl.keyless:
            self._keyless.add(ddl.table.lower())
        return 0

    def _begin(self) -> None:
        if self._tx is not None:
            raise RuntimeError("a transaction is already open")
        tx = self._session.transaction()
        tx.__enter__()
        self._tx = tx

    def _end(self, commit: bool) -> None:
        tx, self._tx = self._tx, None
        if tx is None:
            return
        if commit:
            tx.__exit__(None, None, None)
        else:
            # Chronos rolls back when the block raises; the context manager
            # hands the exception back without raising it here.
            exc = _Rollback()
            tx.__exit__(_Rollback, exc, None)

    def _raw_conn(self):
        if self._raw is None or self._raw.closed:
            self._raw = connect(self.db_name)
        return self._raw

    def _raw_execute(self, query: str, vars=None):
        with self._raw_lock:
            with self._raw_conn().cursor() as cur:
                cur.execute(query, vars)
                return cur.fetchall() if cur.description is not None else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bounded(self, fn, what: str) -> bool:
        """Run a Chronos call that may wait on its GC thread forever (see
        _GC_WAIT_SEC); False if it was left behind."""
        t = threading.Thread(target=fn, daemon=True, name="chronos-bounded")
        t.start()
        t.join(_GC_WAIT_SEC)
        if not t.is_alive():
            return True
        print(f"Warning: Chronos {what} did not return in {_GC_WAIT_SEC:g}s "
              "(interval GC wait); going on without it")
        self.OBSERVED["gc_waits_abandoned"] = self.OBSERVED.get("gc_waits_abandoned", 0) + 1
        return False

    def _busy(self, fn, *args, **kwargs):
        """Call fn, again while its branch is behind another context's
        barrier (see _BUSY_MARKER). The wait is part of the operation."""
        deadline = time.monotonic() + _BUSY_WAIT_SEC
        delay = 0.001
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if _BUSY_MARKER not in str(e) or time.monotonic() >= deadline:
                    raise
            self._check_cancel()
            time.sleep(delay)
            delay = min(delay * 2, 0.05)

    def _on(self, ref: Ref) -> str:
        """Make sure the session is on ref's branch; returns the branch."""
        head = Ref(ref.branch)
        if self._current_ref != head or self._session is None:
            self._connect_impl(head)
            self._current_ref = head
        return ref.branch

    def _close_session(self) -> None:
        if self._tx is not None:
            try:
                self._end(commit=False)
            except Exception:
                pass
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _temp_branch_for(self, commit: str) -> str:
        """A branch holding the state of ``commit``, created on first use
        and deleted when the suite closes (Chronos diffs branches, not
        checkpoints)."""
        name = self._temp_branches.get(commit)
        if name is None:
            name = (f"{_TEMP_PREFIX}{commit}_{os.getpid() % 10000}"
                    f"_{rc.get_current_thread_id()}_{len(self._temp_branches)}")
            self._busy(self._ctx.create_branch_from_checkpoint, name, commit)
            self._temp_branches[commit] = name
        return name

    def _spec(self, ref: Ref) -> str:
        """Branch name Chronos can diff for this ref."""
        return self._temp_branch_for(ref.commit) if ref.commit else ref.branch

    def _merge_unsupported(self, verb: str, exc: Exception):
        """Chronos reports what its merge cannot do (schema-branched
        tables, branches without a shared fork base) as an error; that is
        an unsupported operation, not a failed one."""
        msg = str(exc)
        if "chronos_native_merge_unsupported" in msg:
            return self._unsupported(verb, msg.split("chronos_native_merge_unsupported:", 1)[-1].strip())
        return exc

    def _apply_changes(self, branch: str, changes: list) -> int:
        """Write a diff's right-hand side into ``branch`` in one
        transaction."""
        self._on(Ref(branch))
        deletes, upserts, order = {}, {}, []
        for c in changes:
            if c.table not in order:
                order.append(c.table)
            if c.after is not None:
                upserts.setdefault(c.table, []).append(c.after)
            else:
                deletes.setdefault(c.table, []).append(c.key)
        with self._session.transaction():
            for table in order:
                if table in deletes:
                    self._session.delete_keys(table, deletes[table])
                if table in upserts:
                    self._session.upsert_rows(table, upserts[table])
        return len(changes)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _storage_bytes(self) -> int:
        """pg_database_size of the database: user rows of every branch
        (stored once, with their intervals) plus Chronos' metadata."""
        try:
            return int(self._raw_execute("SELECT pg_database_size(current_database())")[0][0])
        except psycopg2.OperationalError as e:
            if "does not exist" in str(e):
                return 0   # measured after the run's database was dropped
            raise

    def _gc_impl(self) -> None:
        # Chronos reclaims the rows of deleted branches in the background;
        # wait for that, then let PostgreSQL release the space.
        self._bounded(self._ctx.wait_for_background_work, "background work")
        self._raw_execute("VACUUM")

    def _connect_impl(self, ref: Ref) -> None:
        self._close_session()
        if ref.commit:
            self._session = self._busy(self._ctx.checkout_checkpoint, ref.commit)
        else:
            self._session = self._busy(self._ctx.checkout, ref.branch)

    def _branch_impl(self, name: str, from_ref: Ref) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if from_ref.commit:
            lineage = {"parent": from_ref.branch, "from_commit": from_ref.commit}
            self._busy(self._ctx.create_branch_from_checkpoint, name, from_ref.commit)
            # No metadata argument on this call; the lineage is for log().
            self._busy(self._ctx.update_branch_metadata, name, lineage)
        else:
            lineage = {"parent": from_ref.branch, "forked_at": now}
            self._busy(self._ctx.create_branch, name, from_branch=from_ref.branch,
                       metadata=lineage)
        self._lineage[name] = lineage

    def _commit_impl(self, ref: Ref, message: str, **metadata) -> str:
        commit_id = "c" + uuid.uuid4().hex[:15]
        self._busy(self._ctx.create_checkpoint,
                   commit_id, branch=ref.branch, metadata={"message": message or "", **metadata})
        return commit_id

    def _log_entries(self, ref: Ref) -> list:
        """Checkpoints reachable from ref, newest first: the branch's own,
        those of a branch merged into it (up to the merge) and those of the
        branch it was forked from (up to the fork)."""
        out, seen = {}, set()
        pending = [(ref.branch, None)]
        if ref.commit:
            pending = [(ref.branch, self._ctx.get_checkpoint(ref.commit).created_at)]
        while pending:
            branch, cutoff = pending.pop()
            if branch in seen:
                continue
            seen.add(branch)
            try:
                meta = self._ctx.get_branch(branch).metadata
            except self._api.BranchNotFoundError:
                meta = self._lineage.get(branch, {})   # deleted since
            hidden = meta.get("resets") or []
            for cp in self._ctx.list_checkpoints(branch):
                at = cp.created_at
                if cutoff is not None and at > cutoff:
                    continue
                if any(lo < at <= hi for lo, hi in hidden):
                    continue   # undone by a reset
                out[cp.checkpoint_id] = cp
                source = cp.metadata.get("merged_from")
                if source:
                    pending.append((source, at))
            parent = meta.get("parent")
            if parent:
                fork = meta.get("forked_at")
                if meta.get("from_commit"):
                    try:
                        fork = self._ctx.get_checkpoint(meta["from_commit"]).created_at
                    except Exception:
                        fork = None
                if fork is not None:
                    pending.append((parent, fork if cutoff is None else min(fork, cutoff)))
        return sorted(out.values(), key=lambda cp: cp.created_at, reverse=True)

    def _log_impl(self, ref: Ref, limit: int) -> list:
        return [
            {"commit_hash": cp.checkpoint_id, "committer": "chronos", "email": "",
             "date": cp.created_at, "message": cp.metadata.get("message", ""),
             "branch": cp.branch_id}
            for cp in self._log_entries(ref)[:int(limit)]
        ]

    def _diff_impl(self, ref_a: Ref, ref_b: Ref) -> dict:
        diff = self._busy(self._ctx.diff, self._spec(ref_a), self._spec(ref_b))
        tables = {}
        for c in diff.changes:
            t = tables.setdefault(c.table, {"table_name": c.table, "rows_added": 0,
                                            "rows_deleted": 0, "rows_modified": 0})
            t["rows_" + c.change] += 1
        out = list(tables.values())
        return {
            "tables": out,
            "rows_added": sum(t["rows_added"] for t in out),
            "rows_deleted": sum(t["rows_deleted"] for t in out),
            "rows_modified": sum(t["rows_modified"] for t in out),
        }

    # ------------------------------------------------------------------
    # Merge and rebase
    # ------------------------------------------------------------------

    @staticmethod
    def _conflict_payload(conflicts: list) -> list:
        """Chronos' conflicts in the shape a resolve callable gets: per
        table, rows with our_*/their_* columns (the target's and the
        source's version; Chronos does not report the base)."""
        tables = {}
        for c in conflicts:
            ours, theirs = c.before or {}, c.after or {}
            row = {f"our_{k}": v for k, v in (ours or c.key).items()}
            row.update({f"their_{k}": v for k, v in (theirs or c.key).items()})
            row["our_diff_type"] = "modified" if c.before else "removed"
            row["their_diff_type"] = "modified" if c.after else "removed"
            tables.setdefault(c.table, []).append(row)
        return [{"table": t, "rows": rows} for t, rows in tables.items()]

    def _merge(self, verb: str, into: Ref, source: Ref, on_conflict) -> dict:
        """Merge source's branch into into's branch; conflicts (rows both
        changed since the fork) are resolved per on_conflict."""
        if CHRONOS_SCHEMA_BRANCHING:
            # Known up front; asking Chronos would first queue for the
            # branch locks a busy spine holds.
            raise self._unsupported(verb, "schema-branching merge")
        target, src = into.branch, source.branch
        info = {"conflicts": 0, "conflict_tables": [], "resolved": "",
                "fast_forward": False, "applied": 0}
        try:
            if not callable(on_conflict):
                try:
                    info["applied"] = self._busy(
                        self._ctx.merge_apply, src, target, policy="snapshot_isolation").applied
                    return info
                except self._api.UnsupportedSQLError:
                    raise
                except self._api.BranchingError as e:
                    if "conflict" not in str(e).lower():
                        raise
            conflicts = self._busy(self._ctx.merge_preview, src, target).conflicts
            info["conflicts"] = len(conflicts)
            info["conflict_tables"] = sorted({c.table for c in conflicts})
            policy = "source_wins" if on_conflict == "theirs" else "target_wins"
            if callable(on_conflict) and conflicts:
                # The callable writes its resolution into the target; the
                # merge then keeps the target's version of those rows.
                self._on(into)
                info["resolution"] = on_conflict(
                    self._conflict_session(Ref(target)), self._conflict_payload(conflicts))
                info["resolved"] = "custom"
            elif conflicts:
                info["resolved"] = on_conflict
            info["applied"] = self._busy(
                self._ctx.merge_apply, src, target, policy=policy).applied
            return info
        except self._api.UnsupportedSQLError as e:
            raise self._merge_unsupported(verb, e) from e

    def _merge_impl(self, into: Ref, source: Ref, message: str,
                    on_conflict="ours") -> dict:
        info = self._merge("merge", into, source, on_conflict)
        # The merge commit: a restorable point, and how log() finds the
        # source's commits from the target.
        info["hash"] = self._commit_impl(
            Ref(into.branch), message or f"Merge {source.branch} into {into.branch}",
            merged_from=source.branch)
        return info

    def _rebase_impl(self, ref: Ref, onto: Ref, on_conflict="ours") -> dict:
        info = self._merge("rebase", ref, onto, on_conflict)
        info["up_to_date"] = info["applied"] == 0
        return info

    # ------------------------------------------------------------------
    # Reset and revert (Chronos has neither; both apply a diff)
    # ------------------------------------------------------------------

    def _reset_impl(self, ref: Ref, to: str) -> None:
        target = self._ctx.get_checkpoint(to)
        changes = self._busy(self._ctx.diff, ref.branch, self._temp_branch_for(to)).changes
        if changes:
            self._apply_changes(ref.branch, changes)
        # Checkpoints cannot be deleted; hide the ones the reset undid.
        info = self._ctx.get_branch(ref.branch)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        resets = list(info.metadata.get("resets") or []) + [[target.created_at, now]]
        self._busy(self._ctx.update_branch_metadata,
                   ref.branch, {**info.metadata, "resets": resets})

    def _revert_impl(self, ref: Ref, commit: str) -> None:
        cp = self._ctx.get_checkpoint(commit)
        history = self._log_entries(Ref(cp.branch_id, commit))
        if len(history) < 2:
            raise RuntimeError(
                f"commit {commit} has no predecessor checkpoint to revert to")
        before = history[1].checkpoint_id
        changes = self._busy(
            self._ctx.diff, self._temp_branch_for(commit), self._temp_branch_for(before)).changes
        if changes:
            self._apply_changes(ref.branch, changes)
        self._commit_impl(Ref(ref.branch), f"revert {commit}")

    def _descendants(self, branch: str) -> list:
        """Branches forked (directly or not) from ``branch``, as far as
        this process created them."""
        out, frontier = [], [branch]
        links = list(self._lineage.items())
        while frontier:
            parent = frontier.pop()
            for name, meta in links:
                if meta.get("parent") == parent and name not in out:
                    out.append(name)
                    frontier.append(name)
        return out

    def _delete_impl(self, ref: Ref) -> None:
        """delete_branch removes the branch with every branch forked from
        it. A later delete of one of those finds it gone already, which is
        the state that delete asked for."""
        name = ref.branch
        if self._current_ref and self._current_ref.branch == name:
            self._connect_impl(Ref(self.default_branch))
            self._current_ref = Ref(self.default_branch)
        try:
            self._busy(self._ctx.delete_branch, name)
        except self._api.BranchNotFoundError:
            if not self._lineage.get(name, {}).get("deleted_with"):
                raise
            return
        if name in self._lineage:
            self._lineage[name]["deleted"] = True
        for child in self._descendants(name):
            meta = self._lineage.get(child)
            if meta is not None and not (meta.get("deleted") or meta.get("deleted_with")):
                meta["deleted_with"] = name
                self.OBSERVED["branches_deleted_with_an_ancestor"] = \
                    self.OBSERVED.get("branches_deleted_with_an_ancestor", 0) + 1

    def list_branches(self) -> list:
        return [b.branch_id for b in self._ctx.list_branches()
                if not b.branch_id.startswith(_TEMP_PREFIX)]

    # ------------------------------------------------------------------
    # Connection utilities
    # ------------------------------------------------------------------

    def _close_context(self) -> None:
        """Close the session and the Chronos context (its connections)."""
        self._close_session()
        ctx, self._ctx = self._ctx, None
        if ctx is None:
            return
        for tmp in list(self._temp_branches.values()):
            try:
                ctx.delete_branch(tmp)
            except Exception:
                pass
        self._temp_branches.clear()
        def close():
            try:
                ctx.close()
            except Exception as e:
                print(f"Warning: closing the Chronos context failed: {e}")
        self._bounded(close, "context close")

    def close_connection(self) -> None:
        super().close_connection()
        with self._raw_lock:
            if self._raw is not None and not self._raw.closed:
                self._raw.close()
            self._raw = None


class _Rollback(Exception):
    """Raised into session.transaction() to roll it back."""
