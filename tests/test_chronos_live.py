"""Chronos backend against a live PostgreSQL server (skipped when
chronos-core is not installed or nothing listens on
CHRONOS_HOST:CHRONOS_PORT). Exercises every verb through the public API.

Chronos merges or branches schemas, not both: the merge and rebase tests
run with CHRONOS_SCHEMA_BRANCHING=0, the branch-local DDL test with =1
(the default).
"""

import datetime
import socket
from decimal import Decimal

import pytest

from dblib import chronos
from dblib import result_collector as rc

DB = "bb_test_chronos"


def _available() -> bool:
    try:
        import chronos_core  # noqa: F401
    except ImportError:
        return False
    try:
        with socket.create_connection((chronos.CHRONOS_HOST, chronos.CHRONOS_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="no chronos-core or PostgreSQL server")

needs_merge = pytest.mark.skipif(
    chronos.CHRONOS_SCHEMA_BRANCHING, reason="merge needs CHRONOS_SCHEMA_BRANCHING=0")
needs_schema_branching = pytest.mark.skipif(
    not chronos.CHRONOS_SCHEMA_BRANCHING, reason="needs CHRONOS_SCHEMA_BRANCHING=1")


@pytest.fixture
def suite():
    chronos.setup_database(DB)
    s = chronos.ChronosToolSuite.init_for_bench(rc.ResultCollector(), DB)
    res = s.exec(["CREATE TABLE t (id INT NOT NULL, v VARCHAR(20), n INT, PRIMARY KEY (id))",
                  "CREATE TABLE h (a INT, b VARCHAR(10))",
                  "INSERT INTO t (id, v, n) VALUES (1,'a',1),(2,'b',2),(3,'c',3)",
                  "INSERT INTO h (a, b) VALUES (1,'x')"], refs=["main"])[0]
    assert res.ok, res.error
    yield s
    s.close_connection()
    chronos.drop_database(DB)


def rows(suite, ref, sql, vars=None):
    res = suite.exec([(sql, vars)], refs=[ref])[0]
    assert res.ok, res.error
    return [tuple(r) for r in res.rows]


def test_statements_and_types(suite):
    res = suite.exec([
        "CREATE TABLE m (w SMALLINT NOT NULL, i INT NOT NULL, amt DECIMAL(8,2), ts TIMESTAMP, "
        "PRIMARY KEY (w, i))",
        ("INSERT INTO m (w, i, amt, ts) VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)",
         (1, 1, Decimal("1.50"), datetime.datetime(2024, 1, 1, 10), 1, 2, 2.5, None)),
    ], refs=["main"])[0]
    assert res.ok, res.error
    assert rows(suite, "main", "SELECT amt, ts FROM m WHERE w = %s AND i = %s", (1, 1)) == [
        (Decimal("1.50"), datetime.datetime(2024, 1, 1, 10))]
    # Unnamed expressions keep their own columns, with numeric types.
    assert rows(suite, "main", "SELECT MIN(i), MAX(i), COUNT(*), SUM(amt) FROM m") == [
        (1, 2, 2, Decimal("4.00"))]
    # BETWEEN in a write, and a literal % next to a parameter.
    assert suite.exec([("UPDATE t SET n = n + 10 WHERE id BETWEEN %s AND %s", (2, 3))],
                      refs=["main"])[0].ok
    assert rows(suite, "main", "SELECT COUNT(*) FROM t WHERE v LIKE 'a%%' AND n < %s", (5,)) == [(1,)]
    assert sorted(rows(suite, "main", "SELECT id, n FROM t")) == [(1, 1), (2, 12), (3, 13)]
    # A table created without a primary key takes rows through its hidden key.
    assert suite.exec(["INSERT INTO h (a, b) VALUES (2,'y'), (2,'y')"], refs=["main"])[0].ok
    assert sorted(rows(suite, "main", "SELECT a, b FROM h")) == [(1, "x"), (2, "y"), (2, "y")]


def test_transaction_commits_and_rolls_back(suite):
    def script(db):
        with db.transaction():
            db.sql("UPDATE t SET n = n + 10 WHERE id = %s", (1,))
            inside = db.sql("SELECT n FROM t WHERE id = 1")
        with pytest.raises(ValueError):
            with db.transaction():
                db.sql("UPDATE t SET n = 999 WHERE id = 1")
                raise ValueError("abort")
        return inside, db.sql("SELECT n FROM t WHERE id = 1")
    res = suite.exec(script, refs=["main"])[0]
    assert res.ok, res.error
    assert res.value == ([(11,)], [(11,)])


def test_commit_log_diff_reset_revert(suite):
    c1 = suite.commit("main", "one")
    assert c1.ok and c1.value
    suite.exec(["UPDATE t SET v='a2' WHERE id=1", "INSERT INTO t (id, v, n) VALUES (4,'d',4)",
                "DELETE FROM t WHERE id=3"], refs=["main"])
    c2 = suite.commit("main", "two")
    assert c2.ok
    log = suite.log("main", limit=10)
    assert log.ok and [e["message"] for e in log.value][:2] == ["two", "one"]

    d = suite.diff(f"main@{c1.value}", f"main@{c2.value}")
    assert d.ok, d.error
    assert (d.value["rows_added"], d.value["rows_deleted"], d.value["rows_modified"]) == (1, 1, 1)

    r = suite.revert("main", c2.value)
    assert r.ok, r.error
    assert sorted(rows(suite, "main", "SELECT id, v FROM t")) == [(1, "a"), (2, "b"), (3, "c")]

    suite.exec(["UPDATE t SET v='zz'"], refs=["main"])
    rs = suite.reset("main", c2.value)
    assert rs.ok, rs.error
    assert sorted(rows(suite, "main", "SELECT id, v FROM t")) == [(1, "a2"), (2, "b"), (4, "d")]
    assert [e["message"] for e in suite.log("main").value][0] == "two"


def test_branch_isolation_commit_refs_and_delete(suite):
    c1 = suite.commit("main", "base").value
    suite.exec(["UPDATE t SET v='later' WHERE id=1"], refs=["main"])
    suite.commit("main", "later")
    # A commit ref is a read-only checkpoint session.
    assert rows(suite, f"main@{c1}", "SELECT v FROM t WHERE id=1") == [("a",)]
    assert suite.exec(["UPDATE t SET v='x' WHERE id=1"], refs=[f"main@{c1}"])[0].failed
    assert suite.branch("old", from_ref=f"main@{c1}").ok
    assert suite.branch("new", from_ref="main").ok
    suite.exec(["UPDATE t SET v='branch' WHERE id=2"], refs=["new"])
    assert rows(suite, "old", "SELECT v FROM t WHERE id=1") == [("a",)]
    assert rows(suite, "new", "SELECT v FROM t WHERE id IN (1, 2) ORDER BY id") == [("later",), ("branch",)]
    assert rows(suite, "main", "SELECT v FROM t WHERE id=2") == [("b",)]
    # The fork's log is its parent's up to the fork point.
    assert [e["message"] for e in suite.log("old").value] == ["base"]
    assert [e["message"] for e in suite.log("new").value] == ["later", "base"]
    assert {"main", "old", "new"} <= set(suite.list_branches())
    # Multi-branch queries are not something a Chronos session can express.
    multi = suite.exec(["SELECT 1"], refs=["main", "old"], mode="multi")[0]
    assert multi.unsupported
    assert suite.delete("old").ok and suite.delete("new").ok
    assert "old" not in suite.list_branches()
    assert suite.delete("main").failed   # Chronos keeps its root branch


@needs_schema_branching
def test_branch_local_ddl(suite):
    assert suite.branch("feat", "main").ok
    res = suite.exec(["ALTER TABLE t ADD COLUMN extra INT",
                      "CREATE TABLE scratch (id INT NOT NULL, v INT, PRIMARY KEY (id))",
                      "INSERT INTO scratch (id, v) VALUES (1, 7)",
                      "CREATE INDEX idx_t_v ON t (v)",
                      "UPDATE t SET extra = 5 WHERE id = 1"], refs=["feat"])[0]
    assert res.ok, res.error
    assert rows(suite, "feat", "SELECT extra FROM t WHERE id = 1") == [(5,)]
    assert rows(suite, "feat", "SELECT v FROM scratch") == [(7,)]
    # Neither the column nor the table exists on the parent.
    assert suite.exec(["SELECT extra FROM t"], refs=["main"])[0].failed
    assert suite.exec(["SELECT v FROM scratch"], refs=["main"])[0].failed
    # Merge is what this mode gives up.
    assert not suite.supports("merge")
    assert suite.merge("main", "feat").unsupported


@needs_merge
def test_merge_and_conflicts(suite):
    suite.commit("main", "base")
    assert suite.branch("feat", "main").ok
    # Branch: modify 1, add 10, delete 3, add a row to the keyless table.
    suite.exec(["UPDATE t SET v='feat' WHERE id=1", "INSERT INTO t (id, v, n) VALUES (10,'ten',10)",
                "DELETE FROM t WHERE id=3", "INSERT INTO h (a, b) VALUES (2,'y')"], refs=["feat"])
    suite.commit("feat", "feature work SENTINEL")
    # Spine meanwhile: modify 2 (no conflict), modify 1 (conflict), add 20.
    suite.exec(["UPDATE t SET v='spine' WHERE id=2", "UPDATE t SET n=99 WHERE id=1",
                "INSERT INTO t (id, v, n) VALUES (20,'twenty',20)"], refs=["main"])
    suite.commit("main", "spine work")

    m = suite.merge("main", "feat", message="merge feat", on_conflict="theirs")
    assert m.ok, m.error
    assert m.value["conflicts"] == 1 and m.value["conflict_tables"] == ["t"]
    got = {r[0]: r[1:] for r in rows(suite, "main", "SELECT id, v, n FROM t")}
    assert got == {1: ("feat", 1), 2: ("spine", 2), 10: ("ten", 10), 20: ("twenty", 20)}
    assert sorted(rows(suite, "main", "SELECT a FROM h")) == [(1,), (2,)]
    msgs = [e["message"] for e in suite.log("main", limit=10).value]
    assert msgs[0] == "merge feat" and any("SENTINEL" in x for x in msgs)

    # No conflict: the native one-pass merge.
    assert suite.branch("clean", "main").ok
    suite.exec(["INSERT INTO t (id, v, n) VALUES (30,'thirty',30)"], refs=["clean"])
    m2 = suite.merge("main", "clean")
    assert m2.ok and m2.value["conflicts"] == 0 and m2.value["applied"] == 1
    # "ours" keeps the target's version of a row both sides changed.
    assert suite.branch("loser", "main").ok
    suite.exec(["UPDATE t SET v='loser' WHERE id=2"], refs=["loser"])
    suite.exec(["UPDATE t SET v='kept' WHERE id=2"], refs=["main"])
    m3 = suite.merge("main", "loser", on_conflict="ours")
    assert m3.ok and m3.value["conflicts"] == 1
    assert rows(suite, "main", "SELECT v FROM t WHERE id=2") == [("kept",)]


@needs_merge
def test_rebase_with_callable_resolver(suite):
    assert suite.branch("dev", "main").ok
    suite.exec(["UPDATE t SET n = n + 5 WHERE id=1"], refs=["dev"])
    suite.exec(["UPDATE t SET n = n + 7 WHERE id=1", "INSERT INTO t (id, v, n) VALUES (40,'f',40)",
                "DELETE FROM t WHERE id=2"], refs=["main"])

    seen = {}

    def combine(db, conflicts):
        for cf in conflicts:
            seen[cf["table"]] = cf["rows"]
            for row in cf["rows"]:
                # Chronos reports the two sides, not the base.
                db.sql("UPDATE t SET n = %s WHERE id = %s",
                       (row["our_n"] + row["their_n"], row["our_id"]))
            db.resolve(cf["table"])

    rb = suite.rebase("dev", "main", on_conflict=combine)
    assert rb.ok, rb.error
    assert rb.value["conflicts"] == 1 and seen["t"][0]["their_diff_type"] == "modified"
    got = {r[0]: r[1] for r in rows(suite, "dev", "SELECT id, n FROM t")}
    assert got == {1: 14, 3: 3, 40: 40}   # 6 + 8, the spine's delete and insert applied


@needs_merge
def test_merge_of_a_branch_from_a_commit_is_unsupported(suite):
    c1 = suite.commit("main", "base").value
    assert suite.branch("old", from_ref=f"main@{c1}").ok
    suite.exec(["UPDATE t SET v='old' WHERE id=1"], refs=["old"])
    m = suite.merge("main", "old")
    assert m.unsupported and "fork base" in m.error
