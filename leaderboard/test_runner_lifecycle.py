"""Recovery, provenance and deadline checks; no database or vendor account needed."""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psycopg2

from dblib import chronos, dolt as dolt_pg, dolt_mysql, file_copy, matrixone, seekdb
from leaderboard import run as runner
from leaderboard.connector import load_connector


class Recovery(unittest.TestCase):
    def test_scope_mismatch_and_legacy_journals_are_kept(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(runner, "RUNS", Path(folder)):
            server = mock.Mock()
            server.scope.return_value = {"project_id": "B"}
            path = runner.journal("tiger")
            for scope in ({"project_id": "A"}, None):
                path.write_text(json.dumps({"name": "bb_1234abcd", "scope": scope}))
                self.assertIsInstance(runner.clean_up("tiger", server), runner.RunError)
                self.assertTrue(path.exists())
            server.delete.assert_not_called()
            server.scope.return_value = {"project_id": "A"}
            path.write_text(json.dumps({"name": "bb_1234abcd", "scope": server.scope()}))
            self.assertIsNone(runner.clean_up("tiger", server))
            server.delete.assert_called_once_with("bb_1234abcd")
            self.assertFalse(path.exists())

    def test_unlink_failure_is_reported_and_kept(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(runner, "RUNS", Path(folder)):
            server = mock.Mock()
            server.scope.return_value = {"host": "localhost"}
            path = runner.journal("dolt")
            path.write_text(json.dumps({"name": "bb_1234abcd", "scope": server.scope()}))
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("cannot unlink")):
                self.assertIsInstance(runner.clean_up("dolt", server), PermissionError)
            self.assertTrue(path.exists())
            server.exists.return_value = True
            self.assertIsInstance(runner.clean_up("dolt", server), runner.RunError)
            self.assertTrue(path.exists())

    def test_scopes_identify_actual_targets(self):
        for key, module, host in (
            ("dolt", dolt_pg, "DOLT_HOST"),
            ("dolt_mysql", dolt_mysql, "DOLT_MYSQL_HOST"),
            ("seekdb", seekdb, "SEEKDB_HOST"),
            ("matrixone", matrixone, "MO_HOST"),
            ("file_copy", file_copy, "PGSQL_HOST"),
            ("chronos", chronos, "PGSQL_HOST"),
        ):
            server = load_connector(key)
            original = server.scope()
            with mock.patch.object(module, host, "other-host"):
                self.assertNotEqual(server.scope(), original)
            with mock.patch.object(server, "data_dir", Path("/tmp/other-data")):
                self.assertNotEqual(server.scope(), original)

        neon_srv = load_connector("neon")
        with mock.patch.dict(os.environ, {"NEON_API_KEY_ORG": "key-a"}):
            scope_a = neon_srv.scope()
        with mock.patch.dict(os.environ, {"NEON_API_KEY_ORG": "key-b"}):
            scope_b = neon_srv.scope()
        self.assertNotEqual(scope_a, scope_b)
        self.assertNotIn("key-a", json.dumps(scope_a))

        xata_srv = load_connector("xata")
        with mock.patch.dict(os.environ, {"XATA_API_KEY": "key-a", "XATA_ORGANIZATION_ID": "org-a"}):
            x_scope_a = xata_srv.scope()
        with mock.patch.dict(os.environ, {"XATA_API_KEY": "key-b", "XATA_ORGANIZATION_ID": "org-a"}):
            x_scope_b = xata_srv.scope()
        self.assertNotEqual(x_scope_a, x_scope_b)
        self.assertNotIn("key-a", json.dumps(x_scope_a))


class RunLifecycle(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.board = self.root / "leaderboard"
        (self.board / "dolt").mkdir(parents=True)
        self.meta = self.board / "dolt" / "system.json"
        self.meta.write_text(json.dumps({"system": "Doltgres", "hosted": "no", "proprietary": "no",
                                         "tuned": "no", "tags": []}))
        self.dump = self.root / "dump.sql"
        self.dump.write_text("SELECT 1;")
        self.suite = self.board / "suite.json"
        self.suite.write_text(json.dumps({"suite": "test", "seed": 1,
                                          "dataset": {"path": "dump.sql", "sha256": runner.sha256(self.dump)}}))
        (self.root / "uv.lock").write_text("fixture\n")
        (self.root / "build_protos.py").write_text("# fixture\n")
        (self.board / ".gitignore").write_text(".runs/\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.server = mock.Mock(needs_psql=False)
        self.server.scope.return_value = {"host": "fixture"}
        for attr, value in (("ROOT", self.root), ("BOARD", self.board),
                            ("RUNS", self.board / ".runs")):
            patch = mock.patch.object(runner, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        for attr, value in (("start_server", None), ("lifecycle", True), ("measure", ("1", 0, [])),
                            ("load_connector", self.server)):
            patch = mock.patch.object(runner, attr, return_value=value)
            setattr(self, attr, patch.start())
            self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                               "user.email=test@example.invalid", *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def bench(self, **kwargs):
        return runner.run("dolt", "box", self.board, None, kwargs.get("allow_dirty", False),
                          kwargs.get("deadline_min", 1), None)

    def error(self):
        paths = list((self.board / "dolt" / "results").rglob("box.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())["error"]

    def test_success_uses_launch_provenance_and_private_journal(self):
        before = runner.source_snapshot(runner.CODE_PATHS, self.dump)

        def provision(_):
            path = runner.journal("dolt")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["scope"], self.server.scope())

        self.server.provision.side_effect = provision
        payload = json.loads(self.bench().read_text())
        self.assertEqual(payload["commit"], before["commit"])
        self.assertEqual(payload["suite_sha256"], before["files"][str(self.suite)])
        self.assertEqual(payload["lock_sha256"], before["files"][str(self.root / "uv.lock")])
        self.assertFalse(payload["dirty"])
        self.assertFalse(runner.journal("dolt").exists())

    def test_changes_to_dirty_inputs_or_head_cannot_publish_success(self):
        for target in (self.suite, self.meta, self.dump, self.root / "uv.lock", self.root / "build_protos.py"):
            with self.subTest(target=target.name):
                original = target.read_text()
                target.write_text(original + "\n")
                # The dataset must match its suite at launch, unlike other dirty inputs.
                if target == self.dump:
                    target.write_text(original)
                self.server.load.side_effect = lambda *_: target.write_text(target.read_text() + "\n")
                with self.assertRaisesRegex(runner.RunError, "changed during the run"):
                    self.bench(allow_dirty=True)
                self.assertIn("changed during the run", self.error())
                target.write_text(original)
        self.server.load.side_effect = lambda *_: self.git("commit", "--allow-empty", "-qm", "new HEAD")
        with self.assertRaisesRegex(runner.RunError, "changed during the run"):
            self.bench()

    def test_cleanup_and_stop_errors_still_publish(self):
        with mock.patch.object(runner, "clean_up", side_effect=[None, PermissionError("cleanup failed")]):
            self.lifecycle.side_effect = OSError("stop failed")
            with self.assertRaisesRegex(runner.RunError, "cleanup failed"):
                self.bench()
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        self.assertIn("cleanup failed", self.error())

    def test_stop_exception_publishes_an_error(self):
        self.lifecycle.side_effect = OSError("stop failed")
        with self.assertRaisesRegex(runner.RunError, "stop failed"):
            self.bench()
        self.assertIn("stop failed", self.error())

    def test_deadline_covers_startup_and_cleanup_still_runs(self):
        self.start_server.side_effect = lambda *_: time.sleep(2)
        with self.assertRaisesRegex(runner.RunError, "deadline"):
            self.bench(deadline_min=0.001)
        self.server.provision.assert_not_called()
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        self.assertIn("deadline", self.error())

    def test_cleanup_deadline_retains_journal_and_still_stops(self):
        self.server.delete.side_effect = lambda *_: time.sleep(2)
        with mock.patch.object(runner, "VENDOR_TIMEOUT", 0.03), \
                self.assertRaisesRegex(runner.RunError, "cleanup passed"):
            self.bench()
        self.assertTrue(runner.journal("dolt").exists())
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        self.assertIn("cleanup passed", self.error())

    def test_ctrl_c_cleans_up_without_publishing(self):
        self.measure.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.bench()
        self.server.delete.assert_called_once()
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        self.assertFalse((self.board / "dolt" / "results").exists())

    def test_sigterm_cleans_up_without_publishing(self):
        self.measure.side_effect = lambda *_: os.kill(os.getpid(), signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            self.bench()
        self.server.delete.assert_called_once()
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        self.assertFalse((self.board / "dolt" / "results").exists())

    def test_native_only_env_is_rejected_before_start(self):
        with mock.patch.dict(os.environ, {"DBFORK_NATIVE_ONLY": "1"}):
            with self.assertRaisesRegex(runner.RunError, "DBFORK_NATIVE_ONLY"):
                self.bench()
        self.start_server.assert_not_called()

    def test_hosted_timeout_overrides_existing_pgoptions(self):
        meta = json.loads(self.meta.read_text())
        meta["hosted"] = "yes"
        self.meta.write_text(json.dumps(meta))
        self.server.keys = ()
        self.server.region = "test"
        self.server.service = {}
        with mock.patch.dict(os.environ, {"PGOPTIONS": "-c statement_timeout=0 -c application_name=bench"}):
            runner.run("dolt", "box", self.board, None, True, 1, "test location")
            self.assertTrue(os.environ["PGOPTIONS"].endswith(" -c statement_timeout=120s"))
            self.assertIn("application_name=bench", os.environ["PGOPTIONS"])

    def test_verbs_wrapper_runs_microbench_verbs_with_lifecycle(self):
        from leaderboard import verbs
        from microbench.verbs.grid import points_for_run
        self.server.version.return_value = "v1.0"
        self.server.exists.return_value = False
        self.start_server.side_effect = lambda _: (
            runner.RUNS.mkdir(parents=True, exist_ok=True),
            (runner.RUNS / "dolt.pid").write_text(str(os.getpid())),
        )
        expected_pts = [pt.id for pt in points_for_run(1, ["branch", "commit"], None)]

        def fake_verbs_child(cmd, *, cwd, env, timeout=None):
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            (outdir / "micro_verbs_dolt_w1_stats.json").write_text(json.dumps({
                "status": "completed",
                "scale": 1,
                "reps": 10,
                "warmup": 3,
                "native_only": True,
                "points": [
                    {
                        "id": pid,
                        "verb": pid.split(":")[0],
                        "status": "ok",
                        "failed": 0,
                        "latency": {"n": 10, "min": 0.005, "p10": 0.006, "p50": 0.01, "p90": 0.02, "max": 0.03},
                        "reps": [
                            {"timed": True, "status": "OK", "latency": 0.01, "per_delete": [0.003, 0.004]}
                            for _ in range(10)
                        ],
                    }
                    for pid in expected_pts
                ],
            }))
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n")

        with (
            mock.patch.object(verbs, "ROOT", self.root),
            mock.patch.object(verbs.run_macro, "ROOT", self.root),
            mock.patch.object(verbs, "BOARD", self.board),
            mock.patch.object(verbs, "VERB_RUNS", self.board / ".runs" / "verbs"),
            mock.patch.object(verbs, "load_connector", return_value=self.server),
            mock.patch.object(verbs.run_macro, "port_answers", return_value=False),
            mock.patch.object(verbs.run_macro, "pid_alive", return_value=False),
            mock.patch.object(verbs, "launch_verbs_child", side_effect=fake_verbs_child) as proc_run,
        ):
            rc = verbs.run("dolt", 1, "box", self.board, verbs=["branch", "commit"], native_only=True)
        self.assertEqual(rc, 0)
        self.start_server.assert_called_once_with(self.board / "dolt")
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")
        proc_run.assert_called_once()
        written = list((self.board / "dolt" / "verbs").rglob("bb_*.json"))
        self.assertEqual(len(written), 1)
        record = json.loads(written[0].read_text())
        self.assertEqual(record["system"], "Doltgres")
        self.assertEqual(record["scale"], 1)
        self.assertTrue(record["native_only"])
        self.assertEqual(record["outcome"]["status"], "verified")
        self.assertEqual(record["server"], {"started_here": True, "stop": "stopped"})
        # Deeply nested per_delete list is preserved without depth truncation or false secret failure
        self.assertEqual(
            record["stats"]["points"][0]["reps"][0]["per_delete"],
            [0.003, 0.004],
        )
        self.assertEqual(verbs.validate_verbs(written[0], self.board), [])
        entries, excluded = verbs.build_verbs(self.board)
        self.assertEqual((len(entries), len(excluded)), (1, 0))

        # Cohort separation and chronological tie-breaking (started timestamp beats hex run_id)
        second_file = written[0].parent / "bb_00000001.json"
        second_rec = json.loads(written[0].read_text())
        second_rec["run_id"] = "bb_00000001"
        second_rec["started"] = "2099-12-31T23:59:59+00:00"
        second_file.write_text(json.dumps(second_rec))
        entries2, excluded2 = verbs.build_verbs(self.board)
        self.assertEqual((len(entries2), len(excluded2)), (1, 0))
        self.assertEqual(entries2[0]["run_id"], "bb_00000001")

        # Tampered unexpected point or bad quantile is rejected by validate_verbs
        bad_file = written[0].parent / "bb_00000002.json"
        bad_rec = json.loads(written[0].read_text())
        bad_rec["run_id"] = "bb_00000002"
        bad_rec["stats"]["points"].append({"id": "rogue:point", "status": "ok", "failed": 0, "latency": {"n": 10, "p50": 0.01}})
        bad_file.write_text(json.dumps(bad_rec))
        self.assertTrue(any("unexpected point" in p for p in verbs.validate_verbs(bad_file, self.board)))

    def test_launch_verbs_child_timeout_terminates_process_group_cleanly(self):
        from leaderboard import verbs
        import sys
        with self.assertRaises(subprocess.TimeoutExpired):
            verbs.launch_verbs_child(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=self.root,
                env=dict(os.environ),
                timeout=0.1,
            )

    def test_verbs_wrapper_rejects_missing_stats_failed_points_stop_failure_and_ambient_native_only(self):
        from leaderboard import verbs
        self.server.version.return_value = "v1.0"
        self.server.exists.return_value = False
        self.start_server.side_effect = lambda _: (
            runner.RUNS.mkdir(parents=True, exist_ok=True),
            (runner.RUNS / "dolt.pid").write_text(str(os.getpid())),
        )
        with mock.patch.dict(os.environ, {"DBFORK_NATIVE_ONLY": "1"}):
            with self.assertRaisesRegex(runner.RunError, "DBFORK_NATIVE_ONLY"):
                verbs.run("dolt", 1, "box", self.board, verbs=["branch"])
        with (
            mock.patch.object(verbs, "ROOT", self.root),
            mock.patch.object(verbs.run_macro, "ROOT", self.root),
            mock.patch.object(verbs, "BOARD", self.board),
            mock.patch.object(verbs, "VERB_RUNS", self.board / ".runs" / "verbs"),
            mock.patch.object(verbs, "load_connector", return_value=self.server),
            mock.patch.object(verbs.run_macro, "port_answers", return_value=False),
            mock.patch.object(verbs.run_macro, "pid_alive", return_value=False),
            mock.patch.object(
                verbs,
                "launch_verbs_child",
                return_value=subprocess.CompletedProcess(["python", "-m", "microbench.verbs"], 0, stdout="ok\n"),
            ),
        ):
            rc = verbs.run("dolt", 1, "box", self.board, verbs=["branch"])
        self.assertEqual(rc, 1, "missing stats file must not succeed even if child exit code is 0")
        written = list((self.board / "dolt" / "verbs").rglob("bb_*.json"))
        self.assertEqual(len(written), 1)
        self.assertNotEqual(verbs.validate_verbs(written[0], self.board), [])

        # KeyboardInterrupt during child execution still runs verified_cleanup and server stop
        self.server.delete.reset_mock()
        self.lifecycle.reset_mock()
        with (
            mock.patch.object(verbs, "ROOT", self.root),
            mock.patch.object(verbs.run_macro, "ROOT", self.root),
            mock.patch.object(verbs, "BOARD", self.board),
            mock.patch.object(verbs, "VERB_RUNS", self.board / ".runs" / "verbs"),
            mock.patch.object(verbs, "load_connector", return_value=self.server),
            mock.patch.object(verbs.run_macro, "port_answers", return_value=False),
            mock.patch.object(verbs.run_macro, "pid_alive", return_value=False),
            mock.patch.object(verbs, "launch_verbs_child", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                verbs.run("dolt", 1, "box", self.board, verbs=["branch"])
        self.assertGreaterEqual(self.server.delete.call_count, 1)
        self.lifecycle.assert_called_once_with(self.board / "dolt", "stop")


class Deadlines(unittest.TestCase):
    def test_deadline_interrupts_real_libpq_wait_and_restores_state(self):
        callback = psycopg2.extensions.get_wait_callback()
        handler = signal.getsignal(signal.SIGALRM)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()  # TCP accepts, but no server ever answers the PostgreSQL handshake.
            start = time.monotonic()
            with self.assertRaisesRegex(runner.RunError, "deadline"), runner.bounded(0.05, "connection"):
                psycopg2.connect(hostaddr="127.0.0.1", port=listener.getsockname()[1],
                                 sslmode="disable", dbname="test", connect_timeout=10)
            self.assertLess(time.monotonic() - start, 1)
        self.assertIs(psycopg2.extensions.get_wait_callback(), callback)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)

    def test_deadline_repeats_if_row_finally_also_blocks(self):
        read, write = socket.socketpair()
        with read, write:
            start = time.monotonic()
            with self.assertRaisesRegex(runner.RunError, "deadline"), runner.bounded(0.02, "row"):
                try:
                    read.recv(1)
                finally:
                    read.recv(1)
            self.assertLess(time.monotonic() - start, 2)


if __name__ == "__main__":
    unittest.main()
