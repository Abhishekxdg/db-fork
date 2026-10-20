"""The microbench verb operations (commit, diff, log, merge, rebase, revert,
reset) against the in-memory fake backend: each records its own verb row
and nothing else, and an unsupported verb is recorded without failing."""

import itertools

import pytest

from dblib import result_pb2 as rslt
from dblib.db_api import OpStatus
from dblib.result_collector import ResultCollector
from microbench.operations import (
    BranchCommitOperation,
    BranchDiffOperation,
    BranchLogOperation,
    BranchMergeOperation,
    BranchRebaseOperation,
    BranchRevertOperation,
    BranchResetOperation,
)
from tests.fake_backend import FakeSuite


class Context:
    """The parts of WorkerContext the verb operations use."""

    thread_id = 0
    measure_storage = False

    def __init__(self, suite):
        self.db_tools = suite
        self._modified_keys = {}
        self._ids = itertools.count(1)
        self._keys = itertools.count(100)

    @property
    def current_ref(self):
        return self.db_tools.current_ref

    @property
    def current_branch(self):
        return self.current_ref.branch

    def get_next_branch_id(self):
        return next(self._ids)

    def connect(self, branch, timed=True):
        self.db_tools.exec([], refs=[branch], timed=timed).pop().raise_for_status()

    def _insert_without_timing(self, table_name):
        key = next(self._keys)
        self.db_tools.exec(
            [(f"INSERT INTO {table_name} VALUES (?, ?)", (key, "x"))],
            refs=[self.current_ref], timed=False,
        )[0].raise_for_status()
        self._modified_keys.setdefault(self.current_branch, []).append((key,))

    def count(self, table_name):
        return self.db_tools.exec(
            [f"SELECT COUNT(*) FROM {table_name}"], refs=[self.current_ref],
            timed=False,
        )[0].rows[0][0]


@pytest.fixture
def ctx(tmp_path):
    suite = FakeSuite(ResultCollector(run_id="t", output_dir=str(tmp_path)))
    suite.exec(["CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
                ("INSERT INTO t VALUES (?, ?)", (1, "a"))], timed=False)
    return Context(suite)


def recorded(ctx):
    return [(r.op_type, r.status) for r in ctx.db_tools.result_collector.results]


@pytest.mark.parametrize("op_class, op_type", [
    (BranchCommitOperation, rslt.OpType.COMMIT),
    (BranchDiffOperation, rslt.OpType.DIFF),
    (BranchLogOperation, rslt.OpType.LOG),
    (BranchResetOperation, rslt.OpType.RESET),
])
def test_supported_verb_records_one_row_per_op(ctx, op_class, op_type):
    op = op_class(table_name="t", changes_per_op=2)
    for _ in range(3):
        op.execute(ctx)
    assert recorded(ctx) == [(op_type, OpStatus.OK)] * 3
    assert ctx.current_branch == "main"
    assert op.get_operation_type() == op_type


@pytest.mark.parametrize("op_class, op_type", [
    (BranchMergeOperation, rslt.OpType.MERGE),
    (BranchRebaseOperation, rslt.OpType.REBASE),
    (BranchRevertOperation, rslt.OpType.REVERT),
])
def test_unsupported_verb_is_recorded_not_raised(ctx, op_class, op_type):
    op = op_class(table_name="t")
    op.execute(ctx)
    op.execute(ctx)
    assert recorded(ctx) == [(op_type, OpStatus.UNSUPPORTED)] * 2
    assert ctx.count("t") == 1  # nothing was prepared


def test_reset_discards_the_last_commit_only(ctx):
    op = BranchResetOperation(table_name="t", changes_per_op=2)
    op.execute(ctx)
    # seed row + the reset target's two rows; the discarded two are gone
    assert ctx.count("t") == 3
    assert len(ctx._modified_keys["main"]) == 2


def test_diff_reuses_its_child_and_log_its_history(ctx):
    diff = BranchDiffOperation(table_name="t", changes_per_op=1)
    diff.execute(ctx)
    diff.execute(ctx)
    assert len([b for b in ctx.db_tools.list_branches() if b.startswith("op_")]) == 1

    log = BranchLogOperation(table_name="t", history_commits=4, log_limit=3)
    log.execute(ctx)
    before = ctx.count("t")
    result = ctx.db_tools.log(ctx.current_ref, 3, timed=False)
    assert len(result.value) == 3
    log.execute(ctx)
    assert ctx.count("t") == before
