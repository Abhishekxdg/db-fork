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

from leaderboard import serve


class Launcher(unittest.TestCase):
    def test_script_executes_in_order_quotes_inputs_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, executable = root / "calls.jsonl", root / "fake python"
            executable.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                                  f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                  "sys.exit(7 if os.environ.get('FAIL_SYSTEM') in sys.argv else 0)\n")
            executable.chmod(0o700)
            location = "O'Brien $(touch should-not-exist)"
            config = {"systems": ["dolt_mysql", "neon", "dolt"], "machine": "bench-01", "location": location}
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(serve, "PYTHON", str(executable)):
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
            self.assertEqual(calls[3:], [["-m", "leaderboard.validate"], ["-m", "leaderboard.build"]])
            self.assertFalse((root / "should-not-exist").exists())
            log.unlink()
            process = subprocess.run(["/bin/bash", "-c", script], input="test-only\n", text=True,
                                     capture_output=True, env={**env, "FAIL_SYSTEM": "neon"})
            self.assertEqual(process.returncode, 7)
            self.assertEqual(len(log.read_text().splitlines()), 2, 'Failure prevents later runs and validation')

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
        with serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler) as server, \
                mock.patch.object(serve, "open_terminal") as launch, \
                mock.patch.object(serve.Handler, "log_message"):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"

            def request(path, config=None, headers=None, method=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                base = {"Origin": origin, "Content-Type": "application/json"}
                connection.request(method or ("POST" if config is not None else "GET"), path,
                                   json.dumps(config) if config is not None else None, base | (headers or {}))
                response = connection.getresponse()
                data = response.read()
                connection.close()
                return response.status, data

            try:
                config = {"systems": ["dolt", "neon"], "machine": "test-box", "location": "US East"}
                status, data = request("/api/preview", config)
                self.assertEqual(status, 200)
                reviewed = {**config, "script": json.loads(data)["script"]}
                launch.assert_not_called()
                for headers in ({"Origin": "https://other.example"}, {"Origin": "null"},
                                {"Host": "other.example"}, {"Content-Type": "text/plain"}):
                    self.assertEqual(request("/api/run", reviewed, headers)[0], 403)
                self.assertEqual(request("/api/run", {**reviewed, "script": "echo injected"})[0], 400)
                self.assertEqual(request("/api/run", ["bad-config"])[0], 400)
                launch.assert_not_called()
                self.assertEqual(request("/api/run", reviewed)[0], 200)
                launch.assert_called_once_with(reviewed["script"])
                launch.side_effect = OSError("Terminal unavailable")
                self.assertEqual(request("/api/run", reviewed)[0], 503)
                self.assertEqual(request("/run.html")[0], 200)
                for path in ("/.runs/dolt.journal.json", "/serve.py", "/../.env", "/%2e%2e/.env"):
                    self.assertEqual(request(path)[0], 404)
                    self.assertEqual(request(path, method="HEAD")[0], 404)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
