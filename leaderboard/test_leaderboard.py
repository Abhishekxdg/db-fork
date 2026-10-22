"""Checks for the leaderboard.

Run from the repository root: python -m unittest leaderboard.test_leaderboard
These tests need no database server.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from leaderboard import aws_worker
from leaderboard.build import build
from leaderboard.systems.file_copy.connector import FileCopy
from leaderboard.mock import entries as mock_rows
from leaderboard.connector import (
    Connector,
    connector_names,
    load_connector,
    load_system,
    parse_system,
    system_catalog,
    system_dir,
    system_identity,
)
from leaderboard.run import RunError, clean_up, exclusive, scrub
from leaderboard.validate import BOARD, SCHEMA_VERSION, sha256, validate
from microbench.datagen import DynamicDataGenerator

ITEM_DDL = """CREATE TABLE item (
    i_id INT NOT NULL,
    i_im_id INT,
    i_name VARCHAR(24),
    i_price DECIMAL(5, 2),
    i_data VARCHAR(50),
    PRIMARY KEY (i_id)
);"""


class RowGenerator(unittest.TestCase):
    def test_generated_item_rows_fit_the_columns(self) -> None:
        gen = DynamicDataGenerator(ITEM_DDL)
        for _ in range(200):
            row = gen.generate_row()
            self.assertLessEqual(row["i_price"], 999.99)
            self.assertLessEqual(len(row["i_name"]), 24)
            self.assertLessEqual(len(row["i_data"]), 50)


class ResultFiles(unittest.TestCase):
    """The validator and the build, on a scratch copy of the board."""

    def setUp(self) -> None:
        self.board = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.board)
        shutil.copy(BOARD / "suite.json", self.board)
        (self.board / "systems").mkdir()
        for system in ("dolt", "neon"):
            shutil.copytree(system_dir(system), system_dir(system, self.board), ignore=shutil.ignore_patterns("__pycache__"))
        self.suite = json.loads((BOARD / "suite.json").read_text())

    def payload(self, date: str = "2026-09-30", **changes) -> dict:
        meta = system_identity(load_system("dolt"))
        return {"schema_version": SCHEMA_VERSION, "run_id": "0123abcd", **meta, "date": date, "machine": "box",
                "version": "0.54.10", "suite": self.suite["suite"], "suite_sha256": sha256(BOARD / "suite.json"),
                "seed": self.suite["seed"], "commit": "0" * 40, "dirty": False,
                "dataset_sha256": self.suite["dataset"]["sha256"], "python": "3.13.14", "lock_sha256": "0" * 64,
                "provision_time": 0.07, "load_time": 2.3,
                "result": [[0.01] * self.suite["tries"] for _ in self.suite["rows"]], **changes}

    def write(self, payload: dict, day_dir: str = "20260930", system: str = "dolt") -> Path:
        path = self.board / system / "results" / day_dir / "box.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_valid_file_passes(self) -> None:
        self.assertEqual(validate(self.write(self.payload()), self.board), [])

    def test_bad_files_are_rejected(self) -> None:
        good = self.payload()["result"]
        cases = {  # label: file, date directory, the problem it must report
            "a short row": (self.payload(result=[good[0][:2], *good[1:]]), "20260930", "rows of 3 tries"),
            "a wrong suite digest": (self.payload(suite_sha256="0" * 64), "20260930", "suite_sha256 differs"),
            "a bad date directory": (self.payload(), "2026-09-30", "not a YYYYMMDD date"),
            "a boolean timing": (self.payload(result=[[True, 0.01, 0.01], *good[1:]]), "20260930", "every try"),
            "a secret-looking field": (self.payload(version="postgresql://u:pw@host/db"), "20260930", "credential"),
            "a capped run": (self.payload(max_batch=5), "20260930", "max_batch"),
            "a dirty run": (self.payload(dirty=True), "20260930", "uncommitted changes"),
            "a mock posing as a result": (self.payload(mock=True), "20260930", "unexpected keys"),
        }
        for label, (payload, day_dir, problem) in cases.items():
            with self.subTest(label):
                problems = validate(self.write(payload, day_dir), self.board)
                self.assertTrue(any(problem in p for p in problems), problems)

    def test_hosted_results_need_their_fields(self) -> None:
        def hosted(**changes) -> dict:
            meta = system_identity(load_system("neon"))
            return self.payload(**{**meta, "region": "aws-us-east-1", "rtt_ms": 12.5, "client_location": "US East",
                                   "service": {"region": "aws-us-east-1", "pg_version": 17}, **changes})

        self.assertEqual(validate(self.write(hosted(), system="neon"), self.board), [])
        cases = {  # label: file, system, the problem it must report
            "no round trip": ({k: v for k, v in hosted().items() if k != "rtt_ms"}, "neon", "missing keys"),
            "a service key outside the allowlist": (hosted(service={"api_hint": "x"}), "neon", "allowlist"),
            "hosted fields on a local system": (self.payload(region="us-east-1"), "dolt", "unexpected keys"),
        }
        for label, (payload, system, problem) in cases.items():
            with self.subTest(label):
                problems = validate(self.write(payload, system=system), self.board)
                self.assertTrue(any(problem in p for p in problems), problems)

    def test_newest_date_wins(self) -> None:
        self.write(self.payload("2026-09-29"), "20260929")
        self.write(self.payload(load_time=9.0))
        entries, excluded = build(self.board)
        self.assertEqual([(e["load_time"], e["source"]) for e in entries], [(9.0, "dolt/results/20260930/box.json")])
        self.assertEqual(excluded, [])

    def test_previous_suite_is_excluded(self) -> None:
        self.write(self.payload(suite="local-1", suite_sha256="0" * 64))
        entries, excluded = build(self.board)
        self.assertEqual(entries, [])
        self.assertIn("suite differs", excluded[0]["reason"])

    def test_a_mock_stands_in_only_until_a_real_file_exists(self) -> None:
        mock = {"date": "2026-09-30", "machine": "box", "region": "r",
                "rtt_ms": 20.0, "client_location": "US East", "provision_time": 5.0, "load_time": 9.0,
                "result": self.payload()["result"]}
        (self.board / "mock").mkdir()
        (self.board / "mock" / "neon.json").write_text(json.dumps(mock))
        self.assertEqual(build(self.board), ([], []), "the build carries real results only")
        self.assertEqual([(e["system"], e["date"], "source" in e) for e in mock_rows(self.board)], [("Neon", "2026-09-30", False)])
        self.write({"error": "RunError: neon failed"}, system="neon")
        self.assertEqual(mock_rows(self.board), [], "a first result file, even an error record, retires the mock")

    def test_committed_mocks_match_the_suite(self) -> None:
        for path in sorted((BOARD / "mock").glob("*.json")):
            with self.subTest(path.name):
                mock = json.loads(path.read_text())
                self.assertEqual(load_system(path.stem)["hosted"], "yes")
                self.assertEqual([len(r) for r in mock["result"]], [self.suite["tries"]] * len(self.suite["rows"]))
                self.assertTrue(all(c > 0 for r in mock["result"] for c in r))

    def test_newer_error_hides_older_result(self) -> None:
        self.write(self.payload("2026-09-29"), "20260929")
        self.write({"error": "RunError: server failed its check for 60 s"})
        entries, excluded = build(self.board)
        self.assertEqual(entries, [])
        self.assertEqual([e["reason"] for e in excluded], ["RunError: server failed its check for 60 s"])


class Runner(unittest.TestCase):
    def test_runner_has_no_personal_secret_manager_policy(self) -> None:
        forbidden = "bit" + "warden"
        self.assertNotIn(forbidden, (BOARD / "run.py").read_text(encoding="utf-8").lower())

    def test_a_second_run_of_a_system_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as runs, mock.patch("leaderboard.run.RUNS", Path(runs)):
            with exclusive("neon"), self.assertRaisesRegex(RunError, "another run of neon"):
                with exclusive("neon"):
                    pass
            with exclusive("dolt"), self.assertRaisesRegex(RunError, "another benchmark run is in progress"):
                with exclusive("matrixone"):
                    pass

    def test_unreadable_journal_is_reported_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as runs, mock.patch("leaderboard.run.RUNS", Path(runs)):
            journal = Path(runs) / "dolt.journal.json"
            journal.write_text('{"run_id": "1a2b')  # cut short by a kill mid-write
            self.assertIsInstance(clean_up("dolt", load_connector("dolt")), ValueError)
            self.assertTrue(journal.exists())

    def test_errors_never_carry_a_uri_password(self) -> None:
        self.assertEqual(scrub("connect failed: postgresql://owner:hunter2@ep-1.neon.tech/db?sslmode=require"),
                         "connect failed: postgresql://owner:***@ep-1.neon.tech/db?sslmode=require")


class Connectors(unittest.TestCase):
    def test_every_connector_in_the_repository_fills_the_contract(self) -> None:
        self.assertTrue(connector_names())
        self.assertEqual(connector_names(), list(system_catalog(runnable=True)))
        for name in connector_names():
            self.assertIsInstance(load_connector(name), Connector)
        with self.assertRaises(ValueError):
            load_connector("no_such_system")

    def test_adding_a_system_is_adding_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as board:
            systems = Path(board) / "systems"
            systems.mkdir()
            for name, files in (("new", ("system.json", "connector.py")), ("preview", ("system.json",)),
                                ("stray", ("connector.py",))):
                (systems / name).mkdir()
                for file in files:
                    content = json.dumps({**load_system("dolt"), "system": name}) if file == "system.json" else ""
                    (systems / name / file).write_text(content)
            self.assertEqual(connector_names(Path(board)), ["new"])

    def test_system_metadata_rejects_unsafe_or_ambiguous_fields(self) -> None:
        meta = load_system("dolt")
        with self.assertRaisesRegex(ValueError, "invalid system key"):
            load_system("__proto__")
        cases = (
            ({**meta, "credentials": ["SAFE", "BAD-NAME"]}, "environment variable names"),
            ({**meta, "credentials": ["SAME", "SAME"]}, "unique environment variable names"),
            ({**meta, "requirements": "line one\nline two"}, "printable text"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                parse_system(json.dumps(value), Path("system.json"))

    def test_file_copy_provision_runs_info_before_create_database_and_restores_method(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prev_file = Path(tmp) / "file_copy.prev_method"
            fc = FileCopy()
            fc.prev_method_file = prev_file
            events: list[str] = []
            current_method = ["copy"]
            conn = mock.MagicMock()
            cur = conn.cursor.return_value.__enter__.return_value

            def exec_sql(sql, params=None):
                events.append(sql if params is None else f"{sql} {params}")

            cur.execute.side_effect = exec_sql
            cur.fetchone.side_effect = lambda: (current_method[0],)

            def make_info(db: str):
                events.append(f"FileCopyInfo({db})")
                current_method[0] = "clone"
                info = mock.Mock()
                info.prev_method = "copy"
                return info

            with mock.patch.object(fc, "_admin", return_value=conn), \
                    mock.patch("leaderboard.systems.file_copy.connector.FileCopyToolSuite.FileCopyInfo", side_effect=make_info), \
                    mock.patch("leaderboard.systems.file_copy.connector.file_copy.drop_database", side_effect=lambda db: events.append(f"drop({db})")), \
                    mock.patch.object(fc, "exists", return_value=False):
                fc.provision("bb_1234abcd")
                self.assertEqual(prev_file.read_text().strip(), "copy")
                # Simulate crash recovery where version() and capture_prev_method() run while server is still 'clone'
                fc_other = FileCopy()
                fc_other.prev_method_file = prev_file
                with mock.patch.object(fc_other, "_admin", return_value=conn), \
                        mock.patch.object(fc_other, "exists", return_value=False):
                    fc_other.version()
                    fc_other.capture_prev_method()
                    self.assertEqual(prev_file.read_text().strip(), "copy")
                    fc_other.delete("bb_1234abcd")
                self.assertFalse(prev_file.exists())
                # A second recovery delete on a fresh connector instance after prev_file is gone must be idempotent
                fc_repeat = FileCopy()
                fc_repeat.prev_method_file = prev_file
                with mock.patch.object(fc_repeat, "_admin", return_value=conn), \
                        mock.patch.object(fc_repeat, "exists", return_value=False):
                    fc_repeat.delete("bb_1234abcd")
            self.assertEqual(events[:3], ["SHOW file_copy_method;", "FileCopyInfo(bb_1234abcd)", 'CREATE DATABASE "bb_1234abcd"'])
            self.assertIn("ALTER SYSTEM SET file_copy_method = %s; ('copy',)", events)
            self.assertNotIn("ALTER SYSTEM RESET file_copy_method;", events)

    def test_matrixone_start_relocates_mo_data_dir_and_keeps_launch_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            mo_home = home / "mo" / "matrixone"
            (mo_home / "etc" / "launch").mkdir(parents=True)
            (mo_home / "etc" / "launch" / "launch.toml").write_text("# config\n")
            fake_bin = mo_home / "mo-service"
            fake_bin.write_text("#!/bin/bash\npwd > \"$MO_DATA_DIR/launched_from\"\necho \"$@\" > \"$MO_DATA_DIR/launched_args\"\n")
            fake_bin.chmod(0o755)
            board = root / "leaderboard"
            sysdir = system_dir("matrixone", board)
            sysdir.mkdir(parents=True)
            shutil.copy(system_dir("matrixone") / "start", sysdir / "start")
            mount_data = root / "mnt" / "nvme" / "mo" / "matrixone" / "mo-data"
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "MO_DATA_DIR": str(mount_data),
            }
            res = subprocess.run([str(sysdir / "start")], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((board / ".runs" / "matrixone.pid").is_file())
            self.assertTrue((mo_home / "mo-data").is_symlink())
            self.assertEqual((mo_home / "mo-data").resolve(), mount_data.resolve())
            for _ in range(20):
                if (mount_data / "launched_args").is_file():
                    break
                __import__("time").sleep(0.05)
            self.assertEqual((mount_data / "launched_args").read_text().strip(), "-launch ./etc/launch/launch.toml")

    def test_dolt_and_dolt_mysql_check_and_remove_dropped_databases_trash(self) -> None:
        for name in ("dolt", "dolt_mysql"):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                srv = load_connector(name)
                trash = Path(tmp) / ".dolt_dropped_databases" / "bb_1234abcd"
                trash.mkdir(parents=True)
                conn = mock.MagicMock()
                cur = conn.cursor.return_value.__enter__.return_value
                cur.fetchone.return_value = None
                target = mock.patch.object(srv, "_admin", return_value=conn) if name == "dolt" else mock.patch("leaderboard.systems.dolt_mysql.connector.dolt_mysql.connect", return_value=conn)
                with mock.patch.object(srv, "data_dir", Path(tmp)), target, \
                        mock.patch("leaderboard.systems.dolt_mysql.connector.dolt_mysql.drop_database"):
                    self.assertTrue(srv.exists("bb_1234abcd"))
                    srv.delete("bb_1234abcd")
                    self.assertFalse(trash.exists())
                    self.assertFalse(srv.exists("bb_1234abcd"))

    def test_no_files_outside_leaderboard_differ_from_main_merge_base(self) -> None:
        merge_head = subprocess.run(["git", "-C", str(BOARD.parent), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                                    capture_output=True, text=True)
        if merge_head.returncode == 0:
            ref = merge_head.stdout.strip()
        else:
            base = subprocess.run(["git", "-C", str(BOARD.parent), "merge-base", "HEAD", "origin/main"],
                                  capture_output=True, text=True)
            if base.returncode != 0:
                self.skipTest("origin/main is not available in this git clone")
            ref = base.stdout.strip()
        changed = subprocess.run(
            ["git", "-C", str(BOARD.parent), "diff", "--name-only", ref, "--", ".", ":!leaderboard"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(changed, [], f"files outside leaderboard/ differ from {ref}: {changed}")


class AwsWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.board = self.tmp / "leaderboard"
        self.runs = self.board / ".runs"
        shutil.copytree(BOARD, self.board, ignore=shutil.ignore_patterns(".runs", ".site", "results", "macro"))
        self.suite = json.loads((self.board / "suite.json").read_text())
        patch = mock.patch("leaderboard.aws_worker.resolve_shutdown_bin", return_value="/sbin/shutdown")
        patch.start()
        self.addCleanup(patch.stop)

    def _valid_micro_payload(self, system: str = "dolt", machine: str = "aws-box") -> dict:
        meta = system_identity(load_system(system, self.board))
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": "0123abcd",
            **meta,
            "date": "2026-10-20",
            "machine": machine,
            "version": "1.0.0",
            "suite": self.suite["suite"],
            "suite_sha256": sha256(self.board / "suite.json"),
            "seed": self.suite["seed"],
            "commit": "0" * 40,
            "dirty": False,
            "dataset_sha256": self.suite["dataset"]["sha256"],
            "python": "3.13.14",
            "lock_sha256": "0" * 64,
            "provision_time": 0.05,
            "load_time": 1.2,
            "result": [[0.01] * self.suite["tries"] for _ in self.suite["rows"]],
        }

    def test_duplicate_worker_lock_failure_never_triggers_poweroff(self) -> None:
        cmds: list[list[str]] = []

        def fake_cmd(cmd, **kwargs):
            cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with aws_worker.batch_lock(self.runs):
            rc = aws_worker.run_batch(
                machine="aws-box",
                lane="micro",
                systems_arg="dolt",
                poweroff=True,
                board=self.board,
                runs_dir=self.runs,
                cmd_runner=fake_cmd,
                port_checker=lambda _: False,
                sys_platform="Darwin",
            )
        self.assertEqual(rc, 1)
        self.assertEqual(cmds, [], "duplicate worker that fails lock acquisition must never run sudo shutdown")

    def test_interruption_or_dirty_host_aborts_before_next_system(self) -> None:
        steps: list[str] = []

        def interrupt_step(cmd, remaining, env):
            steps.append(cmd[3])
            return 130, True, False

        rc = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt matrixone",
            board=self.board,
            runs_dir=self.runs,
            step_runner=interrupt_step,
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc, 130)
        self.assertEqual(steps, ["dolt"], "SIGINT (130) must abort the batch immediately")

        steps.clear()

        def leave_journal_step(cmd, remaining, env):
            steps.append(cmd[3])
            out = self.board / cmd[3] / "results" / "20261020" / "aws-box.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self._valid_micro_payload(cmd[3])))
            (self.runs / f"{cmd[3]}.journal.json").write_text('{"name":"bb_1234abcd"}')
            return 0, False, False

        rc2 = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt matrixone",
            board=self.board,
            runs_dir=self.runs,
            step_runner=leave_journal_step,
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc2, 1)
        self.assertEqual(steps, ["dolt"], "leftover journal/server after a step must abort before next system")

    def test_poweroff_preflight_and_shutdown_failure_propagate_nonzero(self) -> None:
        steps: list[list[str]] = []

        # 1. sudo -n fails during preflight -> batch does not run any step
        rc_pre = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt",
            poweroff=True,
            board=self.board,
            runs_dir=self.runs,
            step_runner=lambda cmd, rem, env: (steps.append(cmd) or (0, False, False)),
            cmd_runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required"),
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc_pre, 1)
        self.assertEqual(steps, [])

        # 2. sudo -n -l succeeds with NOPASSWD in preflight, step succeeds, but shutdown -h now fails -> returns 1
        def step_ok(cmd, rem, env):
            out = self.board / cmd[3] / "results" / "20261020" / "aws-box.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self._valid_micro_payload(cmd[3])))
            return 0, False, False

        def cmd_shutdown_fails(cmd, **kw):
            if "-l" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "(ALL) NOPASSWD: /sbin/shutdown -h now\n", "")
            if any("shutdown" in str(c) for c in cmd):
                return subprocess.CompletedProcess(cmd, 1, "", "shutdown failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        rc_shut = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt",
            poweroff=True,
            board=self.board,
            runs_dir=self.runs,
            step_runner=step_ok,
            cmd_runner=cmd_shutdown_fails,
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc_shut, 1)

        # 3. sudoers policies allowing only /usr/bin/id, PASSWD: /sbin/shutdown, non-root runas, other shutdown binary, or bare path are rejected
        for bad_stdout in (
            "User ubuntu may run the following commands:\n    (ALL) NOPASSWD: /usr/bin/id\n",
            "User ubuntu may run the following commands:\n    (ALL) NOPASSWD: /usr/bin/id, PASSWD: /sbin/shutdown\n",
            "(root) PASSWD: /sbin/shutdown -h now\n(backup) NOPASSWD: /sbin/shutdown -h now\n",
            "(root) PASSWD: /sbin/shutdown -h now\n(root) NOPASSWD: /opt/test/shutdown -h now\n",
            "/sbin/shutdown -h now\n",
        ):
            rc_bad = aws_worker.run_batch(
                machine="aws-box",
                lane="micro",
                systems_arg="dolt",
                poweroff=True,
                board=self.board,
                runs_dir=self.runs,
                step_runner=lambda cmd, rem, env: (steps.append(cmd) or (0, False, False)),
                cmd_runner=lambda cmd, out=bad_stdout, **kw: subprocess.CompletedProcess(cmd, 0, out, ""),
                port_checker=lambda _: False,
                sys_platform="Darwin",
            )
            self.assertEqual(rc_bad, 1, bad_stdout)
            self.assertEqual(steps, [])

    def test_batch_lock_blocks_direct_runner_and_allows_tokenized_child(self) -> None:
        with mock.patch("leaderboard.run.RUNS", self.runs):
            with aws_worker.batch_lock(self.runs) as token:
                with self.assertRaisesRegex(RunError, "another benchmark batch worker"):
                    with exclusive("dolt"):
                        pass
                with mock.patch.dict("os.environ", {"DBFORK_BATCH_TOKEN": token}):
                    with exclusive("dolt"):
                        pass

    def test_negative_signal_and_preflight_deadline_and_validation_crash_poweroff(self) -> None:
        # 1. Negative signal (-9 / -SIGKILL) aborts batch before next system
        seen: list[str] = []
        rc_sigkill = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt matrixone",
            board=self.board,
            runs_dir=self.runs,
            step_runner=lambda cmd, rem, env: (seen.append(cmd[3]) or (-9, aws_worker.is_interrupted_returncode(-9), False)),
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc_sigkill, 130)
        self.assertEqual(seen, ["dolt"])

        # 2. Preflight consuming batch deadline aborts before starting any child step (even when shutdown also fails)
        fake_now = [100.0]
        child_called: list[str] = []

        def slow_preflight_and_failing_shutdown(cmd, **kw):
            if "-l" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "(ALL) NOPASSWD: /sbin/shutdown -h now\n", "")
            if "-h" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "shutdown failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def slow_port_checker(scope):
            fake_now[0] += 120.0
            return False

        rc_deadline = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt",
            batch_deadline_min=1.0,
            poweroff=True,
            board=self.board,
            runs_dir=self.runs,
            step_runner=lambda cmd, rem, env: (child_called.append(cmd[3]) or (0, False, False)),
            cmd_runner=slow_preflight_and_failing_shutdown,
            port_checker=slow_port_checker,
            sys_platform="Darwin",
            clock=lambda: fake_now[0],
        )
        self.assertEqual(rc_deadline, 124)
        self.assertEqual(child_called, [])

        # 3. Invalid UTF-8 in produced artifact does not skip poweroff
        shutdown_calls: list[list[str]] = []

        def write_bad_utf8(cmd, rem, env):
            out = self.board / cmd[3] / "results" / "20261020" / "aws-box.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\xff\xfeinvalid utf8")
            return 0, False, False

        def track_shutdown(cmd, **kw):
            if "-l" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "(ALL) NOPASSWD: /sbin/shutdown -h now\n", "")
            if "-h" in cmd:
                shutdown_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        rc_utf8 = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt",
            poweroff=True,
            board=self.board,
            runs_dir=self.runs,
            step_runner=write_bad_utf8,
            cmd_runner=track_shutdown,
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc_utf8, 1)
        self.assertEqual(len(shutdown_calls), 1, "poweroff must still run when validation encounters invalid UTF-8")

        # 4. Advancing clock past deadline inside artifact validator returns 124
        val_now = [100.0]
        art = self.board / "dolt" / "results" / "20261020" / "aws-box.json"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text(json.dumps(self._valid_micro_payload("dolt")))
        with mock.patch("leaderboard.aws_worker.validate_micro", side_effect=lambda p, b: (val_now.__setitem__(0, 300.0) or [])):
            self.assertEqual(
                aws_worker.validate_batch_artifacts([art], [], board=self.board, deadline=200.0, clock=lambda: val_now[0]),
                124,
            )

    def test_execute_step_terminates_surviving_descendant_on_timeout(self) -> None:
        pid_file = self.tmp / "grandchild.pid"
        script = (
            "import os, signal, subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
            "time.sleep(60)"
        )
        rc, interrupted, timed_out = aws_worker.execute_step(
            [__import__("sys").executable, "-c", script],
            remaining_sec=0.3,
            env=dict(__import__("os").environ),
        )
        self.assertEqual((rc, interrupted, timed_out), (124, False, True))
        grandchild_pid = int(pid_file.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            __import__("os").kill(grandchild_pid, 0)

    def test_systemd_conflicts_open_ports_and_batch_scoped_validation(self) -> None:
        # 1. pg18-bench active on Linux is caught by systemd preflight
        with mock.patch("shutil.which", return_value="/bin/systemctl"):
            with self.assertRaisesRegex(RuntimeError, "pg18-bench"):
                aws_worker.check_linux_systemd_conflicts(
                    sys_platform="Linux",
                    run_cmd=lambda cmd, **kw: subprocess.CompletedProcess(
                        cmd, 0 if cmd[-1] == "pg18-bench" else 3, "", ""
                    ),
                )

        # 2. Open benchmark port is caught by verify_host_clean
        with self.assertRaisesRegex(RuntimeError, "already accepting connections"):
            aws_worker.verify_host_clean(
                board=self.board,
                runs_dir=self.runs,
                check_port=lambda scope: int(scope["port"]) == 5433,
            )

        # 3. Unrelated broken historical result on disk does not fail a clean batch
        broken_historical = self.board / "matrixone" / "results" / "20260101" / "old-box.json"
        broken_historical.parent.mkdir(parents=True, exist_ok=True)
        broken_historical.write_text("{broken json")

        def step_ok(cmd, rem, env):
            out = self.board / cmd[3] / "results" / "20261020" / "aws-box.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self._valid_micro_payload(cmd[3])))
            return 0, False, False

        rc = aws_worker.run_batch(
            machine="aws-box",
            lane="micro",
            systems_arg="dolt",
            board=self.board,
            runs_dir=self.runs,
            step_runner=step_ok,
            port_checker=lambda _: False,
            sys_platform="Darwin",
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
