"""Chronos DB backend (a branching database speaking the Postgres protocol).

Not to be confused with ``dblib/chronos.py``: that backend is MIT's Chronos
(mitdbg/chronos), a Python library that adds interval branching to a
PostgreSQL database. Chronos DB (https://github.com/Abhishekxdg/chronosdb-engine)
is a separate system: a database server of its own (Rust, Postgres wire
protocol, `chronos serve`) whose copy-on-write branches are called *worlds*.

Every verb is one or two SQL statements of the engine's, sent over psycopg:

* a branch is a world: ``CREATE WORLD x FROM src`` (a fork copies two
  pointers), connecting is ``SWITCH WORLD x``, ``delete()`` is ``DROP WORLD``;
* a commit is a moment: the engine keeps every version of every world for
  ``history_retention``, so a commit records the server's clock (ms) and a
  row in the world's ``_bb_commits`` log table; ``'x@<time>'`` then names
  the world as it was (``DIFF ... TO``, ``CREATE WORLD ... FROM``, a
  read-only ``USE WORLD``), and ``reset()`` is ``RESTORE WORLD x TO <time>``;
* ``diff()`` is ``DIFF WORLD a TO b SUMMARY``; ``merge()`` is ``MERGE WORLD
  ... BY COLUMNS USING OURS|THEIRS`` (three-way against the fork point, its
  conflicts counted from its NOTICE), ``rebase()`` is ``REBASE WORLD``; the
  rows a resolve callable gets come from ``MERGE ... DRY RUN``; ``revert()``
  is ``UNDO DIFF 'x@<before>' TO 'x@<commit>'``.

Those are Chronos DB 0.4.2's statements; on 0.4.1 (``version()`` tells, or
``CHRONOSDB_LEGACY=1``) diff reads ``DIFF``'s rows, a merge is retried after
a ``DRY RUN`` lists its conflicts, and revert runs ``DIFF ... AS SQL``.

The run's database is a world of its own: ``CREATE DATABASE <db>`` makes an
empty world with no parent, which the run uses as its ``main``; branch ``x``
is the world ``<db>__x``. One ``chronos serve`` folder can hold several runs,
one at a time for clean storage numbers (storage is ``SHOW DISK``'s total,
the whole folder).

Settings: ``CHRONOSDB_HOST`` (127.0.0.1), ``CHRONOSDB_PORT`` (5433),
``CHRONOSDB_USER`` (chronos), ``CHRONOSDB_PASSWORD`` (the server's token, if
it has one), ``CHRONOSDB_HISTORY_RETENTION`` (1 day: how far back commits
stay readable; set server-wide at setup), ``CHRONOSDB_BY_COLUMNS`` (1: merge
column by column; 0 for whole rows), ``CHRONOSDB_LEGACY`` (auto).
"""

import os
import re
import threading
import time
from typing import Optional

import psycopg2
from psycopg2 import errors as pgerrors
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from dblib.db_api import DBToolSuite, Ref, UnsupportedOperation
import dblib.result_collector as rc
import dblib.util as dbutil

CHRONOSDB_HOST = (os.environ.get("CHRONOSDB_HOST") or os.environ.get("DBFORK_SERVER_HOST")
                  or "127.0.0.1")
CHRONOSDB_PORT = int(os.environ.get("CHRONOSDB_PORT", "5433"))
CHRONOSDB_USER = os.environ.get("CHRONOSDB_USER", "chronos")
CHRONOSDB_PASSWORD = os.environ.get("CHRONOSDB_PASSWORD", "")
HISTORY_RETENTION = os.environ.get("CHRONOSDB_HISTORY_RETENTION", "1 day")
# Merge a row both sides changed column by column (only a column both changed
# conflicts), as Dolt merges cells; 0 for Chronos DB's whole-row default.
BY_COLUMNS = os.environ.get("CHRONOSDB_BY_COLUMNS", "1") not in ("0", "false", "no")

# Chronos DB 0.4.2 added what the verbs use when they can: a merge's conflict
# count in its NOTICE, DIFF ... SUMMARY, UNDO DIFF. "auto" checks the server's
# version; 1 uses the 0.4.1 statements, 0 the new ones whatever the version says.
LEGACY = os.environ.get("CHRONOSDB_LEGACY", "auto")

MAIN_BRANCH = "main"
LOG_TABLE = "_bb_commits"
SEP = "__"


def _uri(db: str) -> str:
    return dbutil.format_db_uri(CHRONOSDB_USER, CHRONOSDB_PASSWORD, CHRONOSDB_HOST,
                                CHRONOSDB_PORT, db)


def _connect(db: str):
    conn = psycopg2.connect(_uri(db))
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _q(name: str) -> str:
    """A world name as an identifier."""
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _moment(ms: int) -> str:
    """ms since 1970 as the UTC time the engine reads in 'world@time'."""
    sec, frac = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(sec)) + f".{frac:03d}+00"


def _sqlstate(e: Exception) -> str:
    return getattr(e, "pgcode", None) or ""


_SETTLED = re.compile(r"(\d+) rows both sides changed: (\d+) combined by columns")


def _settled(notices) -> Optional[tuple]:
    """(rows both sides changed, of them combined by columns) from a
    MERGE/REBASE's NOTICE, or None if it sent none."""
    for n in reversed(notices):
        m = _SETTLED.search(n)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _new_verbs(version: str) -> bool:
    """Whether a server of this version() has 0.4.2's statements (see LEGACY)."""
    if LEGACY != "auto":
        return LEGACY in ("0", "false", "no")
    m = re.search(r"Chronos (\d+)\.(\d+)\.(\d+)", version or "")
    return bool(m) and tuple(int(x) for x in m.groups()) >= (0, 4, 2)


# ----------------------------------------------------------------------------
# Process-wide state: every worker's suite of one run shares it.
# ----------------------------------------------------------------------------


class _Registry:
    """Commits by id (world, moment, the world's previous commit), the
    worlds a merge consumed (Chronos drops a world merged whole into its
    parent), and parents (a world's parent never changes)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.commits: dict = {}        # id -> (world, ms, prev_id | None)
        self.last_commit: dict = {}    # world -> id of its newest commit
        self.forked_at: dict = {}      # world -> ms just after its fork
        self.merged_into: dict = {}    # world -> world it was merged into (and dropped)
        self.parents: dict = {}        # world -> parent world (None for a root)
        self.deferred: set = set()     # worlds whose DROP waits for their forks
        self.new_verbs = None          # whether the server has 0.4.2's statements (see LEGACY)
        self.seq = 0

    def next_id(self, ms: int) -> str:
        with self.lock:
            self.seq += 1
            return f"c{ms}_{self.seq}"


_REG = _Registry()


class ChronosDBToolSuite(DBToolSuite):
    BACKEND_NAME = "chronosdb"
    # SHOW DISK's total: the whole folder of the server.
    STORAGE_SCOPE = "server"
    SUPPORTS_COMMIT_REFS = True
    # A statement reads one world (plus AS OF of that world's own past);
    # there is no reference to another world's table.
    SUPPORTS_MULTI_REF_EXEC = False
    SCHEMA_DIALECT = "postgres"
    SCHEMA_DIFF_HALF = "native"

    IMPLEMENTATION = {
        "branch": "native",
        "commit": "composed",
        "diff": "native",
        "log": "simulated",
        "merge": "native",
        "rebase": "native",
        "revert": "native",
        "reset": "native",
        "delete": "native",
        "commit_refs": "native",
    }
    IMPLEMENTATION_NOTES = {
        "branch": "CREATE WORLD <db>__x FROM <src> (copy-on-write: a fork copies two pointers); "
                  "from a commit, FROM '<world>@<moment>' (the engine rebuilds that moment "
                  "from its log, once per moment, then caches it)",
        "commit": "a row in the world's _bb_commits table, then the server clock (ms) as the "
                  "commit's moment. The engine keeps every version for history_retention, so "
                  "'world@moment' reads it back; no data is copied. The adapter waits for the "
                  "next millisecond so later writes fall after the moment",
        "diff": "DIFF WORLD a TO b SUMMARY (refs at a commit are 'world@moment'): rows counted "
                "per table without reading them, and the changes to tables themselves; before "
                "Chronos DB 0.4.2, DIFF WORLD a TO b, its rows counted. Rows of _bb_ tables left "
                "out; the schema half from the same table-level rows (create/alter/drop table)",
        "log": "SELECT from the _bb_commits bookkeeping table (Chronos has moments, not "
               "commit messages); forks inherit it, merges carry it, a reset rewinds it",
        "merge": "MERGE WORLD src (into its parent; the engine then drops src, so a later "
                 "delete() of it is a no-op), REBASE WORLD into (merging a parent into its "
                 "child keeps the child: Chronos's rebase is that merge), or MERGE WORLD src "
                 "INTO dst (any other pair; src stays open). Three-way against src's fork "
                 "point, BY COLUMNS (a row both sides changed in different columns combines; "
                 "only a column both changed conflicts, as Dolt counts cells). For ours/theirs "
                 "one statement with USING OURS|THEIRS, the conflicts counted from its NOTICE "
                 "(Chronos DB 0.4.2 on). Before 0.4.2, and for a resolve callable: a conflict "
                 "fails the plain merge (40001), MERGE ... DRY RUN lists the conflicting rows "
                 "and the merge is redone with USING; the callable runs after a merge that "
                 "kept the target's rows. Chronos has no fast-forward: every merge is "
                 "three-way (fast_forward is always False)",
        "rebase": "REBASE WORLD x (onto its parent, the only rebase Chronos has: the fork point "
                  "moves to the parent as it is now, x's changes kept on top). Rebasing a "
                  "parent onto its own child is MERGE WORLD child (the child's changes into "
                  "the parent, with the parent's own changes winning per on_conflict). Any "
                  "other pair fails. Conflicts as for merge",
        "revert": "UNDO DIFF 'w@previous commit' TO 'w@commit' EXCEPT TABLES (_bb_commits) "
                  "on the branch, then a commit: the engine puts back each row the commit "
                  "changed. A row changed again since fails it (40001), as a conflict fails "
                  "git's and Dolt's revert. Before Chronos DB 0.4.2: DIFF WORLD 'w@commit' TO "
                  "'w@previous commit' AS SQL, its statements run in one transaction, "
                  "overwriting rows changed since (composed)",
        "reset": "RESTORE WORLD x TO <moment> (an ordinary write of the old rows; history "
                 "keeps what it replaced). Refused by the engine for a moment before the "
                 "world's last rebase",
        "delete": "DROP WORLD x; refused while x has forks (2BP01), so it is deferred and "
                  "retried after later deletes and at close; a world a merge already "
                  "consumed is gone and the delete is a no-op",
        "commit_refs": "'world@moment', read only (USE WORLD; writes fail with 25006)",
        "multi_ref_exec": "unsupported: a statement reads one world; cross-branch reads run "
                          "once per branch",
    }

    OBSERVED: dict = {}
    _OBS_LOCK = threading.Lock()

    @classmethod
    def observe(cls, key: str, n: int = 1) -> None:
        with cls._OBS_LOCK:
            cls.OBSERVED[key] = cls.OBSERVED.get(key, 0) + n

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return _uri("main")

    @classmethod
    def get_initial_connection_uri(cls, db_name: str) -> str:
        return _uri(db_name)

    @classmethod
    def init_for_bench(cls, collector: rc.ResultCollector, db_name: str,
                       default_branch_name: str = MAIN_BRANCH, measure_storage: bool = False):
        return cls(_connect(db_name), collector, db_name, default_branch_name, measure_storage)

    def __init__(self, connection, collector: rc.ResultCollector, db_name: str,
                 default_branch_name: str = MAIN_BRANCH, measure_storage: bool = False):
        super().__init__(connection, collector, measure_storage)
        self.db_name = db_name
        self.default_branch = default_branch_name or MAIN_BRANCH
        self._head = connection      # the suite's connection; self.conn is it unless on a past moment
        self._past: dict = {}        # commit id -> connection to 'world@moment' (see _connect_impl)
        self._side = None            # connection for storage samples (another thread)
        self._side_lock = threading.Lock()
        # the connection opens on the run's main; a worker may start elsewhere
        if self.default_branch != MAIN_BRANCH:
            self._execute(f"SWITCH WORLD {_q(self._world(self.default_branch))}")
        self._current_ref = Ref(self.default_branch)

    # ------------------------------------------------------------------
    # Names
    # ------------------------------------------------------------------

    def _world(self, branch: str) -> str:
        """The world behind a benchmark branch name."""
        if branch == MAIN_BRANCH:
            return self.db_name
        return f"{self.db_name}{SEP}{branch}"

    def _commit(self, commit: str) -> tuple:
        """(world, ms, prev_id) of a commit id."""
        with _REG.lock:
            c = _REG.commits.get(commit)
        if c is None:
            raise ValueError(f"unknown commit {commit!r}")
        return c

    def _at(self, ref: Ref) -> str:
        """What names ``ref`` in DIFF / CREATE WORLD ... FROM / USE WORLD:
        the world, or the world at a commit's moment (a string)."""
        if not ref.commit:
            return _q(self._world(ref.branch))
        world, ms, _ = self._commit(ref.commit)
        return _lit(f"{world}@{_moment(ms)}")

    def _parent(self, world: str) -> Optional[str]:
        self._home()
        with _REG.lock:
            if world in _REG.parents:
                return _REG.parents[world]
        rows = self._execute("SHOW WORLDS")
        with _REG.lock:
            for r in rows or []:
                _REG.parents.setdefault(r[0], r[2])
            return _REG.parents.get(world)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _storage_bytes(self) -> int:
        with self._side_lock:
            for attempt in range(2):
                try:
                    if self._side is None or self._side.closed:
                        self._side = _connect(self.db_name)
                    with self._side.cursor() as cur:
                        cur.execute("SHOW DISK")
                        for what, n in cur.fetchall():
                            if what == "total":
                                return int(n)
                    return 0
                except psycopg2.OperationalError:
                    self._side = None
                    if attempt:
                        raise
        return 0

    def _gc_impl(self) -> None:
        # A checkpoint folds the log into the trees and the cleanup after
        # it gives back pages nothing needs any more.
        self._execute("CHECKPOINT")

    def _connect_impl(self, ref: Ref) -> None:
        if not ref.commit:
            self._home()
            self._execute(f"SWITCH WORLD {_q(self._world(ref.branch))}")
            return
        self._home()
        try:
            self._execute(f"USE WORLD {self._at(ref)}")
            return
        except psycopg2.Error as e:
            if _sqlstate(e) != "3D000":
                raise
        # Chronos DB up to 0.4.1 switches a session to a past moment only at
        # connect time (database 'world@time'), not with USE WORLD.
        world, ms, _ = self._commit(ref.commit)
        past = self._past.get(ref.commit)
        if past is None or past.closed:
            past = self._past[ref.commit] = _connect(f"{world}@{_moment(ms)}")
            self.observe("past_connections")
        self.conn = past

    def _home(self) -> None:
        """Back on the suite's own connection (off a past moment's)."""
        if self.conn is not self._head:
            self.conn = self._head
            self._current_ref = None

    def _on(self, ref: Ref) -> None:
        if self._current_ref != ref:
            self._connect_impl(ref)
            self._current_ref = ref

    def _now_ms(self) -> int:
        self._home()
        rows = self._execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")
        return int(rows[0][0])

    def _branch_impl(self, name: str, from_ref: Ref) -> None:
        self._home()
        world = self._world(name)
        parent = self._world(from_ref.branch)
        if from_ref.commit:
            parent = self._commit(from_ref.commit)[0]
        self._execute(f"CREATE WORLD {_q(world)} FROM {self._at(from_ref)}")
        ms = self._now_ms()
        with _REG.lock:
            _REG.forked_at[world] = ms
            _REG.merged_into.pop(world, None)
            _REG.parents.pop(world, None)
            if not from_ref.commit:
                _REG.parents[world] = parent

    def _commit_impl(self, ref: Ref, message: str) -> str:
        world = self._world(ref.branch)
        self._on(Ref(ref.branch))
        ms = self._now_ms()
        cid = _REG.next_id(ms)
        with _REG.lock:
            prev = _REG.last_commit.get(world)
        self._execute(f"INSERT INTO {LOG_TABLE} (id, world, at_ms, message) VALUES (%s, %s, %s, %s)",
                      (cid, world, ms, message or ""))
        # The commit's moment is after its own log row; the next write
        # lands in a later millisecond.
        ms = self._now_ms()
        while self._now_ms() <= ms:
            time.sleep(0.0005)
        with _REG.lock:
            _REG.commits[cid] = (world, ms, prev)
            _REG.last_commit[world] = cid
        return cid

    # ---------------- diff ----------------

    def _diff_rows(self, ref_a: Ref, ref_b: Ref) -> list:
        """The diff as (table, id, change, before, after, columns, rows):
        id None for a change to a table itself. From DIFF ... SUMMARY where
        the server has it (one row per table and change, id '*', counted
        without reading the rows), else DIFF's own rows, one each."""
        self._home()
        stmt = f"DIFF WORLD {self._at(ref_a)} TO {self._at(ref_b)}"
        if self._new_verbs():
            return [(t, None if n is None else "*", change, before, after, cols, n or 0)
                    for t, change, n, before, after, cols in self._execute(f"{stmt} SUMMARY") or []]
        return [(*r, 1) for r in self._execute(stmt) or []]

    def _diff_impl(self, ref_a: Ref, ref_b: Ref) -> dict:
        rows = self._diff_rows(ref_a, ref_b)
        self._last_diff = (ref_a, ref_b, rows)
        tables: dict = {}
        totals = {"insert": 0, "delete": 0, "update": 0}
        for table, rid, change, *_, n in rows:
            if rid is None or str(table).startswith("_bb_"):
                continue
            t = tables.setdefault(table, {"table_name": table, "rows_added": 0,
                                          "rows_deleted": 0, "rows_modified": 0})
            key = {"insert": "rows_added", "delete": "rows_deleted", "update": "rows_modified"}.get(change)
            if key:
                t[key] += n
                totals[change] += n
        return {
            "tables": list(tables.values()),
            "rows_added": totals["insert"],
            "rows_deleted": totals["delete"],
            "rows_modified": totals["update"],
        }

    def _schema_diff_impl(self, ref_a: Ref, ref_b: Ref) -> dict:
        """From the diff's table-level rows: create/drop table, and for
        alter table the two schemas the row carries (``before``/``after``),
        columns matched by their id (a rename keeps it): added, dropped,
        and changed in type or size (varchar(n), numeric(p,s), smallint)."""
        last = getattr(self, "_last_diff", None)
        rows = last[2] if last and last[0] == ref_a and last[1] == ref_b else self._diff_rows(ref_a, ref_b)
        out = {"tables_added": [], "tables_dropped": [], "columns_added": {},
               "columns_dropped": {}, "columns_changed": {}}
        for table, rid, change, before, after, _columns, _n in rows:
            if rid is not None or str(table).startswith("_bb_"):
                continue
            if change == "create table":
                out["tables_added"].append(table)
            elif change == "drop table":
                out["tables_dropped"].append(table)
            elif change == "alter table":
                b = {c["id"]: c for c in (_json_obj(before) or {}).get("columns", [])}
                a = {c["id"]: c for c in (_json_obj(after) or {}).get("columns", [])}
                added = sorted(a[i]["name"] for i in a.keys() - b.keys())
                dropped = sorted(b[i]["name"] for i in b.keys() - a.keys())
                changed = sorted([a[i]["name"], _col_type(b[i]), _col_type(a[i])]
                                 for i in a.keys() & b.keys() if _col_type(a[i]) != _col_type(b[i]))
                for k, v in (("columns_added", added), ("columns_dropped", dropped),
                             ("columns_changed", changed)):
                    if v:
                        out[k][table] = v
        for k in ("tables_added", "tables_dropped"):
            out[k].sort()
        out["changed"] = bool(out["tables_added"] or out["tables_dropped"] or out["columns_added"]
                              or out["columns_dropped"] or out["columns_changed"])
        return out

    def _log_impl(self, ref: Ref, limit: int) -> list:
        self._on(ref)
        rows = self._execute(f"SELECT id, world, at_ms, message FROM {LOG_TABLE} "
                             f"ORDER BY at_ms DESC, id DESC LIMIT %s", (int(limit),))
        return [{"commit_hash": r[0], "world": r[1], "date": _moment(r[2]), "message": r[3]}
                for r in rows or []]

    # ---------------- merge / rebase ----------------

    @staticmethod
    def _using(bench_policy: str, bench_ours_is_world: bool) -> str:
        """USING clause for a bench policy. In Chronos OURS is the world
        merged (or rebased) and THEIRS its parent (or the INTO world)."""
        keep_ours = bench_policy != "theirs"
        side_world = keep_ours == bench_ours_is_world
        return "USING OURS" if side_world else "USING THEIRS"

    def _dry_run_conflicts(self, world: str, into: Optional[str], bench_ours_is_world: bool) -> list:
        """Conflicting rows of merging ``world`` (into its parent, or
        ``into``), grouped per table in Dolt's base_/our_/their_ shape."""
        clause = f" INTO {_q(into)}" if into else ""
        cols = " BY COLUMNS" if BY_COLUMNS else ""
        rows = self._execute(f"MERGE WORLD {_q(world)}{clause}{cols} DRY RUN") or []
        by_table: dict = {}
        for table, rid, outcome, _detail, base, ours, theirs, *_ in rows:
            if outcome != "conflict" or table is None or str(table).startswith("_bb_"):
                continue
            our, their = (ours, theirs) if bench_ours_is_world else (theirs, ours)
            row = {}
            for prefix, side in (("base_", base), ("our_", our), ("their_", their)):
                for col, v in (_json_obj(side) or {}).items():
                    row[prefix + col] = v
            row["our_diff_type"] = _diff_type(base, our)
            row["their_diff_type"] = _diff_type(base, their)
            row["id"] = rid
            by_table.setdefault(table, []).append(row)
        return [{"table": t, "rows": rs} for t, rs in by_table.items()]

    def _settle(self, stmt: str, world: str, into: Optional[str], target: Ref,
                bench_ours_is_world: bool, on_conflict) -> dict:
        """Run a MERGE/REBASE statement settling conflicts per ``on_conflict``.
        For ours/theirs on a server that counts them, one statement with USING,
        the count read from its NOTICE. Otherwise (or for a resolve callable,
        which needs the rows) a plain attempt; on a row conflict list the rows,
        then redo it settling them."""
        info = {"fast_forward": False, "conflicts": 0, "conflict_tables": [], "resolved": ""}
        if BY_COLUMNS:
            stmt += " BY COLUMNS"
        if not callable(on_conflict) and self._new_verbs():
            del self.conn.notices[:]
            try:
                info["applied"] = self._rowcount(
                    f"{stmt} {self._using(on_conflict, bench_ours_is_world)}")
            except psycopg2.Error as e:
                msg = str(e).strip()
                if _sqlstate(e) == "40001" and ("columns or constraints" in msg or "schema" in msg.lower()):
                    raise RuntimeError(f"schema conflict; nothing merged: {msg}") from None
                raise
            counts = _settled(self.conn.notices)
            if counts is None:
                self.observe("merge_count_missing")
                return info
            settled, combined = counts
            # combined by columns = no cell both changed: no conflict, as in Dolt
            info["conflicts"] = info["conflict_rows"] = settled - combined
            info["combined_by_columns"] = combined
            if info["conflicts"]:
                info["resolved"] = on_conflict
            return info
        try:
            info["applied"] = self._rowcount(stmt)
            return info
        except psycopg2.Error as e:
            if _sqlstate(e) != "40001":
                raise
            msg = str(e).strip()
            if "columns or constraints" in msg or "schema" in msg.lower():
                raise RuntimeError(f"schema conflict; nothing merged: {msg}") from None
        conflicts = self._dry_run_conflicts(world, into, bench_ours_is_world)
        info["conflicts"] = sum(len(c["rows"]) for c in conflicts)
        info["conflict_rows"] = info["conflicts"]
        info["conflict_tables"] = sorted(c["table"] for c in conflicts)
        policy = "ours" if callable(on_conflict) else on_conflict
        info["applied"] = self._rowcount(f"{stmt} {self._using(policy, bench_ours_is_world)}")
        info["resolved"] = policy
        if callable(on_conflict) and conflicts:
            self._current_ref = None
            self._on(target)
            info["resolution"] = on_conflict(self._conflict_session(target), conflicts)
            info["resolved"] = "custom"
        return info

    def _new_verbs(self) -> bool:
        with _REG.lock:
            known = _REG.new_verbs
        if known is None:
            rows = self._execute("SELECT version()") or [("",)]
            known = _new_verbs(str(rows[0][0]))
            with _REG.lock:
                _REG.new_verbs = known
        return known

    def _rowcount(self, stmt: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(stmt)
            return max(int(cur.rowcount or 0), 0)

    def _merge_impl(self, into: Ref, source: Ref, message: str, on_conflict="ours") -> dict:
        self._home()
        if source.commit or into.commit:
            raise ValueError("merge takes branch heads")
        src, dst = self._world(source.branch), self._world(into.branch)
        with _REG.lock:
            done = _REG.merged_into.get(src)
        if done == dst:
            # The world's changes are all in dst already (a rebase of dst
            # onto it merged it): git would say "already up to date".
            self.observe("merge_already_merged")
            return {"fast_forward": False, "conflicts": 0, "up_to_date": True}
        if done is not None:
            raise RuntimeError(f"{source.branch} was merged into {done} and is gone")
        self._current_ref = None   # a merge may move the session
        if self._parent(src) == dst:
            # bench ours = the target = the parent = Chronos THEIRS
            info = self._settle(f"MERGE WORLD {_q(src)}", src, None, into, False, on_conflict)
            with _REG.lock:
                _REG.merged_into[src] = dst
            return info
        if self._parent(dst) == src:
            # Merging a parent into its child is the child's rebase; bench
            # ours = the target = the child = Chronos OURS.
            info = self._settle(f"REBASE WORLD {_q(dst)}", dst, None, into, True, on_conflict)
            info["as_rebase"] = True
            return info
        # bench ours = the INTO world = Chronos THEIRS
        info = self._settle(f"MERGE WORLD {_q(src)} INTO {_q(dst)}", src, dst, into, False,
                            on_conflict)
        info["into"] = True
        return info

    def _rebase_impl(self, ref: Ref, onto: Ref, on_conflict="ours") -> dict:
        self._home()
        if ref.commit or onto.commit:
            raise ValueError("rebase takes branch heads")
        world, up = self._world(ref.branch), self._world(onto.branch)
        self._current_ref = None
        if self._parent(world) == up:
            # bench ours = the upstream = the parent = Chronos THEIRS
            return self._settle(f"REBASE WORLD {_q(world)}", world, None, ref, False, on_conflict)
        if self._parent(up) == world:
            # Rebasing a parent onto its child: the child's changes go into
            # the parent; bench ours = the upstream = the child = Chronos OURS.
            info = self._settle(f"MERGE WORLD {_q(up)}", up, None, ref, True, on_conflict)
            with _REG.lock:
                _REG.merged_into[up] = world
            info["as_merge"] = True
            return info
        raise RuntimeError(f"Chronos rebases a world only onto its parent; "
                           f"{onto.branch} is neither {ref.branch}'s parent nor its child")

    # ---------------- revert / reset / delete ----------------

    def _revert_impl(self, ref: Ref, commit: str) -> str:
        self._home()
        world, ms, prev = self._commit(commit)
        if prev is not None:
            pworld, pms, _ = self._commit(prev)
        else:
            with _REG.lock:
                pworld, pms = world, _REG.forked_at.get(world)
            if pms is None:
                raise RuntimeError(f"revert of {commit}: no earlier commit or fork moment of {world}")
        before, after = _lit(f'{pworld}@{_moment(pms)}'), _lit(f'{world}@{_moment(ms)}')
        if self._new_verbs():
            # one statement; a row changed since the commit fails it, as git
            # and Dolt fail a revert on a conflict
            self._on(Ref(ref.branch))
            try:
                self._execute(f"UNDO DIFF {before} TO {after} EXCEPT TABLES ({LOG_TABLE})")
            except psycopg2.Error as e:
                if _sqlstate(e) == "40001":
                    raise RuntimeError(f"revert conflict; nothing reverted: {str(e).strip()}") from None
                raise
            return self._commit_impl(Ref(ref.branch), f"Revert {commit}")
        stmts = self._execute(f"DIFF WORLD {after} TO {before} AS SQL") or []
        stmts = [s[0] for s in stmts if not _touches_bookkeeping(s[0])]
        self._on(Ref(ref.branch))
        self._execute("BEGIN")
        try:
            for s in stmts:
                self._execute(s)
            self._execute("COMMIT")
        except BaseException:
            try:
                self._execute("ROLLBACK")
            except Exception:
                pass
            raise
        return self._commit_impl(Ref(ref.branch), f"Revert {commit}")

    def _reset_impl(self, ref: Ref, to: str) -> None:
        self._home()
        world = self._world(ref.branch)
        cworld, ms, _ = self._commit(to)
        if cworld != world:
            raise ValueError(f"commit {to} is on {cworld}, not {world}")
        self._current_ref = None
        self._execute(f"RESTORE WORLD {_q(world)} TO {_lit(_moment(ms))}")
        with _REG.lock:
            # the log rows after the commit are gone with the restore
            _REG.last_commit[world] = to

    def _drop(self, world: str) -> bool:
        """DROP WORLD; False when it still has forks."""
        try:
            self._execute(f"DROP WORLD {_q(world)}")
            return True
        except psycopg2.Error as e:
            if _sqlstate(e) == "2BP01":
                return False
            raise

    def _delete_impl(self, ref: Ref) -> None:
        self._home()
        world = self._world(ref.branch)
        if world == self.db_name:
            raise ValueError("cannot delete the run's main branch")
        with _REG.lock:
            merged = world in _REG.merged_into
        if self._current_ref and self._current_ref.branch == ref.branch:
            self._on(Ref(MAIN_BRANCH))
        if merged:
            self.observe("delete_after_merge")
            return
        if self._drop(world):
            self._retry_deferred()
            return
        with _REG.lock:
            _REG.deferred.add(world)
        self.observe("deferred_deletes")

    def _retry_deferred(self) -> None:
        with _REG.lock:
            pending = sorted(_REG.deferred, key=len, reverse=True)
        for w in pending:
            try:
                if self._drop(w):
                    with _REG.lock:
                        _REG.deferred.discard(w)
            except psycopg2.Error as e:
                if _sqlstate(e) == "3D000":
                    with _REG.lock:
                        _REG.deferred.discard(w)

    def list_branches(self) -> list:
        prefix = f"{self.db_name}{SEP}"
        rows = self._execute("SHOW WORLDS") or []
        out = [MAIN_BRANCH] if any(r[0] == self.db_name for r in rows) else []
        out += [r[0][len(prefix):] for r in rows if str(r[0]).startswith(prefix)]
        return out

    def close_connection(self) -> None:
        for past in self._past.values():
            try:
                past.close()
            except Exception:
                pass
        self._past.clear()
        self.conn = self._head
        if self.conn is not None and not self.conn.closed:
            try:
                self._retry_deferred()
            except Exception:
                pass
        with self._side_lock:
            if self._side is not None:
                try:
                    self._side.close()
                except Exception:
                    pass
                self._side = None
        super().close_connection()

    # ------------------------------------------------------------------
    # Async (exec_async): a psycopg pool on the run's world; each pool
    # connection remembers the world it switched to.
    # ------------------------------------------------------------------

    async def open_async_pool(self, size: int) -> None:
        import psycopg
        from psycopg_pool import AsyncConnectionPool

        self.async_pool = AsyncConnectionPool(
            _uri(self.db_name), min_size=size, max_size=size,
            kwargs={"autocommit": True, "cursor_factory": psycopg.AsyncClientCursor},
            open=False,
        )
        await self.async_pool.open(wait=True)

    async def _connect_impl_async(self, conn, ref: Ref):
        target = str(ref)
        if getattr(conn, "_bb_world", None) != target:
            if ref.commit:
                await conn.execute(f"USE WORLD {self._at(ref)}")
            else:
                await conn.execute(f"SWITCH WORLD {_q(self._world(ref.branch))}")
            conn._bb_world = target
        return conn

    # ------------------------------------------------------------------
    # Run-level helpers (harness)
    # ------------------------------------------------------------------

    class ChronosDBInfo:
        """Run-level state: the run's database, a world with no parent,
        created fresh (with the bookkeeping log table)."""

        def __init__(self, db_name: str):
            self.db_name = db_name
            self.branches = set()
            self.branches_lock = threading.Lock()
            setup_database(db_name)

    @classmethod
    def cleanup(cls, info) -> None:
        if info is not None:
            n = drop_database(info.db_name)
            print(f"Database '{info.db_name}' deleted ({n} worlds dropped).")


def _json_obj(v):
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    import json
    return json.loads(v)


def _col_type(c: dict) -> str:
    """A column of a stored table schema (DIFF's before/after) as a type
    name with its size."""
    ty = str(c.get("type") or "?")
    chars, numeric = c.get("chars"), c.get("numeric")
    if chars:
        n, fixed = chars
        return f"character({n})" if fixed else ("character varying" if n >= 2**32 - 1 else f"character varying({n})")
    if numeric:
        return f"numeric({numeric[0]},{numeric[1]})"
    if c.get("small"):
        return "smallint"
    if c.get("dim") is not None and ty == "vector":
        return f"vector({c['dim']})"
    return ty


def _diff_type(base, side) -> str:
    if base is None:
        return "added"
    if side is None:
        return "removed"
    return "modified"


_BOOKKEEPING = re.compile(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:"?public"?\.)?"?_bb_', re.I)


def _touches_bookkeeping(stmt: str) -> bool:
    return bool(_BOOKKEEPING.match(stmt or ""))


def drop_database(db_name: str) -> int:
    """Drop the run's world, its forks and every other world of the run."""
    conn = _connect("main")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW WORLDS")
            names = [r[0] for r in cur.fetchall()]
            mine = [n for n in names if n == db_name or str(n).startswith(f"{db_name}{SEP}")]
            dropped = 0
            for n in sorted(mine, key=len, reverse=True):
                try:
                    cur.execute(f"DROP WORLD IF EXISTS {_q(n)} CASCADE")
                    dropped += 1
                except psycopg2.Error as e:
                    print(f"Warning: could not drop world {n}: {e}")
        with _REG.lock:
            for d in (_REG.commits, ):
                for k in [k for k, v in d.items() if v[0] in mine]:
                    del d[k]
            for d in (_REG.last_commit, _REG.forked_at, _REG.merged_into, _REG.parents):
                for k in [k for k in d if k in mine]:
                    del d[k]
            _REG.deferred -= set(mine)
        return dropped
    finally:
        conn.close()


def setup_database(db_name: str) -> None:
    """A fresh run database: a parentless world with the commit log table,
    and history kept long enough for the run's commits."""
    drop_database(db_name)
    conn = _connect("main")
    try:
        with conn.cursor() as cur:
            if HISTORY_RETENTION:
                cur.execute(f"ALTER SYSTEM SET history_retention = {_lit(HISTORY_RETENTION)}")
            cur.execute(f"CREATE DATABASE {_q(db_name)}")
    finally:
        conn.close()
    conn = _connect(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {LOG_TABLE} (id text PRIMARY KEY, world text NOT NULL, "
                        f"at_ms bigint NOT NULL, message text)")
    finally:
        conn.close()
    with _REG.lock:
        _REG.parents[db_name] = None


def server_up() -> bool:
    try:
        _connect("main").close()
        return True
    except Exception:
        return False
