"""chronosdb backend (Chronos DB, not MIT's Chronos) against a live
`chronos serve` (skipped when none answers on CHRONOSDB_HOST:CHRONOSDB_PORT).
Exercises every verb through the public API."""

import pytest

from dblib import result_collector as rc
from dblib import chronosdb

DB = "bb_test_chronosdb"
MAIN = chronosdb.MAIN_BRANCH

pytestmark = pytest.mark.skipif(not chronosdb.server_up(), reason="no chronos serve")


@pytest.fixture
def suite():
    chronosdb.setup_database(DB)
    s = chronosdb.ChronosDBToolSuite.init_for_bench(rc.ResultCollector(), DB, MAIN)
    s.exec(["CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20), n INT)",
            "CREATE TABLE h (a INT, b VARCHAR(10))",
            "INSERT INTO t VALUES (1,'a',1),(2,'b',2),(3,'c',3)",
            "INSERT INTO h VALUES (1,'x')"], refs=[MAIN])
    yield s
    s.close_connection()
    chronosdb.drop_database(DB)


def rows(suite, ref, sql):
    return sorted(tuple(r) for r in suite.exec([sql], refs=[ref])[0].rows)


def test_implementation_summary():
    impl = chronosdb.ChronosDBToolSuite.implementation()
    for verb in ("branch", "diff", "merge", "rebase", "reset", "delete", "commit_refs"):
        assert impl[verb] == "native", verb
    assert impl["commit"] == "composed" and impl["revert"] == "composed"
    assert impl["log"] == "simulated" and impl["multi_ref_exec"] == "unsupported"


def test_commit_log_diff_reset_revert(suite):
    c1 = suite.commit(MAIN, "one")
    assert c1.ok and c1.value, c1.error
    suite.exec(["UPDATE t SET v='a2' WHERE id=1", "INSERT INTO t VALUES (4,'d',4)",
                "DELETE FROM t WHERE id=3"], refs=[MAIN])
    c2 = suite.commit(MAIN, "two")
    assert c2.ok, c2.error
    log = suite.log(MAIN, limit=10)
    assert log.ok and [e["message"] for e in log.value][:2] == ["two", "one"], log.error

    d = suite.diff(f"{MAIN}@{c1.value}", f"{MAIN}@{c2.value}")
    assert d.ok, d.error
    assert (d.value["rows_added"], d.value["rows_deleted"], d.value["rows_modified"]) == (1, 1, 1)
    assert d.value["schema"]["changed"] is False

    r = suite.revert(MAIN, c2.value)
    assert r.ok, r.error
    assert rows(suite, MAIN, "SELECT id, v FROM t") == [(1, "a"), (2, "b"), (3, "c")]
    assert suite.log(MAIN, limit=1).value[0]["message"].startswith("Revert ")

    suite.exec(["UPDATE t SET v='zz'"], refs=[MAIN])
    rs = suite.reset(MAIN, c2.value)
    assert rs.ok, rs.error
    assert rows(suite, MAIN, "SELECT id, v FROM t") == [(1, "a2"), (2, "b"), (4, "d")]
    assert [e["message"] for e in suite.log(MAIN).value][0] == "two"


def test_commit_ref_is_read_only_and_branch_from_it_writable(suite):
    c1 = suite.commit(MAIN, "base").value
    suite.exec(["UPDATE t SET v='later' WHERE id=1"], refs=[MAIN])
    r = suite.exec(["UPDATE t SET v='nope' WHERE id=1"], refs=[f"{MAIN}@{c1}"])[0]
    assert r.failed and ("25006" in r.error or "read" in r.error.lower()), r.error
    assert rows(suite, f"{MAIN}@{c1}", "SELECT v FROM t WHERE id=1") == [("a",)]
    assert suite.branch("child", from_ref=f"{MAIN}@{c1}").ok
    assert suite.exec(["UPDATE t SET v='yes' WHERE id=1"], refs=["child"])[0].ok
    assert rows(suite, "child", "SELECT v FROM t WHERE id=1") == [("yes",)]
    assert rows(suite, MAIN, "SELECT v FROM t WHERE id=1") == [("later",)]


def test_merge_then_delete_is_noop(suite):
    assert suite.branch("f", from_ref=MAIN).ok
    suite.exec(["INSERT INTO t VALUES (10,'f',10)", "UPDATE t SET n = 20 WHERE id = 2"], refs=["f"])
    m = suite.merge(MAIN, "f")
    assert m.ok, m.error
    assert m.value["conflicts"] == 0 and m.value["fast_forward"] is False
    assert rows(suite, MAIN, "SELECT id, n FROM t WHERE id IN (2, 10)") == [(2, 20), (10, 10)]
    assert suite.delete("f").ok
    assert "f" not in suite.list_branches()


def test_merge_conflicts_ours_theirs_and_callable(suite):
    suite.exec(["UPDATE t SET v='base' WHERE id=1"], refs=[MAIN])
    for name in ("a", "b", "c"):
        assert suite.branch(name, from_ref=MAIN).ok
        suite.exec([f"UPDATE t SET v='{name}' WHERE id=1"], refs=[name])
    suite.exec(["UPDATE t SET v='main' WHERE id=1"], refs=[MAIN])

    m = suite.merge(MAIN, "a", on_conflict="ours")        # keep the target's row
    assert m.ok and m.value["conflicts"] == 1, (m.error, m.value)
    assert rows(suite, MAIN, "SELECT v FROM t WHERE id=1") == [("main",)]

    m = suite.merge(MAIN, "b", on_conflict="theirs")      # take the source's row
    assert m.ok and m.value["conflicts"] == 1, (m.error, m.value)
    assert rows(suite, MAIN, "SELECT v FROM t WHERE id=1") == [("b",)]

    seen = []

    def resolve(db, conflicts):
        seen.extend(conflicts)
        for cf in conflicts:
            for row in cf["rows"]:
                db.sql("UPDATE t SET v = %s WHERE id = %s",
                       (row["our_v"] + "+" + row["their_v"], row["our_id"]))
            db.resolve(cf["table"])

    m = suite.merge(MAIN, "c", on_conflict=resolve)
    assert m.ok, m.error
    assert seen and seen[0]["table"] == "t"
    row = seen[0]["rows"][0]
    assert (row["base_v"], row["our_v"], row["their_v"]) == ("base", "b", "c")
    assert row["our_diff_type"] == row["their_diff_type"] == "modified"
    assert rows(suite, MAIN, "SELECT v FROM t WHERE id=1") == [("b+c",)]


def test_rebase_and_merge_parent_into_child(suite):
    assert suite.branch("dev", from_ref=MAIN).ok
    suite.exec(["INSERT INTO t VALUES (20,'dev',0)"], refs=["dev"])
    suite.exec(["INSERT INTO t VALUES (21,'main',0)"], refs=[MAIN])
    rb = suite.rebase("dev", MAIN)
    assert rb.ok, rb.error
    assert rows(suite, "dev", "SELECT id FROM t WHERE id >= 20") == [(20,), (21,)]
    suite.exec(["INSERT INTO t VALUES (22,'main',0)"], refs=[MAIN])
    m = suite.merge("dev", MAIN)                             # the parent into the child
    assert m.ok and m.value.get("as_rebase"), m.error
    assert rows(suite, "dev", "SELECT id FROM t WHERE id >= 20") == [(20,), (21,), (22,)]
    assert rows(suite, MAIN, "SELECT id FROM t WHERE id >= 20") == [(21,), (22,)]


def test_rebase_parent_onto_child_then_merge_is_up_to_date(suite):
    assert suite.branch("alt", from_ref=MAIN).ok
    suite.exec(["INSERT INTO t VALUES (30,'alt',0)"], refs=["alt"])
    suite.exec(["INSERT INTO t VALUES (31,'main',0)"], refs=[MAIN])
    rb = suite.rebase(MAIN, "alt", on_conflict="theirs")
    assert rb.ok and rb.value.get("as_merge"), rb.error
    m = suite.merge(MAIN, "alt")
    assert m.ok and m.value.get("up_to_date"), m.error
    assert rows(suite, MAIN, "SELECT id FROM t WHERE id >= 30") == [(30,), (31,)]


def test_schema_diff_and_delete_with_forks_deferred(suite):
    assert suite.branch("p", from_ref=MAIN).ok
    suite.exec(["ALTER TABLE t ADD COLUMN extra INT", "CREATE TABLE z (id INT PRIMARY KEY)",
                "CREATE INDEX t_n ON t (n)", "ALTER TABLE t ALTER COLUMN n TYPE BIGINT",
                "ALTER TABLE h DROP COLUMN b"], refs=["p"])
    d = suite.diff(MAIN, "p")
    assert d.ok, d.error
    sch = d.value["schema"]
    assert sch["tables_added"] == ["z"] and sch["columns_added"] == {"t": ["extra"]}, sch
    assert sch["columns_dropped"] == {"h": ["b"]}, sch
    assert sch["columns_changed"] == {"t": [["n", "integer", "bigint"]]}, sch
    assert suite.branch("kid", from_ref="p").ok
    assert suite.delete("p").ok                              # deferred: p has a fork
    assert "p" in suite.list_branches()
    assert suite.delete("kid").ok                            # then p goes too
    assert "p" not in suite.list_branches()
