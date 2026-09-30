"""Replay the real leaderboard workload without starting a database server."""

import copy
import json
import random
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from dblib.result_collector import ResultCollector
from leaderboard.run import BOARD, build_chain, run_row, task_config
from leaderboard.test_leaderboard import ITEM_DDL
from microbench import task2_pb2 as tp
from microbench.runner2 import BackendInfo, SharedBranchManager, WorkerContext, register_all_operations


class MemoryDatabase:
    """Only emulate branch/key state; SQL and arguments come from real operations."""

    autocommit = True

    def __init__(self, collector, fail=None):
        self.result_collector = collector
        self.branch = "main"
        self.branches = {"main": set(range(1, 1001))}
        self.calls = {}
        self.fail = fail

    def record(self, timed):
        if timed:
            self.result_collector.results.append(SimpleNamespace(latency=0.01))

    def create_branch(self, name, parent_id=None, timed=False):
        self.branches[name] = self.branches[parent_id or self.branch].copy()
        self.record(timed)

    def connect_branch(self, name, timed=False):
        assert name in self.branches
        self.branch = name
        self.record(timed)

    def delete_branch(self, name, timed=False):
        assert name != self.branch
        del self.branches[name]
        self.record(timed)

    def get_current_branch(self):
        return self.branch, self.branch

    def list_branches(self):
        return list(self.branches)

    def get_current_connection(self):
        return self

    def get_table_schema(self, table):
        return ITEM_DDL

    def execute_sql(self, sql, params=None, timed=False):
        # main's adapters wrap every driver error in a plain Exception.
        if timed:
            calls = self.calls.setdefault(self.branch, [])
            calls.append((sql, copy.deepcopy(params)))
            if self.fail == (self.branch, len(calls)):
                self.fail = None
                raise Exception("injected failure")
        if sql.startswith("INSERT"):
            key = params["i_id"]
            if key in self.branches[self.branch]:
                raise Exception("duplicate key")
            self.branches[self.branch].add(key)
        if sql.startswith(("INSERT", "UPDATE")):
            assert self.branch.startswith("row_")
        self.record(timed)
        return [(1,)]


def targets(calls: dict) -> dict:
    """The rows each statement targets, which the seed fixes. main's row generator is unseeded,
    so generated values and insert keys are left out."""
    def target(params):
        if not isinstance(params, dict):
            return params  # a point read's key
        bounds = {k: v for k, v in params.items() if k.startswith(("_start_", "_end_"))}
        return bounds or params["i_id"]  # a range's bounds, or a point update's key
    return {branch: [(sql, target(params)) for sql, params in rows]
            for branch, rows in calls.items() if branch != "row_insert"}


class Workload(unittest.TestCase):
    def setUp(self):
        register_all_operations()
        self.suite = json.loads((BOARD / "suite.json").read_text())
        for name, value in (("get_pk_column_names", ["i_id"]),
                            ("get_all_columns", ["i_id", "i_im_id", "i_name", "i_price", "i_data"])):
            patcher = mock.patch(f"util.db_helpers.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Keys come back in key order. main's query has no ORDER BY, so a real server is assumed to do the same.
        patcher = mock.patch("util.db_helpers.get_pk_values", side_effect=lambda db, *args:
                             [(key,) for key in sorted(db.branches[db.branch])])
        patcher.start()
        self.addCleanup(patcher.stop)
        # main's row generator draws integers from the global random module; pin it so insert counts cannot flake.
        self.addCleanup(random.setstate, random.getstate())
        random.seed(0)

    def replay(self, fail=None, max_batch=None):
        with tempfile.TemporaryDirectory() as output:
            collector = ResultCollector(output_dir=output)
            ctx = WorkerContext(task_config(tp.Backend.DOLT, "test", self.suite),
                                BackendInfo(default_branch_name="main"), 0, self.suite["seed"],
                                collector, SharedBranchManager(), None, [])
            db = ctx.db_tools = MemoryDatabase(collector, fail)
            chain = build_chain(ctx, self.suite["chain_length"], "main")
            result = [run_row(ctx, row, chain, self.suite, max_batch) for row in self.suite["rows"]]
            self.assertEqual(db.branches[chain[-1][0]], set(range(1, 1001)))
            self.assertEqual(set(db.branches), {"main", *(name for name, _ in chain)})
            return result, db.calls

    def row_index(self, operation):
        return next(i for i, row in enumerate(self.suite["rows"]) if row["op"] == operation)

    def test_full_suite_fills_every_cell_and_repeats_its_targets(self):
        result, calls = self.replay()
        _, repeated = self.replay()
        self.assertEqual([len(row) for row in result], [3] * 8)
        self.assertTrue(all(cell is not None for row in result for cell in row), result)
        self.assertEqual(targets(calls), targets(repeated))

    def test_failed_try_does_not_change_later_tries_or_rows(self):
        _, baseline = self.replay(max_batch=6)
        for operation in ["read", "range_read", "update", "range_update"]:
            with self.subTest(operation=operation):
                name = f"row_{operation}"
                result, calls = self.replay(fail=(name, 3), max_batch=6)
                self.assertIsNone(result[self.row_index(operation)][0])
                self.assertTrue(all(cell is not None for cell in result[self.row_index(operation)][1:]))
                self.assertEqual(len(calls[name]), 3 + 6 * 2)
                failed, clean = targets(calls), targets(baseline)
                self.assertEqual(failed[name][:3], clean[name][:3])
                self.assertEqual(failed[name][3:], clean[name][6:])
                for other in clean.keys() - {name}:
                    self.assertEqual(failed[other], clean[other], other)

    def test_failed_insert_is_retried_inside_its_try(self):
        # main's insert makes up to 5 attempts, so a failure costs one untimed extra statement, not the try.
        result, calls = self.replay(fail=("row_insert", 3), max_batch=6)
        self.assertTrue(all(cell is not None for cell in result[self.row_index("insert")]))
        self.assertEqual(len(calls["row_insert"]), 6 * 3 + 1)


if __name__ == "__main__":
    unittest.main()
