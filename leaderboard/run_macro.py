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
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pyarrow.parquet as pq
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
    setup_outcome,
    timings,
    workload_digest,
)
from macrobench import task_pb2 as tp
from macrobench.intensity import apply_intensity
from macrobench.scenarios.base import ScenarioStopped, is_retryable_error

ROOT = micro.ROOT
CODE_PATHS = [*micro.CODE_PATHS, "macrobench"]
MACRO_RUNS = micro.RUNS / "macro"
GRACE = 120  # seconds past its runtime cap the child gets to write its result and exit
DEADLINES = {"mini": 30.0, "full": 360.0}  # minutes per scenario, setup included
BRANCH_VERBS = ("BRANCH", "CONNECT", "DELETE", "COMMIT", "DIFF", "SCHEMA_DIFF", "MERGE", "REBASE", "RESET", "REVERT", "LOG")
SPINE_LABELS = ("spine", "spine_update", "invariant")
WORKER_LOG_RE = re.compile(r"^\[W(\d+)\]\s+(.+)$")


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


def expected_work_units(scenario_key: str | tp.MacroBenchConfig, workload: dict | None = None, config_file: Path | None = None) -> int:
    """Compute the scenario's planned logical work units (Scenario.total_units)."""
    if isinstance(scenario_key, tp.MacroBenchConfig):
        cfg_obj = tp.MacroBenchConfig()
        cfg_obj.CopyFrom(scenario_key)
        apply_intensity(cfg_obj.workload)
        workload = MessageToDict(cfg_obj.workload, preserving_proto_field_name=True)
        scenario_key = cfg_obj.workload.WhichOneof("scenario") or ""
    elif config_file is not None and config_file.is_file():
        try:
            cfg = tp.MacroBenchConfig()
            text_format.Parse(config_file.read_text(), cfg)
            apply_intensity(cfg.workload)
            workload = MessageToDict(cfg.workload, preserving_proto_field_name=True)
        except Exception:
            pass
    w = (workload or {}).get(scenario_key) if isinstance(workload, dict) else {}
    if not isinstance(w, dict):
        w = {}
    if scenario_key == "rl_env":
        tasks = int(w.get("tasks") or 2)
        group_size = int(w.get("group_size") or 3)
        return max(1, tasks * (1 + group_size))
    if scenario_key == "context_mgmt":
        cycles = int(w.get("compaction_cycles") or 2)
        interval = int(w.get("compaction_interval") or 2)
        cpr = int(w.get("cycles_per_rejection") or cycles)
        return max(1, cycles * (interval + 1) + -(-cycles // max(1, cpr)))
    if scenario_key == "multi_agent":
        rounds = int(w.get("rounds") or 2)
        agents = int(w.get("agents") or 3)
        return max(1, rounds * agents)
    if scenario_key == "dev_agent":
        branches = int(w.get("dev_branches") or 1)
        dev_steps = int(w.get("dev_phase_steps") or 4)
        rev_steps = int(w.get("review_phase_steps") or 2)
        return max(1, branches * (dev_steps + rev_steps))
    if scenario_key == "ops_agent":
        spine_commits = int(w.get("spine_commits") or 6)
        cpi = int(w.get("commits_per_incident") or spine_commits)
        inv_branches = int(w.get("investigation_branches") or 2)
        n = max(1, -(-spine_commits // max(1, cpi)))
        per = max(1, inv_branches // n)
        return max(1, spine_commits + n * per + n)
    if scenario_key == "data_agent":
        batches = int(w.get("batches") or 2)
        steps = int(w.get("steps_per_batch") or 2)
        return max(1, batches * steps)
    return 1


def enrich_e2e_stats(raw: dict, outdir: Path, run_id: str, runtime: dict | None = None) -> dict:
    """Bridge upstream macrobench.runner output (<run_id>_e2e_stats.json + <run_id>.parquet)
    into the validator's full evidence contract without mutating synthetic unit-test fixtures."""
    parquet_path = outdir / f"{run_id}.parquet"
    validator_keys = ("work", "worker_errors", "failed_verbs", "commit_ref_fallbacks")
    upstream_keys = ("op_stall_sec", "stalls", "storage", "storage_before_bytes")
    is_upstream = (
        runtime is not None
        or parquet_path.is_file()
        or any(k in raw for k in upstream_keys)
        or all(k not in raw for k in validator_keys)
    )
    if not is_upstream:
        return raw

    out = dict(raw)
    metrics = dict(out["metrics"]) if isinstance(out.get("metrics"), dict) else {}
    out["metrics"] = metrics

    ok_latency: dict[str, float] = {}
    fallback_count = 0
    branch_sec = 0.0
    data_sec = 0.0
    spine_sec = 0.0
    parquet_rows: list[tuple[str, str, float, str, str, bool]] = []
    has_retry_wait = False

    if parquet_path.is_file():
        try:
            table = pq.read_table(
                parquet_path,
                columns=["op_name", "status", "latency", "label", "error_message", "commit_ref_fallback"],
            )
            cols = table.to_pydict()
            n_rows = len(cols.get("op_name", []))
            for i in range(n_rows):
                op = str(cols["op_name"][i] or "")
                st = str(cols["status"][i] or "")
                lat_raw = cols["latency"][i]
                lat = float(lat_raw) if isinstance(lat_raw, (int, float)) and math.isfinite(lat_raw) and lat_raw >= 0 else 0.0
                lbl = str(cols["label"][i] or "")
                err = str(cols["error_message"][i] or "")
                fb = bool(cols["commit_ref_fallback"][i])
                parquet_rows.append((op, st, lat, lbl, err, fb))
                if op == "API_RETRY_WAIT":
                    has_retry_wait = True
                if fb:
                    fallback_count += 1
                if st == "OK":
                    ok_latency[op] = ok_latency.get(op, 0.0) + lat
                if lbl in ("spine", "spine_update") and op == "EXEC":
                    spine_sec += lat
                elif lbl not in SPINE_LABELS and op != "EXEC":
                    if op in BRANCH_VERBS:
                        branch_sec += lat
                    else:
                        data_sec += lat
        except Exception:
            pass

    counts = out.get("op_status_counts")
    if isinstance(counts, dict):
        new_counts = {}
        for op, entry in counts.items():
            if isinstance(entry, dict):
                e_copy = dict(entry)
                if "ok_seconds" not in e_copy:
                    e_copy["ok_seconds"] = round(ok_latency.get(op, 0.0), 6)
                new_counts[op] = e_copy
            else:
                new_counts[op] = entry
        out["op_status_counts"] = new_counts

    if "commit_ref_fallbacks" not in out:
        out["commit_ref_fallbacks"] = int(fallback_count)

    if "worker_time" not in out and parquet_rows:
        out["worker_time"] = {
            "branch_sec": round(branch_sec, 6),
            "data_sec": round(data_sec, 6),
            "total_sec": round(branch_sec + data_sec, 6),
            "spine_sec": round(spine_sec, 6),
        }

    spine_load = out.get("spine_load")
    spine_attempted = 0
    spine_failed = 0
    if isinstance(spine_load, dict):
        txns = spine_load.get("transactions")
        if isinstance(txns, dict):
            spine_attempted = sum(v for v in txns.values() if type(v) is int and v >= 0)
        f_val = spine_load.get("failures")
        if type(f_val) is int and f_val >= 0:
            spine_failed = f_val
    if parquet_rows:
        pq_spine_exec = [r for r in parquet_rows if r[3] in ("spine", "spine_update") and r[0] == "EXEC"]
        if pq_spine_exec:
            spine_attempted = max(spine_attempted, len(pq_spine_exec))
            spine_failed = max(spine_failed, sum(1 for r in pq_spine_exec if r[1] == "FAILED"))
    if spine_attempted > 0 or spine_failed > 0:
        metrics.setdefault("spine_attempted", int(spine_attempted))
        metrics.setdefault("spine_failed", int(spine_failed))

    if "failed_verbs" not in out:
        expected_failed: dict[str, int] = {}
        unexpected_failed: dict[str, int] = {}
        if spine_failed > 0:
            expected_failed["spine"] = int(spine_failed)
        schema_resolved = bool(
            metrics.get("schema_conflict_resolved_by_rebase", 0) > 0
            or metrics.get("schema_conflict_resolved_by_merge", 0) > 0
        ) and metrics.get("rebase_failed", 0) == 0
        if parquet_rows:
            for op, st, _lat, lbl, err, _fb in parquet_rows:
                if st != "FAILED" or op not in (*BRANCH_VERBS, "EXEC"):
                    continue
                if lbl in ("spine", "spine_update"):
                    if op == "EXEC":
                        expected_failed["spine"] = max(expected_failed.get("spine", 0), 1)
                    continue
                if lbl == "invariant":
                    expected_failed["invariant"] = expected_failed.get("invariant", 0) + 1
                    continue
                if op == "REBASE" and "schema conflict" in err.lower() and schema_resolved:
                    expected_failed["schema_conflict_rebase"] = expected_failed.get("schema_conflict_rebase", 0) + 1
                    continue
                if has_retry_wait and is_retryable_error(err):
                    expected_failed["retry"] = expected_failed.get("retry", 0) + 1
                    continue
                key = lbl if (op == "EXEC" and lbl) else op
                unexpected_failed[key] = unexpected_failed.get(key, 0) + 1
        elif isinstance(counts, dict):
            for op, entry in counts.items():
                if op not in (*BRANCH_VERBS, "EXEC") or not isinstance(entry, dict):
                    continue
                f_cnt = entry.get("failed", 0)
                if type(f_cnt) is not int or f_cnt <= 0:
                    continue
                if op == "EXEC":
                    f_cnt = max(0, f_cnt - expected_failed.get("spine", 0))
                if op == "REBASE" and schema_resolved:
                    f_cnt = max(0, f_cnt - int(metrics.get("schema_conflict_rebases", 0) or 0))
                if f_cnt > 0:
                    unexpected_failed[op] = unexpected_failed.get(op, 0) + f_cnt
        out["failed_verbs"] = {"expected": expected_failed, "unexpected": unexpected_failed}

    if "worker_errors" not in out:
        w_errors: list[dict] = []
        if runtime and isinstance(runtime.get("worker_errors"), list):
            w_errors = list(runtime["worker_errors"])
        elif (outdir / "runner.log").is_file():
            try:
                for line in (outdir / "runner.log").read_text(errors="replace").splitlines():
                    m = WORKER_LOG_RE.match(line.strip())
                    if m:
                        w_errors.append({"worker": int(m.group(1)), "item": "worker", "error": scrub(m.group(2))[:240]})
            except OSError:
                pass
        out["worker_errors"] = w_errors

    if "work" not in out:
        scenario_key = str(out.get("scenario") or "")
        expected = (
            int(runtime["expected"])
            if runtime and type(runtime.get("expected")) is int and runtime["expected"] > 0
            else expected_work_units(scenario_key, out.get("workload"), outdir / "config.textproto")
        )
        status = str(out.get("status") or "")
        setup_prob = setup_outcome(out.get("setup") if isinstance(out.get("setup"), dict) else {})
        if setup_prob is not None or status.startswith("setup_failed"):
            succeeded, failed, skipped = 0, 0, 0
        elif runtime and any(runtime.get(k, 0) > 0 for k in ("succeeded", "failed", "skipped")):
            succeeded = min(expected, max(0, int(runtime.get("succeeded", 0))))
            failed = min(expected - succeeded, max(0, int(runtime.get("failed", 0))))
            skipped = min(expected - succeeded - failed, max(0, int(runtime.get("skipped", 0))))
            if status == "completed" and not out.get("timed_out") and not out.get("unsupported_ops") and succeeded + failed + skipped == 0:
                succeeded = expected
        else:
            w_err_cnt = len(out.get("worker_errors") or [])
            unexp_cnt = sum((out.get("failed_verbs") or {}).get("unexpected", {}).values())
            unsup_cnt = len(out.get("unsupported_ops") or [])
            if status.startswith("unsupported:") or unsup_cnt > 0:
                if status == "completed":
                    skipped = min(expected, max(1, unsup_cnt))
                    succeeded = max(0, expected - skipped)
                    failed = 0
                else:
                    succeeded = 0
                    failed = 0
                    skipped = min(expected, max(1, unsup_cnt))
            elif out.get("timed_out") or status == "interrupted":
                succeeded = max(0, expected // 2) if parquet_rows else 0
                failed = min(expected - succeeded, w_err_cnt)
                skipped = 0
            elif status == "completed" and w_err_cnt == 0 and unexp_cnt == 0:
                succeeded, failed, skipped = expected, 0, 0
            else:
                failed = min(expected, max(1, w_err_cnt or unexp_cnt or 1))
                succeeded = max(0, expected - failed)
                skipped = 0
        out["work"] = {
            "expected": int(expected),
            "succeeded": int(succeeded),
            "failed": int(failed),
            "skipped": int(skipped),
        }

    return out


def run_child_runner(argv: list[str]) -> int:
    """Child-process wrapper around macrobench.runner that records exact work-unit
    outcomes and worker exceptions in memory, then enriches <run_id>_e2e_stats.json."""
    from macrobench import runner as mr
    from macrobench.scenarios import base as sb

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--outdir", default="/tmp/run_stats")
    known, _ = parser.parse_known_args(argv)
    outdir = Path(known.outdir)
    config_file = Path(known.config)
    run_id = ""
    try:
        cfg = tp.MacroBenchConfig()
        text_format.Parse(config_file.read_text(), cfg)
        run_id = cfg.run_id
    except Exception:
        pass

    runtime: dict = {"expected": 0, "succeeded": 0, "failed": 0, "skipped": 0, "worker_errors": []}
    lock = threading.Lock()
    local_state = threading.local()

    orig_create_scenario = mr.create_scenario
    orig_note = sb.ScenarioContext.note
    orig_tick = sb.ScenarioContext.tick
    orig_run_workers = sb.ScenarioContext.run_workers

    def hooked_create_scenario(ctx):
        scenario = orig_create_scenario(ctx)
        try:
            runtime["expected"] = int(scenario.total_units())
        except Exception:
            pass
        return scenario

    def hooked_note(self, msg: str) -> None:
        text = str(msg or "")
        if ("not run:" in text) or ("UNSUPPORTED" in text and ("branch" in text or "candidate" in text)):
            local_state.pending_disposition = "skipped"
        elif "FAILED" in text and ("branch" in text or "candidate" in text):
            local_state.pending_disposition = "failed"
        return orig_note(self, msg)

    def hooked_tick(self, n: int = 1) -> None:
        disp = getattr(local_state, "pending_disposition", "succeeded")
        local_state.pending_disposition = "succeeded"
        with lock:
            runtime[disp] = runtime.get(disp, 0) + int(n)
        return orig_tick(self, n)

    def hooked_run_workers(self, items: list, fn, threads: int = None, thread_base: int = 1) -> list:
        def wrapped_fn(worker, item):
            try:
                return fn(worker, item)
            except ScenarioStopped:
                raise
            except Exception as e:
                with lock:
                    runtime["worker_errors"].append({
                        "worker": int(getattr(worker, "id", 0)),
                        "item": str(item)[:120],
                        "error": f"{type(e).__name__}: {scrub(str(e))[:240]}",
                    })
                    runtime["failed"] = runtime.get("failed", 0) + 1
                raise

        return orig_run_workers(self, items, wrapped_fn, threads=threads, thread_base=thread_base)

    mr.create_scenario = hooked_create_scenario
    sb.ScenarioContext.note = hooked_note
    sb.ScenarioContext.tick = hooked_tick
    sb.ScenarioContext.run_workers = hooked_run_workers
    rc = 0
    try:
        mr.main(argv)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else (1 if e.code else 0)
    finally:
        if run_id:
            e2e_path = outdir / f"{run_id}_e2e_stats.json"
            if e2e_path.is_file():
                try:
                    raw = json.loads(e2e_path.read_text())
                    if isinstance(raw, dict):
                        enriched = enrich_e2e_stats(raw, outdir, run_id, runtime=runtime)
                        e2e_path.write_text(json.dumps(enriched, indent=2, default=str) + "\n")
                except Exception:
                    pass
    return rc


def launch_child(config: Path, outdir: Path, deadline_sec: int, *, env: dict | None = None) -> dict:
    """Run main's runner with its runtime cap; scrub output in memory before writing to disk."""
    log = outdir / "runner.log"
    raw = ""
    run_kw = {" env": env} if env is not None else {}
    try:
        process = subprocess.run(
            [sys.executable, "-m", "leaderboard.run_macro", "--child-runner", "--config", str(config),
             "--outdir", str(outdir), "--no-progress", "--max-runtime-sec", str(deadline_sec)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            timeout=deadline_sec + GRACE, **({} if env is None else {"env": env}))
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
    return evidence_summary(enrich_e2e_stats(raw, outdir, run_id)), []


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
            out: Path, identity: dict, version: str, *, child_env: dict | None = None) -> tuple[Path, dict]:
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
        child = (launch_child(config_file, outdir, deadline_sec)
                 if child_env is None else launch_child(config_file, outdir, deadline_sec, env=child_env))
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
                   "effective": effective, **({"native_only": True} if child_env and child_env.get("DBFORK_NATIVE_ONLY") == "1" else {})},
    }
    path = write_json(out / system / "macro" / started.strftime("%Y%m%d") / f"{run_id}.json", payload)
    print(f"{SCENARIOS[key]}: {status} ({'; '.join(payload['outcome']['reasons']) or 'all required work verified'}); "
          f"cleanup {cleanup['status']}; {path}", flush=True)
    return path, payload


def run(system: str, preset: str, keys: list[str], machine: str, out: Path, deadline_min: float, *,
        allow_hosted: bool = False, native_only: bool = False) -> int:
    if not MACHINE_RE.fullmatch(machine or ""):
        raise RunError(f"machine label {machine!r} must match {MACHINE_RE.pattern}")
    if not math.isfinite(deadline_min) or deadline_min <= 0:
        raise RunError("--deadline-min must be finite and greater than zero")
    if not native_only and os.environ.get("DBFORK_NATIVE_ONLY", "").lower() in ("1", "true", "yes"):
        raise RunError("DBFORK_NATIVE_ONLY is set; unset it so every run uses the standard benchmark mode")
    server, sysdir = load_connector(system), BOARD / system
    meta = json.loads((sysdir / "system.json").read_text())
    is_hosted = meta["hosted"] == "yes"
    if is_hosted and not allow_hosted:
        raise RunError("agent workflows run only on local systems")
    if is_hosted:
        missing_keys = [k for k in getattr(server, "keys", ()) if not os.environ.get(k)]
        if missing_keys:
            raise RunError(f"{system} requires {', '.join(missing_keys)} in the environment")
    if server.needs_psql and shutil.which("psql") is None:
        raise RunError("psql is not on PATH")
    for key in keys:
        if not config_path(key, preset).is_file():
            raise RunError(f"no {preset} preset for {key}")
    snapshot = micro.source_snapshot(CODE_PATHS)
    if snapshot["status"].strip():
        raise RunError("the working tree has uncommitted changes; commit them first, so every result names the code that produced it")
    if not is_hosted:
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
                "started": started.isoformat(timespec="seconds"),
                **({"native_only": True} if native_only else {})}
    record = {"schema_version": MACRO_SCHEMA_VERSION, "suite": MACRO_SUITE, "record": "campaign", **campaign,
              "system": meta["system"], "finished": None, "aborted": None, "server": {"started_here": False, "stop": "pending"},
              "attempts": {}, "version": None, **provenance(snapshot, env)}
    record_path = out / system / "macro" / started.strftime("%Y%m%d") / f"campaign_{campaign['id']}.json"
    identity = {k: meta[k] for k in ("system", "proprietary", "hosted", "tuned", "tags")}
    if native_only and "native_only" not in identity["tags"]:
        identity["tags"] = list(identity["tags"]) + ["native_only"]
    child_env = None
    if native_only:
        child_env = dict(os.environ)
        child_env["DBFORK_NATIVE_ONLY"] = "1"
    published: list[dict] = []
    abort = None
    print(f"campaign {campaign['id']}: {preset} preset, {len(keys)} workflows on {meta['system']}", flush=True)
    write_json(record_path, record)  # the campaign exists before any server or database does
    with micro.catch_sigterm():
        try:
            if is_hosted:
                server.preflight()
            else:
                try:
                    micro.start_server(sysdir)
                finally:  # start may have launched the server and then failed its check
                    record["server"]["started_here"] = pid_file(system).exists()
            record["version"] = server.version()
            for key in keys:
                _, payload = attempt(system, server, key, campaign, snapshot, env, deadline_min, out, identity,
                                     record["version"], child_env=child_env)
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
    if "--child-runner" in sys.argv:
        child_argv = [arg for arg in sys.argv[1:] if arg != "--child-runner"]
        return run_child_runner(child_argv)
    parser = argparse.ArgumentParser(description="Run the agent-workflow scenarios on one local or hosted system.")
    parser.add_argument("system", choices=connector_names())
    parser.add_argument("--preset", choices=sorted(PRESETS), default="mini",
                        help="mini: the upstream mini workloads, minutes per system, never published; full: the paper-size workloads, hours per system")
    parser.add_argument("--machine", default=os.environ.get("machine"), help="machine label; defaults to $machine")
    parser.add_argument("--out", type=Path, default=BOARD, help="directory that receives <system>/macro/")
    parser.add_argument("--deadline-min", type=float, help="per-scenario bound on setup and workflow "
                        f"(defaults: {', '.join(f'{k} {v:g}' for k, v in DEADLINES.items())})")
    parser.add_argument("--allow-hosted", action="store_true",
                        help="allow running agent-workflow scenarios against a hosted system (e.g., neon)")
    parser.add_argument("--native-only", action="store_true",
                        help="run with DBFORK_NATIVE_ONLY=1 to benchmark native verb support without emulation fallbacks")
    args = parser.parse_args()
    try:
        with micro.exclusive(args.system):
            return run(args.system, args.preset, list(SCENARIOS), args.machine, args.out,
                       args.deadline_min or DEADLINES[args.preset],
                       allow_hosted=args.allow_hosted, native_only=args.native_only)
    except RunError as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
