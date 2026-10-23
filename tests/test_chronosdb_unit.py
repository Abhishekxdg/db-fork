"""chronosdb backend: the parts that need no server (conflict sides, the
schema half of diff, names, moments)."""

import json

from dblib import chronosdb
from dblib.chronosdb import ChronosDBToolSuite as S
from dblib.db_api import Ref


def test_capabilities_and_implementation():
    caps = S.capabilities()
    assert all(caps[v] for v in ("branch", "commit", "diff", "log", "merge", "rebase", "revert",
                                 "reset", "delete", "commit_refs", "exec_async"))
    assert caps["multi_ref_exec"] is False
    impl = S.implementation()
    assert impl["merge"] == impl["rebase"] == impl["reset"] == "native"
    assert impl["revert"] == "composed" and impl["log"] == "simulated"
    assert set(S.IMPLEMENTATION_NOTES) >= {"branch", "commit", "diff", "log", "merge", "rebase",
                                           "revert", "reset", "delete", "commit_refs"}


def test_using_maps_bench_sides_to_chronos_sides():
    # merge of a child into its parent: bench "ours" (the target) is Chronos's THEIRS
    assert S._using("ours", False) == "USING THEIRS"
    assert S._using("theirs", False) == "USING OURS"
    # merging a parent into its child (a rebase): bench "ours" is the child, Chronos's OURS
    assert S._using("ours", True) == "USING OURS"
    assert S._using("theirs", True) == "USING THEIRS"


def test_moment_is_utc_milliseconds():
    assert chronosdb._moment(0) == "1970-01-01 00:00:00.000+00"
    assert chronosdb._moment(1792791957808) == "2026-10-23 21:45:57.808+00"


def test_col_type_reads_sizes():
    t = chronosdb._col_type
    assert t({"type": "integer"}) == "integer"
    assert t({"type": "integer", "small": True}) == "smallint"
    assert t({"type": "text", "chars": [10, False]}) == "character varying(10)"
    assert t({"type": "text", "chars": [4294967295, False]}) == "character varying"
    assert t({"type": "text", "chars": [3, True]}) == "character(3)"
    assert t({"type": "numeric", "numeric": [12, 2]}) == "numeric(12,2)"


class _Fake(S):
    """A suite whose statements return canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.db_name, self.default_branch = "db", "main"
        self.conn = self._head = None

    def _execute(self, query, vars=None):
        return self.rows


def _schema(*cols):
    return json.dumps({"columns": [dict(id=i, name=n, type=t, **kw) for i, (n, t, kw) in enumerate(cols)]})


def test_schema_half_from_diff_rows():
    before = _schema(("id", "integer", {}), ("n", "integer", {}), ("b", "text", {}))
    after = _schema(("id", "integer", {}), ("n", "numeric", {"numeric": [10, 2]}))
    after = json.loads(after)
    after["columns"].append({"id": 7, "name": "extra", "type": "integer"})
    rows = [
        ("t", None, "alter table", before, json.dumps(after), '["+extra integer", "-b"]'),
        ("z", None, "create table", None, "{}", "[]"),
        ("old", None, "drop table", "{}", None, "[]"),
        ("_bb_commits", None, "create table", None, "{}", "[]"),
        ("t", "1", "update", "{}", "{}", '["n"]'),
    ]
    out = _Fake(rows)._schema_diff_impl(Ref("a"), Ref("b"))
    assert out["tables_added"] == ["z"] and out["tables_dropped"] == ["old"]
    assert out["columns_added"] == {"t": ["extra"]} and out["columns_dropped"] == {"t": ["b"]}
    assert out["columns_changed"] == {"t": [["n", "integer", "numeric(10,2)"]]}
    assert out["changed"] is True


def test_row_half_counts_and_skips_bookkeeping():
    rows = [("t", "1", "insert", None, "{}", "[]"), ("t", "2", "update", "{}", "{}", "[]"),
            ("u", "3", "delete", "{}", None, "[]"), ("_bb_commits", "c1", "insert", None, "{}", "[]"),
            ("t", None, "alter table", "{}", "{}", "[]")]
    out = _Fake(rows)._diff_impl(Ref("a"), Ref("b"))
    assert (out["rows_added"], out["rows_modified"], out["rows_deleted"]) == (1, 1, 1)
    assert sorted(t["table_name"] for t in out["tables"]) == ["t", "u"]


def test_dry_run_conflicts_in_dolt_shape():
    rows = [
        ("t", "1", "conflict", "both changed v", {"id": 1, "v": "base"}, {"id": 1, "v": "world"},
         {"id": 1, "v": "parent"}, None, None, {}),
        ("t", "2", "conflict", "", {"id": 2, "v": "x"}, None, {"id": 2, "v": "p"}, None, None, {}),
        ("t", "3", "apply", "", None, {"id": 3}, None, {"id": 3}, None, {}),
        (None, None, "summary", "", None, None, None, None, None, None),
    ]
    # a child merged into its parent: bench ours = the parent (Chronos theirs)
    [c] = _Fake(rows)._dry_run_conflicts("db__a", None, bench_ours_is_world=False)
    assert c["table"] == "t" and len(c["rows"]) == 2
    r = c["rows"][0]
    assert (r["base_v"], r["our_v"], r["their_v"]) == ("base", "parent", "world")
    assert r["our_diff_type"] == r["their_diff_type"] == "modified"
    assert c["rows"][1]["their_diff_type"] == "removed" and c["rows"][1]["our_diff_type"] == "modified"
    # the other way round: bench ours = the world
    [c] = _Fake(rows)._dry_run_conflicts("db__a", None, bench_ours_is_world=True)
    assert (c["rows"][0]["our_v"], c["rows"][0]["their_v"]) == ("world", "parent")


def test_bookkeeping_statements_are_left_out_of_revert():
    assert chronosdb._touches_bookkeeping("insert into _bb_commits (id) values ('c1');")
    assert chronosdb._touches_bookkeeping('DELETE FROM public."_bb_commits" WHERE id = 1')
    assert not chronosdb._touches_bookkeeping("update t set v = 1 where id = 2;")
