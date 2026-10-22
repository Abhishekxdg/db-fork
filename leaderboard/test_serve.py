"""Run with python -m unittest leaderboard.test_serve. Never starts a real benchmark."""

import http.client
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from leaderboard import serve, site
from leaderboard.connector import system_dir
from leaderboard.test_site import Board, FULL
from leaderboard.validate_macro import SCENARIOS


def add_system(board: Path, name: str) -> None:
    sysdir = system_dir(name, board)
    sysdir.mkdir(parents=True)
    (sysdir / "connector.py").touch()
    hosted = name == "neon"
    (sysdir / "system.json").write_text(json.dumps({
        "system": name,
        "proprietary": "no",
        "hosted": "yes" if hosted else "no",
        "tuned": "no",
        "tags": ["test"],
        "requirements": "Test tools installed.",
        "credentials": ["NEON_API_KEY_ORG"] if hosted else [],
    }))


class Launcher(unittest.TestCase):
    def test_invalid_credential_metadata_cannot_reach_script_generation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(serve, "BOARD", Path(directory)):
            board = Path(directory)
            add_system(board, "neon")
            manifest = system_dir("neon", board) / "system.json"
            meta = json.loads(manifest.read_text())
            meta["credentials"] = ["SAFE", "BAD; touch injected"]
            manifest.write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "environment variable names"):
                serve.script_for({"systems": ["neon"], "machine": "bench-01", "location": "US East"})

    def test_script_executes_in_order_quotes_inputs_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, executable = root / "calls.jsonl", root / "fake python"
            executable.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                                  f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                  "sys.exit(7 if os.environ.get('FAIL_SYSTEM') in sys.argv else 0)\n")
            executable.chmod(0o700)
            for system in ("dolt_mysql", "neon", "dolt"):
                add_system(root, system)
            location = "O'Brien $(touch should-not-exist)"
            config = {"systems": ["dolt_mysql", "neon", "dolt"], "machine": "bench-01", "location": location}
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(serve, "BOARD", root), \
                    mock.patch.object(serve, "PYTHON", str(executable)):
                script = serve.script_for(config)
            env = {k: v for k, v in os.environ.items() if k not in ("NEON_API_KEY_ORG", "FAIL_SYSTEM")}
            # Prompts read secrets from stdin, never from shell source or process arguments.
            process = subprocess.run(["/bin/bash"], input=script, text=True, capture_output=True,
                                     env={**env, "NEON_API_KEY_ORG": "test-only"})
            self.assertEqual(process.returncode, 0, process.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([args[2] for args in calls[:3]], config["systems"])
            self.assertNotIn("--client-location", calls[0])
            self.assertEqual(calls[1][-2:], ["--client-location", location])
            self.assertEqual(calls[3:], [["-m", "leaderboard.site", "--phase", "local"]], "the wizard rebuilds the development site only")
            self.assertFalse((root / "should-not-exist").exists())
            log.unlink()
            process = subprocess.run(["/bin/bash", "-c", script], input="test-only\n", text=True,
                                     capture_output=True, env={**env, "FAIL_SYSTEM": "neon"})
            self.assertEqual(process.returncode, 7)
            self.assertEqual(len(log.read_text().splitlines()), 2, 'Failure prevents later runs and rebuilding')

    def test_macro_script_rebuilds_after_a_failed_run_and_keeps_its_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, executable = root / "calls.jsonl", root / "fake python"
            executable.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                                  f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                  "sys.exit(3 if os.environ.get('FAIL_SYSTEM') in sys.argv else 0)\n")
            executable.chmod(0o700)
            for system in ("dolt_mysql", "dolt"):
                add_system(root, system)
            config = {"systems": ["dolt_mysql", "dolt"], "machine": "bench-01", "suite": "macro", "preset": "mini"}
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(serve, "BOARD", root), \
                    mock.patch.object(serve, "PYTHON", str(executable)):
                script = serve.script_for(config)
                full = serve.script_for({**config, "preset": "full"})
            for text in (script, full):
                self.assertNotIn("--allow-dirty", text, "the runner validates its sources; no run bypasses that")
                self.assertNotIn("--scenarios", text, "a campaign always runs all six workflows")
            env = {k: v for k, v in os.environ.items() if k != "FAIL_SYSTEM"}
            process = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True, env={**env, "FAIL_SYSTEM": "dolt_mysql"})
            self.assertEqual(process.returncode, 3, "the first failing run's status survives the rebuild")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([c[1:] for c in calls][:2], [["leaderboard.run_macro", *calls[0][2:]], ["leaderboard.run_macro", *calls[1][2:]]])
            self.assertEqual(calls[2][1:], ["leaderboard.site", "--phase", "local"], "a failed run neither stops the other system nor the rebuild of the development site")
            self.assertEqual(calls[0][2:], ["dolt_mysql", "--preset", "mini", "--machine", "bench-01"])

    def test_macro_script_keeps_the_first_failure_and_always_rebuilds(self):
        """Fake Python exits with the code FAIL_<token> names for the first matching argv token."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, executable = root / "calls.jsonl", root / "fake python"
            executable.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                                  f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                  "sys.exit(next((int(os.environ[k]) for k in (f'FAIL_{a.replace(\".\", \"_\")}' for a in sys.argv) if k in os.environ), 0))\n")
            executable.chmod(0o700)
            for system in ("dolt_mysql", "dolt"):
                add_system(root, system)
            config = {"systems": ["dolt_mysql", "dolt"], "machine": "bench-01", "suite": "macro", "preset": "mini"}
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(serve, "BOARD", root), mock.patch.object(serve, "PYTHON", str(executable)):
                script = serve.script_for(config)
            clean = {k: v for k, v in os.environ.items() if not k.startswith("FAIL_")}
            for failures, expected in (({}, 0), ({"FAIL_dolt_mysql": "3", "FAIL_dolt": "7"}, 3), ({"FAIL_leaderboard_site": "2"}, 2),
                                       ({"FAIL_dolt_mysql": "3", "FAIL_leaderboard_site": "2"}, 3), ({"FAIL_dolt": "7", "FAIL_leaderboard_site": "2"}, 7)):
                with self.subTest(failures=failures):
                    log.write_text("")
                    process = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True, env={**clean, **failures})
                    self.assertEqual(process.returncode, expected, process.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual([c[1] for c in calls], ["leaderboard.run_macro", "leaderboard.run_macro", "leaderboard.site"], "every run and the build always execute")
                    self.assertEqual(calls[2][1:], ["leaderboard.site", "--phase", "local"])

    def test_macro_rejects_unknown_enums_and_hosted_systems(self):
        base = {"systems": ["dolt"], "machine": "bench-01", "suite": "macro", "preset": "mini"}
        for invalid in ({"preset": "huge"}, {"suite": "nano"},
                        # Shapes that used to raise TypeError before the membership test.
                        {"preset": ["mini"]}, {"preset": {}}, {"suite": []},
                        {"systems": [["dolt"]]}, {"machine": ["bench-01"]}, {"location": {}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                serve.script_for({**base, **invalid})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(serve, "BOARD", Path(directory)):
            for system in ("neon", "dolt"):
                add_system(Path(directory), system)
            with self.assertRaisesRegex(ValueError, "local"):
                serve.script_for({**base, "systems": ["neon"], "location": "US East"})
            self.assertIn("leaderboard.run", serve.script_for({**base, "suite": "micro"}))

    def test_missing_connectors_cannot_generate_scripts(self):
        for system in ("tiger",):
            with self.subTest(system=system), self.assertRaisesRegex(ValueError, "no runnable"):
                serve.script_for({"systems": [system], "machine": "bench-01", "location": "US East"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(serve, "BOARD", Path(directory)):
            system = system_dir("dolt", Path(directory))
            system.mkdir(parents=True)
            (system / "connector.py").touch()
            with self.assertRaisesRegex(ValueError, "supported"):
                serve.script_for({"systems": ["dolt"], "machine": "bench-01"})

    def test_rejects_invalid_configuration_and_passes_script_as_data(self):
        config = {"systems": ["dolt"], "machine": "bench-01", "location": ""}
        for invalid in ({"systems": []}, {"systems": ["dolt", "dolt"]}, {"systems": ["other"]},
                        {"systems": [{}]}, {"systems": "dolt"}, {"machine": "a;touch nope"},
                        {"machine": "a" * 65}, {"systems": ["neon"]}, {"location": "bad\nlocation"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                serve.script_for({**config, **invalid})
        script = serve.script_for(config)
        with mock.patch.object(serve.sys, "platform", "darwin"), mock.patch.object(serve.subprocess, "run") as launch:
            serve.open_terminal(script)
        args = launch.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertEqual(shlex.split(args[-1]), ["/bin/bash", "-c", script])
        self.assertNotIn(script, args[2], 'User values are not interpolated into AppleScript')

    def test_http_launch_requires_local_origin_and_exact_reviewed_script(self):
        with Served("local") as served:
            request, launch = served.request, served.launch
            config = {"systems": ["dolt", "dolt_mysql"], "machine": "test-box", "location": ""}
            status, data = request("/api/preview", config)
            self.assertEqual(status, 200)
            reviewed = {**config, "script": json.loads(data)["script"]}
            launch.assert_not_called()
            for headers in ({"Origin": "https://other.example"}, {"Origin": "null"},
                            {"Host": "other.example"}, {"Content-Type": "text/plain"}):
                self.assertEqual(request("/api/run", reviewed, headers)[0], 403)
            self.assertEqual(request("/api/run", {**reviewed, "script": "echo injected"})[0], 400)
            self.assertEqual(request("/api/run", ["bad-config"])[0], 400)
            unsupported = {**config, "systems": ["tiger"], "location": "US East"}
            self.assertEqual(request("/api/preview", unsupported)[0], 400)
            self.assertEqual(request("/api/run", {**unsupported, "script": "echo injected"})[0], 400)
            launch.assert_not_called()
            self.assertEqual(request("/api/run", reviewed)[0], 200)
            launch.assert_called_once_with(reviewed["script"])
            macro = {**config, "suite": "macro", "preset": "mini"}
            status, data = request("/api/preview", macro)
            self.assertEqual(status, 200)
            reviewed_macro = {**macro, "script": json.loads(data)["script"]}
            self.assertIn("leaderboard.run_macro", reviewed_macro["script"])
            self.assertIn("leaderboard.site --phase local", reviewed_macro["script"])
            self.assertEqual(request("/api/run", {**reviewed_macro, "preset": "full"})[0], 400, "the reviewed script must match the enums")
            for bad in ({"preset": ["mini"]}, {"suite": {}}, {"systems": [[]]}, {"machine": {}}):
                with self.subTest(bad=bad):
                    self.assertEqual(request("/api/preview", {**macro, **bad})[0], 400, "a bad shape is a 400, never a dropped connection")
                    self.assertEqual(request("/api/run", {**reviewed_macro, **bad})[0], 400)
            launch.assert_called_once_with(reviewed["script"])
            self.assertEqual(request("/api/run", reviewed_macro)[0], 200)
            launch.assert_called_with(reviewed_macro["script"])
            launch.side_effect = OSError("Terminal unavailable")
            self.assertEqual(request("/api/run", reviewed)[0], 503)

    def test_local_serves_the_development_site_and_prod_only_the_published_site(self):
        for phase in ("local", "alpha", "prod"):
            with self.subTest(phase=phase), Served(phase) as served:
                request, board = served.request, served.board
                self.assertEqual(request("/")[0], 200)
                self.assertIn(b'"phase": "' + phase.encode(), request("/data.js")[1])
                self.assertEqual(request("/index.html")[0], 200); self.assertEqual(request("/styles.css")[0], 200)
                self.assertEqual(request("/macro-data.js")[0], 200)
                local = phase == "local"
                non_prod = phase != "prod"
                self.assertEqual(request("/run.html")[0], 200 if local else 404, "the wizard exists only in local development")
                self.assertEqual(request("/architecture.html")[0], 200 if non_prod else 404)
                self.assertEqual(request("/README.md")[0], 200 if non_prod else 404)
                self.assertEqual(request("/requirements/README.md")[0], 200 if non_prod else 404)
                self.assertEqual(request("/mock/tiger.json")[0], 404, "a mock file is not a record and is never served")
                self.assertEqual(request(f"/{board.smoke}")[0], 200 if non_prod else 404, "a smoke campaign is never served by production")
                self.assertEqual(request(f"/{board.smoke_attempt}")[0], 200 if non_prod else 404)
                self.assertEqual(request(f"/{board.unpublished}")[0], 200 if non_prod else 404, "a valid result the manifest does not name is not downloadable in production")
                self.assertEqual(request(f"/{board.published}")[0], 200, "the published record is downloadable in all phases")
                self.assertEqual(request(f"/{board.published_campaign}")[0], 200)
                self.assertEqual(request(f"/{board.published_attempt}")[0], 200)
                for rel in site.WORKFLOW_DIAGRAMS:
                    for method in ("GET", "HEAD"):
                        status, data, headers = served.response(f"/{rel}", method)
                        self.assertEqual((status, headers["content-type"]), (200, "image/png"), f"{phase} {method} {rel}: the workflow map is served as an image")
                    self.assertEqual(data, b"", "HEAD carries no body")
                    self.assertEqual(request(f"/{rel}")[1], (board.board / rel).read_bytes(), f"{phase}: {rel} is the real rendering, byte for byte")
                stray = served.board.site / phase / "workflow-diagrams"
                (stray / "source.html").write_text("<!doctype html>"); (stray / "draft.png").write_bytes(b"\x89PNG"); (stray / "rl_env.svg").write_text("<svg/>")
                for path in ("/workflow-diagrams/source.html", "/workflow-diagrams/draft.png", "/workflow-diagrams/rl_env.svg", "/workflow-diagrams/", "/workflow-diagrams"):
                    self.assertEqual(request(path)[0], 404, f"{path}: only the six named maps are routes, even if a file is dropped into the output")
                    self.assertEqual(request(path, method="HEAD")[0], 404, path)
                for path, config in (("/api/preview", {"systems": ["dolt"], "machine": "box", "location": ""}), ("/api/run", {"systems": ["dolt"], "machine": "box", "location": "", "script": "x"})):
                    self.assertEqual(request(path, config)[0], 200 if local and path == "/api/preview" else 400 if local else 404, "no launcher route outside local")
                served.launch.assert_not_called()
                for path in ("/.runs/dolt.journal.json", "/.runs/macro/bb_00000000/runner.log", "/dolt/macro/20261010/notes.json", "/dolt/macro/20261010/bb_00000000.parquet",
                             "/serve.py", "/site.py", "/publish.json", "/../.env", "/%2e%2e/.env", "/dolt/macro/20261010/campaign_x.json", "/.site/local/index.html",
                             f"/../local/{board.smoke}", "/suite.json", "/dolt/system.json"):
                    self.assertEqual(request(path)[0], 404, path)
                    self.assertEqual(request(path, method="HEAD")[0], 404, path)
                self.assertEqual(request("/", headers={"Host": "other.example"})[0], 403)


def scratch_board() -> Board:
    """A board with one published and one unpublished micro result, one published Full campaign and one quick (mini) campaign."""
    b = Board()
    b.setUp()
    b.write(b.payload())
    b.published = "dolt/results/20260930/box.json"
    b.write(b.payload(system="dolt_mysql"), system="dolt_mysql")
    b.unpublished = "dolt_mysql/results/20260930/box.json"
    b.published_campaign = b.write_campaign(dict(FULL))
    b.published_attempt = "dolt_mysql/macro/20261011/bb_10000000.json"
    b.smoke = b.write_campaign({**FULL, "preset": "mini", "id": "0000a1ff", "started": "2026-10-12T10:00:00+00:00"}, "2")
    b.smoke_attempt = "dolt_mysql/macro/20261012/bb_20000000.json"
    b.publish({"micro": [b.published], "macro": [b.published_campaign]})
    return b


class Served:
    """A loopback server of one phase on a scratch site, with the launcher stubbed."""

    def __init__(self, phase: str):
        self.phase = phase

    def __enter__(self):
        self.board = scratch_board()
        site.assemble(self.phase, self.board.board)
        self.patches = [mock.patch.object(site, "SITE", self.board.site), mock.patch.object(serve.Handler, "phase", self.phase),
                        mock.patch.object(serve, "open_terminal"), mock.patch.object(serve.Handler, "log_message"),
                        mock.patch.object(serve, "BOARD", self.board.board)]
        started = [p.start() for p in self.patches]
        self.launch = started[2]
        self.server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def request(self, path, config=None, headers=None, method=None):
        return self.response(path, method, config, headers)[:2]

    def response(self, path, method=None, config=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        base = {"Origin": self.origin, "Content-Type": "application/json"}
        connection.request(method or ("POST" if config is not None else "GET"), path,
                           json.dumps(config) if config is not None else None, base | (headers or {}))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data, {k.lower(): v for k, v in response.getheaders()}

    def __exit__(self, *exc):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        for p in self.patches:
            p.stop()
        self.board.doCleanups()


if __name__ == "__main__":
    unittest.main()
