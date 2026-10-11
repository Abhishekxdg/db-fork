"""Replay the real leaderboard workload without starting a database server."""

import copy
import itertools
import json
import random
import tempfile
import unittest
from unittest import mock

from dblib import result_pb2 as rslt
from dblib.db_api import DBToolSuite, Ref, UnsupportedOperation
from dblib.result_collector import ResultCollector
from leaderboard.run import BOARD, batch_total, build_chain, run_row, task_config
from leaderboard.test_leaderboard import ITEM_DDL
from microbench import task2_pb2 as tp
from microbench.datagen import DynamicDataGenerator
from microbench.runner2 import BackendInfo, SharedBranchManager, WorkerContext, register_all_operations


class MemoryDatabase(DBToolSuite):
    """Only emulate branch/key state; SQL and arguments come from real operations."""

    autocommit = True

    def __init__(self, collector, fail=None):
        super().__init__(connection=self, result_collector=collector)
        self.active_branch = "main"
        self._current_ref = Ref("main")
        self.branches = {"main": set(range(1, 1001))}
        self.calls = {}
        self.fail = fail

    def _storage_bytes(self):
        return 0

    def _branch_impl(self, name, from_ref):
        self.branches[name] = self.branches[from_ref.branch].copy()

    def _connect_impl(self, ref):
        assert ref.branch in self.branches
        self.active_branch = ref.branch

    def _delete_impl(self, ref):
        assert ref.branch != self.active_branch
        del self.branches[ref.branch]

    def list_branches(self):
        return list(self.branches)

    def get_table_schema(self, table):
        return ITEM_DDL

    def cursor(self):
        return MemoryCursor(self)


class MemoryCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, params=None):
        db = self.db
        calls = db.calls.setdefault(db.active_branch, [])
        calls.append((sql, copy.deepcopy(params)))
        if db.fail == (db.active_branch, len(calls)):
            db.fail = None
            raise Exception("injected failure")
        if sql.startswith("INSERT"):
            key = params["i_id"]
            if key in db.branches[db.active_branch]:
                raise Exception("duplicate key")
            db.branches[db.active_branch].add(key)
        if sql.startswith(("INSERT", "UPDATE")):
            assert db.active_branch.startswith("row_")
        self.description = [("value",)] if sql.startswith("SELECT") else None

    def fetchall(self):
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
                             [(key,) for key in sorted(db.branches[db.active_branch])])
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
            self.assertEqual(db.branches[chain[-1]], set(range(1, 1001)))
            self.assertEqual(set(db.branches), {"main", *chain})
            return result, db.calls

    def row_index(self, operation):
        return next(i for i, row in enumerate(self.suite["rows"]) if row["op"] == operation)

    def test_full_suite_fills_every_cell_and_repeats_its_targets(self):
        keys = itertools.count(1001)
        generate = DynamicDataGenerator.generate_value
        def collision_free(generator, column):
            return next(keys) if column == "i_id" else generate(generator, column)

        with mock.patch.object(DynamicDataGenerator, "generate_value", collision_free):
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

    def test_failed_insert_retry_leaves_the_try_incomplete(self):
        result, calls = self.replay(fail=("row_insert", 3), max_batch=6)
        self.assertIsNone(result[self.row_index("insert")][0])
        self.assertTrue(all(cell is not None for cell in result[self.row_index("insert")][1:]))
        self.assertEqual(len(calls["row_insert"]), 6 * 3 + 1)

    def test_non_ok_branch_results_do_not_become_timings(self):
        branch = MemoryDatabase._branch_impl
        for error in (UnsupportedOperation("branch"), RuntimeError("branch failed")):
            with self.subTest(error=type(error).__name__):
                def fail_measurement(db, name, from_ref):
                    if name.startswith(("ttfq_", "create_")):
                        raise error
                    return branch(db, name, from_ref)

                with mock.patch.object(MemoryDatabase, "_branch_impl", fail_measurement):
                    result, _ = self.replay(max_batch=6)
                self.assertEqual(result[:2], [[None] * 3] * 2)
                self.assertTrue(all(cell is not None for row in result[2:] for cell in row))

    def test_batch_totals_ignore_wrappers_but_reject_non_ok_results(self):
        with tempfile.TemporaryDirectory() as output:
            collector = ResultCollector(output_dir=output)
            collector.emit(rslt.OpType.CONNECT, latency=3)
            collector.emit(rslt.OpType.READ, latency=2)
            collector.emit(rslt.OpType.EXEC, latency=5)
            self.assertEqual(batch_total(collector, 1, rslt.OpType.READ), 2)
            self.assertEqual(batch_total(collector, 1, rslt.OpType.CONNECT), 3)
            self.assertIsNone(batch_total(collector, 2, rslt.OpType.READ))
            for status in (rslt.OpStatus.FAILED, rslt.OpStatus.UNSUPPORTED):
                collector.results[-1].status = status
                self.assertFalse(collector.failed_operations)
                self.assertIsNone(batch_total(collector, 1, rslt.OpType.READ))


if __name__ == "__main__":
    unittest.main()
