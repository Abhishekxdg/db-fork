"""Run main's agent-workflow scenarios (S1-S6) on one local system, one bounded
child process per scenario, and publish one immutable attempt record each.

From the repository root:
    python -m leaderboard.run_macro <system> --preset mini --machine <label>

Validation comes first: the runner refuses to start, and writes nothing, unless the
sources are committed and the machine, system, tools and port are fit to run. A
campaign always runs all six workflows.

The parent owns the per-system lock, the server it starts (never one that already
answers on the port or whose pid file names a live process), a durable recovery
journal written before the child can create anything, the child's deadline, its
termination, the deletion of the run database and the proof that it is gone, and
atomic publication. The child, main's macrobench.runner, owns the database
content: schema, generated seed data and the workflow. It gets a unique bb_<id>
database name with cleanup off, so only the parent deletes. A scenario's outcome
is classified from the evidence the child wrote and how the child ended, never
from its exit code; its cleanup verdict is recorded separately.

Attempts land in <out>/<system>/macro/<YYYYMMDD>/<attempt id>.json and are never
rewritten. The campaign record <out>/<system>/macro/<YYYYMMDD>/campaign_<id>.json
is written before the server starts and rewritten as the campaign proceeds, so an
abort before the first child, a stop failure or an interruption stays visible.
Raw child output (log, e2e stats, Parquet, effective config) stays in
leaderboard/.runs/macro/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from google.protobuf import text_format
from google.protobuf.json_format import MessageToDict

from leaderboard import run as micro
from leaderboard.connector import Connector, connector_names, load_connector
from leaderboard.run import RunError, scrub
from leaderboard.validate import BOARD, MACHINE_RE, sha256
from leaderboard.validate_macro import (
    ATTEMPT_RE,
    MACRO_SCHEMA_VERSION,
    MACRO_SUITE,
    PRESETS,
    SCENARIOS,
    classify,
    config_path,
    evidence_summary,
    timings,
    workload_digest,
)
from macrobench import task_pb2 as tp

ROOT = micro.ROOT
CODE_PATHS = [*micro.CODE_PATHS, "macrobench"]
MACRO_RUNS = micro.RUNS / "macro"
GRACE = 120  # seconds past its runtime cap the child gets to write its result and exit
DEADLINES = {"mini": 30.0, "full": 360.0}  # minutes per scenario, setup included


def effective_config(base: Path, backend: int, run_id: str) -> tuple[tp.MacroBenchConfig, dict]:
    """The upstream preset bound to this attempt, and the complete normalized workload
    it describes: the same config with only the run id, backend selector and database
    name cleared. Its digest identifies the workload across systems."""
    cfg = tp.MacroBenchConfig()
    text_format.Parse(base.read_text(), cfg)
    if cfg.database_setup.WhichOneof("source") == "existing_db":
        raise RunError(f"{base.name} reuses an existing database; the child must generate its own")
    cfg.run_id = run_id
    cfg.backend = backend
    cfg.database_setup.db_name = run_id
    cfg.database_setup.cleanup = False
    cfg.database_setup.generated.SetInParent()
    identity = tp.MacroBenchConfig()
    identity.CopyFrom(cfg)
    identity.run_id, identity.backend, identity.database_setup.db_name = "", 0, ""
    return cfg, MessageToDict(identity, preserving_proto_field_name=True)


def environment() -> dict:
    """The concrete machine, with honest unknowns."""
    cpu = None
    try:
        if sys.platform == "darwin":
            cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                 timeout=5, check=True).stdout.strip() or None
        elif Path("/proc/cpuinfo").is_file():
            cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                        if line.startswith("model name")), None)
    except (OSError, subprocess.SubprocessError):
        cpu = None
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        memory = None
    return {"cpu": cpu, "cpus": os.cpu_count(), "memory_bytes": memory if isinstance(memory, int) and memory > 0 else None}


def port_answers(scope: dict) -> bool:
    try:
        with socket.create_connection((scope["host"], int(scope["port"])), timeout=1):
            return True
    except OSError:
        return False


def pid_file(system: str) -> Path:
    """Where the system's start script records the server it launched."""
    return micro.RUNS / f"{system}.pid"


def pid_alive(system: str) -> bool:
    try:
        pid = int(pid_file(system).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by someone else
        return True
    return True


def write_journal(system: str, record: dict) -> None:
    """Private, scoped and durable before the child can create anything."""
    with open(micro.journal(system), "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        json.dump(record, handle)
        handle.flush()
        os.fsync(handle.fileno())


def launch_child(config: Path, outdir: Path, deadline_sec: int) -> dict:
    """Run main's runner with its runtime cap; scrub output in memory before writing to disk."""
    log = outdir / "runner.log"
    raw = ""
    try:
        process = subprocess.run(
            [sys.executable, "-m", "macrobench.runner", "--config", str(config), "--outdir", str(outdir),
             "--no-progress", "--max-runtime-sec", str(deadline_sec)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            timeout=deadline_sec + GRACE)
        raw = process.stdout or ""
        exit_code, ended = process.returncode, "exited"
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        raw = out.decode(errors="replace") if isinstance(out, bytes) else out
        exit_code, ended = None, "killed"
    finally:
        try:
            with open(log, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
                handle.write(scrub(raw))
        except OSError:
            pass
    return {"exit_code": exit_code, "ended": ended, "error": None, "deadline_sec": deadline_sec, "log_tail": log_tail(outdir)}


def log_tail(outdir: Path) -> str:
    try:
        return scrub("\n".join((outdir / "runner.log").read_text(errors="replace").splitlines()[-25:]))[-3000:]
    except OSError:
        return ""


def read_evidence(outdir: Path, run_id: str) -> tuple[dict | None, list[str]]:
    path = outdir / f"{run_id}_e2e_stats.json"
    if not path.is_file():
        return None, []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return None, [f"the child's result is unreadable: {scrub(str(e))[:200]}"]
    if not isinstance(raw, dict) or raw.get("run_id") != run_id:
        return None, ["the child's result does not belong to this attempt"]
    return evidence_summary(raw), []


def verified_cleanup(system: str, server: Connector) -> dict:
    """Delete what the journal names and prove it is gone. The journal is deleted
    only after that proof, so it survives a failed delete, a database that is still
    there, and a check that could not run."""
    path = micro.journal(system)
    if not path.exists():
        return {"status": "verified", "detail": "no journal, nothing to delete"}
    try:
        with micro.bounded(micro.VENDOR_TIMEOUT, "cleanup"):
            record = json.loads(path.read_text())
            if record.get("scope") != server.scope():
                raise RunError("the recovery journal names a different or missing target scope; restore its target before retrying")
            name = record.get("name")
            if not isinstance(name, str) or not ATTEMPT_RE.fullmatch(name):
                raise RunError("the recovery journal has an invalid resource name")
            try:
                server.delete(name)
            except Exception as e:
                return {"status": "failed", "detail": f"delete failed, journal kept: {scrub(str(e))[:300]}"}
            present = server.exists(name)
    except Exception as e:
        return {"status": "unknown", "detail": f"could not verify deletion, journal kept: {scrub(str(e))[:300]}"}
    if present:
        return {"status": "failed", "detail": f"{name} still exists after delete; journal kept for recovery"}
    path.unlink()
    return {"status": "verified", "detail": f"{name} deleted and absent"}


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=1) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def source_digest(snapshot: dict) -> str:
    """One digest for the exact source state, committed or not, normalized relative to the repository root."""
    root = ROOT.resolve()
    items = sorted(
        (Path(k).resolve().relative_to(root).as_posix()
         if Path(k).is_absolute() and Path(k).resolve().is_relative_to(root) else Path(k).as_posix(), v)
        for k, v in snapshot["files"].items()
    )
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def provenance(snapshot: dict, env: dict) -> dict:
    return {
        "source": {"commit": snapshot["commit"], "dirty": bool(snapshot["status"].strip()),
                   "changed_during_run": micro.source_snapshot(CODE_PATHS) != snapshot, "source_sha256": source_digest(snapshot)},
        "lock_sha256": snapshot["files"][str(ROOT / "uv.lock")],
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "environment": env,
    }


def attempt(system: str, server: Connector, key: str, campaign: dict, snapshot: dict, env: dict, deadline_min: float,
            out: Path, identity: dict, version: str) -> tuple[Path, dict]:
    """One scenario: journal, bounded child, evidence, verified cleanup, publication.
    Once the journal exists, cleanup runs and an attempt is published on every path,
    including a child that could not be started and an interruption."""
    run_id = f"bb_{secrets.token_hex(4)}"
    outdir = MACRO_RUNS / run_id
    outdir.mkdir(parents=True)
    base = config_path(key, campaign["preset"])
    cfg, effective = effective_config(base, server.backend, run_id)
    config_file = outdir / "config.textproto"
    config_file.write_text(text_format.MessageToString(cfg))
    if (leftover := verified_cleanup(system, server))["status"] != "verified":
        raise RunError(f"cannot delete what an earlier run left behind: {leftover['detail']}")
    if hasattr(server, "capture_prev_method"):
        server.capture_prev_method()
    write_journal(system, {"run_id": run_id, "name": run_id, "scope": server.scope()})
    started = dt.datetime.now(dt.UTC)
    deadline_sec = int(deadline_min * 60)
    print(f"{SCENARIOS[key]}: attempt {run_id}, deadline {deadline_min:g} min", flush=True)
    child, evidence, problems = None, None, []
    try:
        child = launch_child(config_file, outdir, deadline_sec)
        evidence, problems = read_evidence(outdir, run_id)
    except KeyboardInterrupt:  # recorded here; the campaign re-raises once the attempt is on file
        child = {"exit_code": None, "ended": "interrupted", "error": None, "deadline_sec": deadline_sec, "log_tail": log_tail(outdir)}
    except Exception as e:
        if child is None:  # the child never ran
            child = {"exit_code": None, "ended": "launch_failed", "error": f"{type(e).__name__}: {scrub(str(e))[:300]}",
                     "deadline_sec": deadline_sec, "log_tail": log_tail(outdir)}
        else:  # it ran, but its result cannot be read
            evidence, problems = None, [f"the child's result could not be read: {type(e).__name__}: {scrub(str(e))[:200]}"]
    cleanup = verified_cleanup(system, server)
    status, reasons = classify(evidence, child)
    elapsed, setup = timings(evidence)
    payload = {
        "schema_version": MACRO_SCHEMA_VERSION,
        "suite": MACRO_SUITE,
        "attempt_id": run_id,
        "campaign": campaign,
        **identity,
        "scenario": key,
        "date": started.date().isoformat(),
        "machine": campaign["machine"],
        "version": version,
        "outcome": {"status": status, "reasons": problems + reasons, "elapsed_sec": elapsed, "setup_sec": setup,
                    "completion_sec": elapsed if status == "verified" else None},
        "cleanup": cleanup,
        "child": child,
        "evidence": evidence,
        **provenance(snapshot, env),
        "config": {"preset": campaign["preset"], "base_config_sha256": sha256(base), "config_sha256": workload_digest(effective),
                   "effective": effective},
    }
    path = write_json(out / system / "macro" / started.strftime("%Y%m%d") / f"{run_id}.json", payload)
    print(f"{SCENARIOS[key]}: {status} ({'; '.join(payload['outcome']['reasons']) or 'all required work verified'}); "
          f"cleanup {cleanup['status']}; {path}", flush=True)
    return path, payload


def run(system: str, preset: str, keys: list[str], machine: str, out: Path, deadline_min: float) -> int:
    if not MACHINE_RE.fullmatch(machine or ""):
        raise RunError(f"machine label {machine!r} must match {MACHINE_RE.pattern}")
    if not math.isfinite(deadline_min) or deadline_min <= 0:
        raise RunError("--deadline-min must be finite and greater than zero")
    if os.environ.get("DBFORK_NATIVE_ONLY", "").lower() in ("1", "true", "yes"):
        raise RunError("DBFORK_NATIVE_ONLY is set; unset it so every run uses the standard benchmark mode")
    server, sysdir = load_connector(system), BOARD / system
    meta = json.loads((sysdir / "system.json").read_text())
    if meta["hosted"] == "yes":
        raise RunError("agent workflows run only on local systems")
    if server.needs_psql and shutil.which("psql") is None:
        raise RunError("psql is not on PATH")
    for key in keys:
        if not config_path(key, preset).is_file():
            raise RunError(f"no {preset} preset for {key}")
    snapshot = micro.source_snapshot(CODE_PATHS)
    if snapshot["status"].strip():
        raise RunError("the working tree has uncommitted changes; commit them first, so every result names the code that produced it")
    if port_answers(server.scope()):
        raise RunError(f"a server already answers on {server.scope()['host']}:{server.scope()['port']}; "
                       "this runner only benchmarks servers it starts, so stop it first")
    if pid_alive(system):
        raise RunError(f"{pid_file(system)} names a live process this runner did not start; stop it or remove the stale file")
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    MACRO_RUNS.mkdir(parents=True, exist_ok=True)
    env = environment()
    started = dt.datetime.now(dt.UTC)
    campaign = {"id": secrets.token_hex(4), "preset": preset, "scenarios": keys, "machine": machine,
                "started": started.isoformat(timespec="seconds")}
    record = {"schema_version": MACRO_SCHEMA_VERSION, "suite": MACRO_SUITE, "record": "campaign", **campaign,
              "system": meta["system"], "finished": None, "aborted": None, "server": {"started_here": False, "stop": "pending"},
              "attempts": {}, "version": None, **provenance(snapshot, env)}
    record_path = out / system / "macro" / started.strftime("%Y%m%d") / f"campaign_{campaign['id']}.json"
    identity = {k: meta[k] for k in ("system", "proprietary", "hosted", "tuned", "tags")}
    published: list[dict] = []
    abort = None
    print(f"campaign {campaign['id']}: {preset} preset, {len(keys)} workflows on {meta['system']}", flush=True)
    write_json(record_path, record)  # the campaign exists before any server or database does
    with micro.catch_sigterm():
        try:
            try:
                micro.start_server(sysdir)
            finally:  # start may have launched the server and then failed its check
                record["server"]["started_here"] = pid_file(system).exists()
            record["version"] = server.version()
            for key in keys:
                _, payload = attempt(system, server, key, campaign, snapshot, env, deadline_min, out, identity, record["version"])
                published.append(payload)
                record["attempts"][key] = payload["attempt_id"]
                write_json(record_path, record)
                if payload["child"]["ended"] == "interrupted":
                    raise KeyboardInterrupt
                if payload["cleanup"]["status"] != "verified":
                    abort = f"stopped after {SCENARIOS[key]}: cleanup {payload['cleanup']['status']}"
                    break
        except KeyboardInterrupt:
            abort = "interrupted"
            raise
        except Exception as e:
            abort = f"{type(e).__name__}: {(scrub(str(e)).splitlines() or [''])[0][:300]}" if not isinstance(e, RunError) else str(e)
        finally:
            if record["server"]["started_here"]:
                try:
                    with micro.bounded(micro.STOP_TIMEOUT, "server stop"):
                        if not micro.lifecycle(sysdir, "stop"):
                            raise RunError(f"{system}: the server did not stop")
                    record["server"]["stop"] = "stopped"
                except Exception as e:
                    record["server"]["stop"] = "failed"
                    abort = abort or f"server stop failed: {scrub(str(e))[:200]}"
            else:
                record["server"]["stop"] = "not_started"
            record["aborted"] = abort
            record["finished"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            record["source"]["changed_during_run"] = micro.source_snapshot(CODE_PATHS) != snapshot
            write_json(record_path, record)
            if abort:
                print(f"campaign {campaign['id']} aborted: {abort}", file=sys.stderr, flush=True)
    outcomes = [p["outcome"]["status"] for p in published]
    print(f"campaign {campaign['id']}: {len(published)} of {len(keys)} attempted, "
          f"{outcomes.count('verified')} verified, {len(keys) - len(published)} not run; {record_path}", flush=True)
    return 0 if not abort and len(published) == len(keys) and all(o == "verified" for o in outcomes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent-workflow scenarios on one local system.")
    parser.add_argument("system", choices=connector_names())
    parser.add_argument("--preset", choices=sorted(PRESETS), default="mini",
                        help="mini: the upstream mini workloads, minutes per system, never published; full: the paper-size workloads, hours per system")
    parser.add_argument("--machine", default=os.environ.get("machine"), help="machine label; defaults to $machine")
    parser.add_argument("--out", type=Path, default=BOARD, help="directory that receives <system>/macro/")
    parser.add_argument("--deadline-min", type=float, help="per-scenario bound on setup and workflow "
                        f"(defaults: {', '.join(f'{k} {v:g}' for k, v in DEADLINES.items())})")
    args = parser.parse_args()
    try:
        with micro.exclusive(args.system):
            return run(args.system, args.preset, list(SCENARIOS), args.machine, args.out, args.deadline_min or DEADLINES[args.preset])
    except RunError as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
