"""Run the paper's 8-verb scaling microbenchmark (microbench.verbs) on one system
under leaderboard server lifecycle, lock, recovery journal, and provenance rules.

From the repository root:
    python -m leaderboard.verbs <system> --scale 1 --machine <label>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from leaderboard import run as micro
from leaderboard import run_macro
from leaderboard.connector import connector_names, load_connector
from leaderboard.run import RunError, scrub
from leaderboard.validate import BOARD, MACHINE_RE
from leaderboard.validate_macro import scrubbed
from microbench.verbs.grid import AXES, BASE, VERBS

ROOT = BOARD.parent
VERB_RUNS = micro.RUNS / "verbs"
BACKEND_ARG = {
    "dolt": "dolt",
    "dolt_mysql": "dolt_mysql",
    "seekdb": "seekdb",
    "matrixone": "matrixone",
    "file_copy": "file_copy",
    "chronos": "chronos",
    "neon": "neon",
    "xata": "xata",
}


def launch_verbs_child(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")


def run(
    system: str,
    scale: int,
    machine: str,
    out: Path,
    *,
    verbs: list[str] | None = None,
    axes: list[str] | None = None,
    reps: int = 10,
    warmup: int = 3,
    allow_hosted: bool = False,
    native_only: bool = False,
) -> int:
    if not MACHINE_RE.fullmatch(machine or ""):
        raise RunError(f"machine label {machine!r} must match {MACHINE_RE.pattern}")
    if scale < 1:
        raise RunError("--scale must be a positive integer")
    for v in verbs or ():
        if v not in VERBS:
            raise RunError(f"unknown verb {v!r}; choose from {', '.join(VERBS)}")
    valid_axes = {a.name for a in AXES} | {a.key for a in AXES}
    for a in axes or ():
        if a not in valid_axes:
            raise RunError(f"unknown axis {a!r}")
    server, sysdir = load_connector(system), BOARD / system
    meta = json.loads((sysdir / "system.json").read_text())
    is_hosted = meta["hosted"] == "yes"
    if is_hosted and not allow_hosted:
        raise RunError("pass --allow-hosted to run verb scaling against a hosted system")
    if is_hosted:
        missing_keys = [k for k in getattr(server, "keys", ()) if not os.environ.get(k)]
        if missing_keys:
            raise RunError(f"{system} requires {', '.join(missing_keys)} in the environment")
    if server.needs_psql and shutil.which("psql") is None:
        raise RunError("psql is not on PATH")
    snapshot = micro.source_snapshot(run_macro.CODE_PATHS)
    if snapshot["status"].strip():
        raise RunError("the working tree has uncommitted changes; commit them first")
    if not is_hosted:
        if run_macro.port_answers(server.scope()):
            raise RunError(f"a server already answers on {server.scope()['host']}:{server.scope()['port']}; stop it first")
        if run_macro.pid_alive(system):
            raise RunError(f"{run_macro.pid_file(system)} names a live process this runner did not start")

    run_id = f"bb_{secrets.token_hex(4)}"
    outdir = VERB_RUNS / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now(dt.UTC)
    env = run_macro.environment()
    started_here = False
    with micro.catch_sigterm():
        try:
            if is_hosted:
                server.preflight()
            else:
                try:
                    micro.start_server(sysdir)
                finally:
                    started_here = run_macro.pid_file(system).exists()
            version = server.version()
            if (leftover := run_macro.verified_cleanup(system, server))["status"] != "verified":
                raise RunError(f"cannot delete what an earlier run left behind: {leftover['detail']}")
            if hasattr(server, "capture_prev_method"):
                server.capture_prev_method()
            run_macro.write_journal(system, {"run_id": run_id, "name": run_id, "scope": server.scope()})
            backend_name = BACKEND_ARG.get(system, system)
            cmd = [
                sys.executable, "-m", "microbench.verbs",
                "--backend", backend_name,
                "--scale", str(scale),
                "--db-name", run_id,
                "--outdir", str(outdir),
                "--reps", str(reps),
                "--warmup", str(warmup),
            ]
            if verbs:
                cmd.extend(["--verbs", ",".join(verbs)])
            if axes:
                cmd.extend(["--axes", ",".join(axes)])
            child_env = dict(os.environ)
            if native_only:
                child_env["DBFORK_NATIVE_ONLY"] = "1"
            proc = launch_verbs_child(cmd, cwd=ROOT, env=child_env)
            (outdir / "verbs.log").write_text(scrub(proc.stdout or ""))
            cleanup = run_macro.verified_cleanup(system, server)
            stats_file = outdir / f"micro_verbs_{backend_name}_w{scale}_stats.json"
            raw_stats = json.loads(stats_file.read_text()) if stats_file.is_file() else None
            payload = {
                "suite": "verbs-1",
                "run_id": run_id,
                "system": meta["system"],
                "scale": scale,
                "base": dict(BASE),
                "machine": machine,
                "date": started.date().isoformat(),
                "version": version,
                "native_only": native_only,
                "exit_code": proc.returncode,
                "cleanup": cleanup,
                "stats": scrubbed(raw_stats) if raw_stats else None,
                **run_macro.provenance(snapshot, env),
            }
            dest = run_macro.write_json(out / system / "verbs" / started.strftime("%Y%m%d") / f"{run_id}.json", payload)
            print(f"verbs {run_id}: exit {proc.returncode}, cleanup {cleanup['status']}; {dest}", flush=True)
            return 0 if proc.returncode == 0 and cleanup["status"] == "verified" else 1
        finally:
            if started_here:
                with micro.bounded(micro.STOP_TIMEOUT, "server stop"):
                    micro.lifecycle(sysdir, "stop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the 8-verb scaling microbenchmark on one system.")
    parser.add_argument("system", choices=connector_names())
    parser.add_argument("--scale", type=int, default=1, help="CH-benCHmark warehouse scale W (default: 1)")
    parser.add_argument("--machine", default=os.environ.get("machine"), help="machine label; defaults to $machine")
    parser.add_argument("--out", type=Path, default=BOARD, help="directory that receives <system>/verbs/")
    parser.add_argument("--verbs", help="comma-separated subset of verbs (default: all 8)")
    parser.add_argument("--axes", help="comma-separated subset of axes (default: all)")
    parser.add_argument("--reps", type=int, default=10, help="timed repetitions per grid point (default: 10)")
    parser.add_argument("--warmup", type=int, default=3, help="untimed warm-up repetitions per grid point (default: 3)")
    parser.add_argument("--allow-hosted", action="store_true", help="allow running against a hosted system")
    parser.add_argument("--native-only", action="store_true", help="run with DBFORK_NATIVE_ONLY=1")
    args = parser.parse_args(argv)
    verbs = [v.strip() for v in args.verbs.split(",") if v.strip()] if args.verbs else None
    axes = [a.strip() for a in args.axes.split(",") if a.strip()] if args.axes else None
    try:
        with micro.exclusive(args.system):
            return run(
                args.system,
                args.scale,
                args.machine,
                args.out,
                verbs=verbs,
                axes=axes,
                reps=args.reps,
                warmup=args.warmup,
                allow_hosted=args.allow_hosted,
                native_only=args.native_only,
            )
    except RunError as e:
        print(f"verbs run failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
