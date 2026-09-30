"""Replay the real leaderboard workload without starting a database server."""

import asyncio
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from psycopg2.errors import UniqueViolation

from dblib.db_api import SqlError
from dblib.result_collector import ResultCollector
from leaderboard.run import BOARD, build_chain, run_row, task_config
from leaderboard.test_leaderboard import ITEM_DDL
from microbench import task2_pb2 as tp
from microbench.operations.crud import InsertOperation
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
        if timed:
            calls = self.calls.setdefault(self.branch, [])
            calls.append((sql, copy.deepcopy(params)))
            if self.fail == (self.branch, len(calls)):
                self.fail = None
                raise SqlError("injected failure") from UniqueViolation("duplicate key")
        if sql.startswith("INSERT"):
            key = params["i_id"]
            if key in self.branches[self.branch]:
                raise SqlError("duplicate key") from UniqueViolation("duplicate key")
            self.branches[self.branch].add(key)
        if sql.startswith(("INSERT", "UPDATE")):
            assert self.branch.startswith("row_")
        self.record(timed)
        if sql.upper().startswith("SELECT MAX("):
            return [(max(self.branches[self.branch]),)]
        return [(1,)]


class Workload(unittest.TestCase):
    def setUp(self):
        register_all_operations()
        self.suite = json.loads((BOARD / "suite.json").read_text())
        for name, value in (("get_pk_column_names", ["i_id"]),
                            ("get_all_columns", ["i_id", "i_im_id", "i_name", "i_price", "i_data"])):
            patcher = mock.patch(f"util.db_helpers.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("util.db_helpers.get_pk_values", side_effect=lambda db, *args:
                             [(key,) for key in sorted(db.branches[db.branch])])
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_full_suite_repeats_and_inserts_have_no_collisions(self):
        result, calls = self.replay()
        _, repeated = self.replay()
        self.assertEqual(calls, repeated)
        self.assertEqual([len(row) for row in result], [3] * 8)
        self.assertTrue(all(cell is not None for row in result for cell in row), result)
        inserted = [args["i_id"] for sql, args in calls["row_insert"]]
        self.assertEqual(len(inserted), 3000)
        self.assertEqual(len(set(inserted)), 3000)
        self.assertTrue(all(key > 1000 for key in inserted))

    def test_failed_try_does_not_change_later_tries_or_rows(self):
        _, baseline = self.replay(max_batch=6)
        operations = ["read", "range_read", "insert", "update", "range_update"]
        for operation in operations:
            with self.subTest(operation=operation):
                name = f"row_{operation}"
                result, calls = self.replay(fail=(name, 3), max_batch=6)
                row_index = next(i for i, row in enumerate(self.suite["rows"]) if row["op"] == operation)
                self.assertIsNone(result[row_index][0])
                self.assertTrue(all(cell is not None for cell in result[row_index][1:]))
                # A duplicate is a failed benchmark try, not a hidden extra INSERT.
                self.assertEqual(len(calls[name]), 3 + 6 * 2)
                self.assertEqual(calls[name][:3], baseline[name][:3])
                self.assertEqual(calls[name][3:], baseline[name][6:])
                for other in baseline.keys() - {name}:
                    self.assertEqual(calls[other], baseline[other], other)

    def test_explicit_insert_keys_disable_retries_but_legacy_callers_keep_them(self):
        for asynchronous in (False, True):
            for explicit in (False, True):
                with self.subTest(asynchronous=asynchronous, explicit=explicit):
                    context = mock.Mock()
                    context.db_tools.autocommit = True
                    context.get_pk_columns.return_value = ["i_id"]
                    context.generate_row.side_effect = [{"i_id": 7}, {"i_id": 8}]
                    error = SqlError("duplicate key")
                    error.__cause__ = UniqueViolation("duplicate key")
                    execute = mock.AsyncMock(side_effect=[error, None]) if asynchronous else \
                        mock.Mock(side_effect=[error, None])
                    setattr(context.db_tools, "execute_sql_async" if asynchronous else "execute_sql", execute)
                    operation = InsertOperation("item", primary_keys=[(1500,)]) if explicit else InsertOperation("item")

                    def run():
                        if asynchronous:
                            asyncio.run(operation.execute_async(context))
                        else:
                            operation.execute(context)

                    if explicit:
                        with self.assertRaises(SqlError):
                            run()
                        self.assertEqual(execute.call_count, 1)
                        self.assertEqual(execute.call_args.args[1]["i_id"], 1500)
                        context.track_modified_key.assert_not_called()
                    else:
                        run()
                        self.assertEqual(execute.call_count, 2)
                        context.track_modified_key.assert_called_once_with((8,))


if __name__ == "__main__":
    unittest.main()
