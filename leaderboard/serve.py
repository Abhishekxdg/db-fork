"""Local wizard with one-click macOS Terminal launch; standard library only.

Run from the repository root: python -m leaderboard.serve
"""

import argparse
import json
import shlex
import subprocess
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from leaderboard.validate import BOARD, MACHINE_RE

ROOT = BOARD.parent
PYTHON = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
SYSTEMS = {
    "dolt_mysql": (), "dolt": (), "neon": ("NEON_API_KEY_ORG",),
    "tiger": ("TIGER_ACCESS_KEY", "TIGER_SECRET_KEY", "TIGER_PROJECT_ID"),
    "xata": ("XATA_API_KEY", "XATA_ORGANIZATION_ID"),
}
STATIC = {"/", "/index.html", "/run.html", "/styles.css", "/data.js", "/README.md"}


def script_for(config: dict) -> str:
    systems, machine, location = config.get("systems"), config.get("machine"), config.get("location", "")
    if (not isinstance(systems, list) or not 1 <= len(systems) <= len(SYSTEMS)
            or any(not isinstance(system, str) or system not in SYSTEMS for system in systems)
            or len(set(systems)) != len(systems)):
        raise ValueError("Choose one or more distinct supported databases.")
    if not isinstance(machine, str) or not MACHINE_RE.fullmatch(machine):
        raise ValueError("Use a valid machine name.")
    if not isinstance(location, str) or len(location) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in location):
        raise ValueError("Use a valid client location.")
    keys = [key for system in systems for key in SYSTEMS[system]]
    if keys and not location.strip():
        raise ValueError("Enter the client location for managed services.")
    lines = ["#!/bin/bash", "set -e", f"cd {shlex.quote(str(ROOT))}"]
    if keys:
        lines += ["", "# Terminal prompts for missing credentials; input is hidden.",
                  f"for benchmark_key in {' '.join(keys)}; do",
                  '  if [ -z "${!benchmark_key:-}" ]; then',
                  '    read -r -s -p "$benchmark_key: " "$benchmark_key"',
                  "    printf '\\n'", "  fi", '  export "$benchmark_key"', "done"]
    lines += ["", "# Run one database at a time, then validate and rebuild."]
    for system in systems:
        args = [PYTHON, "-m", "leaderboard.run", system, "--machine", machine]
        if SYSTEMS[system]:
            args += ["--client-location", location.strip()]
        lines.append(shlex.join(args))
    lines += [shlex.join([PYTHON, "-m", "leaderboard.validate"]),
              shlex.join([PYTHON, "-m", "leaderboard.build"])]
    return "\n".join(lines) + "\n"


def open_terminal(script: str) -> None:
    if sys.platform != "darwin":
        raise ValueError("One-click Terminal launch is available on macOS. You can still copy the script.")
    # Script is data to AppleScript, and a single shell-quoted argument to bash.
    applescript = '''on run argv
tell application "Terminal"
    activate
    do script (item 1 of argv)
end tell
end run'''
    subprocess.run(["/usr/bin/osascript", "-e", applescript, "/bin/bash -c " + shlex.quote(script)],
                   check=True, capture_output=True, text=True, timeout=20)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BOARD), **kwargs)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def send_head(self):
        if self.headers.get("Host") != self.origin.removeprefix("http://"):
            self.send_error(403, "Use the local launcher URL.")
            return None
        path = unquote(urlsplit(self.path).path)
        parts = Path(path).parts[1:]
        result = (len(parts) == 4 and parts[0] in SYSTEMS and parts[1] == "results"
                  and len(parts[2]) == 8 and parts[2].isdigit() and parts[3].endswith(".json"))
        preview = len(parts) == 2 and parts[0] == "mock" and parts[1] in {f"{s}.json" for s in SYSTEMS}
        if (path not in STATIC and not result and not preview) or not Path(self.translate_path(path)).resolve().is_relative_to(BOARD):
            self.send_error(404)
            return None
        return super().send_head()

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # No CORS: only this loopback origin can request a terminal launch.
        if (self.headers.get("Host") != self.origin.removeprefix("http://")
                or self.headers.get("Origin") != self.origin
                or self.headers.get("Content-Type") != "application/json"):
            self.reply(403, {"error": "Open the wizard from the local launcher URL."})
            return
        if self.path not in ("/api/preview", "/api/run"):
            self.reply(404, {"error": "Unknown launcher endpoint."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid request size.")
            config = json.loads(self.rfile.read(length))
            if not isinstance(config, dict):
                raise ValueError("Invalid benchmark configuration.")
            script = script_for(config)
            if self.path == "/api/run":
                if config.get("script") != script:
                    raise ValueError("The script changed. Return to setup and review it again.")
                open_terminal(script)
                self.reply(200, {"message": "Opened Terminal. Follow the run there, then return to the leaderboard."})
            else:
                self.reply(200, {"script": script, "can_launch": sys.platform == "darwin"})
        except (ValueError, UnicodeError) as error:
            self.reply(400, {"error": str(error)})
        except (OSError, subprocess.SubprocessError):
            self.reply(503, {"error": "Could not open Terminal. Check macOS Automation access, or copy the script."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/run.html"
        print(f"Benchmark wizard: {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
