"""Run the leaderboard suite on one system and publish one result file.

From the repository root:
    python -m leaderboard.run <system> --machine <label>

<system> names a folder under leaderboard/systems with a connector.py, so the
runner only loads code that lives in this repository. The result lands in
<out>/<system>/results/<YYYYMMDD>/<machine>.json only after cleanup succeeded.
From the server start on, a failure publishes
{"error": ...} in its place, so a stale success never stays current. A preflight
failure publishes nothing.

Hosted systems read their API keys from the environment at launch. Everything a
run creates is named after its database, bb_<run id>,
and cleanup deletes by that name, so even a killed run leaves nothing behind
once the next run of that system starts with the same target configuration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import secrets
import select
import shutil
import signal
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2

import dblib.result_collector as rc
from dblib import result_pb2 as rslt
from leaderboard.connector import Connector, connector_names, load_connector, parse_system, system_dir
from leaderboard.validate import BOARD, MACHINE_RE, SCHEMA_VERSION, sha256
from microbench import task2_pb2 as tp
from microbench.operations.crud import (
    InsertOperation,
    RangeReadOperation,
    RangeUpdateOperation,
    ReadOperation,
    UpdateOperation,
)
from microbench.runner2 import (
    BackendInfo,
    BenchmarkConfig,
    SharedBranchManager,
    WorkerContext,
    register_all_operations,
)

ROOT = BOARD.parent
RUNS = BOARD / ".runs"
# What a run imports or reads. A change here, tracked or not, makes the run dirty.
# Results, macro attempts, the generated site, the legacy generated data files and
# the publication manifest are outputs or website configuration that no run reads,
# so none of them ever does.
CODE_PATHS = ["dblib", "microbench", "util", "leaderboard", "db_setup", "pyproject.toml", "uv.lock", "build_protos.py",
              ":!leaderboard/*/results/*", ":!leaderboard/*/macro/*", ":!leaderboard/*/verbs/*", ":!leaderboard/.site/*", ":!leaderboard/publish.json",
              ":!leaderboard/data.js", ":!leaderboard/macro-data.js"]
POINT_OPS = {"read": ReadOperation, "insert": InsertOperation, "update": UpdateOperation}
RANGE_OPS = {"range_read": RangeReadOperation, "range_update": RangeUpdateOperation}
SCRIPT_TIMEOUT = 900  # seconds for an install, start, or check script
VENDOR_TIMEOUT = 1200  # seconds for a hosted preflight, and for cleanup to delete what a run created
STOP_TIMEOUT = 60


class RunError(Exception):
    """The run cannot produce a valid result."""


class DeadlineExpired(BaseException):
    """Abort the run, bypassing handlers that turn operation failures into null cells."""


def wait_for_postgres(conn) -> None:
    """Use interruptible Python waits, not blocking libpq calls; propagate Ctrl-C."""
    while (state := conn.poll()) != psycopg2.extensions.POLL_OK:
        if state == psycopg2.extensions.POLL_READ:
            select.select([conn.fileno()], [], [])
        elif state == psycopg2.extensions.POLL_WRITE:
            select.select([], [conn.fileno()], [])
        else:
            raise psycopg2.OperationalError(f"unexpected PostgreSQL poll state: {state}")


@contextmanager
def bounded(seconds: float, label: str):
    """Bound socket, SQL, and subprocess waits on this Unix-only, single-threaded runner."""
    def expired(*_):
        raise DeadlineExpired(f"{label} passed its {seconds:g} s deadline")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_callback = psycopg2.extensions.get_wait_callback()
    psycopg2.extensions.set_wait_callback(wait_for_postgres)
    # Repeat during stack unwinding so a row's finally/delete cannot stall the abort.
    signal.setitimer(signal.ITIMER_REAL, seconds, 1)
    try:
        yield
    except DeadlineExpired as e:
        raise RunError(str(e)) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        psycopg2.extensions.set_wait_callback(previous_callback)


def lifecycle(sysdir: Path, step: str) -> bool:
    """Run a system's install, start, check, or stop script. Hosted systems have none."""
    script = sysdir / step
    if not script.exists():
        return True
    env = {**os.environ, "PYTHON": sys.executable}
    # check is polled while the server starts, so its failures stay quiet.
    out = subprocess.DEVNULL if step == "check" else None
    return subprocess.run([str(script)], env=env, stdout=out, stderr=out,
                          timeout=STOP_TIMEOUT if step == "stop" else SCRIPT_TIMEOUT).returncode == 0


def start_server(sysdir: Path) -> None:
    if not (lifecycle(sysdir, "install") and lifecycle(sysdir, "start")):
        raise RunError(f"{sysdir.name}: install or start failed")
    deadline = time.monotonic() + 60
    while not lifecycle(sysdir, "check"):
        if time.monotonic() > deadline:
            raise RunError(f"{sysdir.name}: server failed its check for 60 s")
        time.sleep(0.5)


@contextmanager
def catch_sigterm():
    """Turn SIGTERM into KeyboardInterrupt so finally blocks clean up and stop the server."""
    def handler(*_):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


DEFAULT_ALPHA_LEASE_TTL_SEC = 86400


def check_alpha_lease(runs_dir: Path = RUNS) -> None:
    """Reject direct or batch runs when an active remote alpha sync/run session owns the worker under another token."""
    owner_candidates = [runs_dir / "alpha.owner"]
    if runs_dir.parent.parent.name == "db-fork":
        owner_candidates.append(runs_dir.parent.parent.parent / "db-fork.alpha.owner")
    caller_token = os.environ.get("DBFORK_ALPHA_TOKEN", "")
    now_ts = time.time()
    for owner_path in owner_candidates:
        try:
            if not owner_path.is_file():
                continue
            raw = owner_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw or ":" not in raw or any(ch.isspace() for ch in raw):
            raise RunError("another alpha sync/benchmark session owns this worker")
        parts = raw.split(":")
        if len(parts) not in (2, 3) or not parts[0]:
            raise RunError("another alpha sync/benchmark session owns this worker")
        owner_token = parts[0]
        try:
            owner_ts = float(parts[1].strip())
        except ValueError:
            raise RunError("another alpha sync/benchmark session owns this worker") from None
        if not math.isfinite(owner_ts) or owner_ts <= 0:
            raise RunError("another alpha sync/benchmark session owns this worker")
        if len(parts) == 2:
            lease_ttl = float(DEFAULT_ALPHA_LEASE_TTL_SEC)
        else:
            if not parts[2].strip():
                raise RunError("another alpha sync/benchmark session owns this worker")
            try:
                lease_ttl = float(parts[2].strip())
            except ValueError:
                raise RunError("another alpha sync/benchmark session owns this worker") from None
            if not math.isfinite(lease_ttl) or lease_ttl <= 0:
                raise RunError("another alpha sync/benchmark session owns this worker")
        if now_ts - owner_ts < lease_ttl:
            if not caller_token or not secrets.compare_digest(owner_token, caller_token):
                raise RunError("another alpha sync/benchmark session owns this worker")


@contextmanager
def exclusive(system: str):
    """Hold a machine-wide and per-system lock, so two runs never share a worker, a journal, or a server."""
    RUNS.mkdir(parents=True, exist_ok=True)
    check_alpha_lease(RUNS)
    with (
        (RUNS / "alpha.lock").open("a+", encoding="utf-8") as alpha_fh,
        (RUNS / "batch.lock").open("a+", encoding="utf-8") as batch_fh,
        (RUNS / "run.lock").open("a+", encoding="utf-8") as global_lock,
        (RUNS / f"{system}.lock").open("a+", encoding="utf-8") as lock,
    ):
        try:
            fcntl.flock(alpha_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            check_alpha_lease(RUNS)
            if not os.environ.get("DBFORK_ALPHA_TOKEN"):
                raise RunError("another alpha sync/benchmark session is in progress on this machine") from None
        else:
            fcntl.flock(alpha_fh, fcntl.LOCK_UN)
        check_alpha_lease(RUNS)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError(f"another run of {system} is in progress") from None
        try:
            fcntl.flock(global_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError("another benchmark run is in progress on this machine") from None
        try:
            fcntl.flock(batch_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            batch_fh.seek(0)
            parts = batch_fh.read().strip().split()
            owner_token = parts[1] if len(parts) >= 2 else ""
            caller_token = os.environ.get("DBFORK_BATCH_TOKEN", "")
            if not owner_token or not caller_token or not secrets.compare_digest(owner_token, caller_token):
                raise RunError("another benchmark batch worker is in progress on this machine") from None
        else:
            fcntl.flock(batch_fh, fcntl.LOCK_UN)
        yield


def journal(system: str) -> Path:
    """Privately names a run's resources and target, until deletion succeeds."""
    return RUNS / f"{system}.journal.json"


def clean_up(system: str, server: Connector) -> Exception | None:
    """Delete everything named after the database the journal names, then delete
    the journal. The journal stays if the delete fails. Reports instead of raising."""
    try:  # an unreadable journal fails the run too, and stays for a person to inspect
        if not journal(system).exists():
            return None
        record = json.loads(journal(system).read_text())
        if record.get("scope") != server.scope():
            raise RunError("recovery journal has a different or missing target scope; restore its target before retrying")
        if not re.fullmatch(r"bb_[0-9a-f]{8}", record["name"]):
            raise RunError("recovery journal has an invalid resource name")
        server.delete(record["name"])
        if server.exists(record["name"]) is True:
            raise RunError(f"{record['name']} still exists after delete; journal kept for recovery")
        journal(system).unlink()
    except Exception as e:  # the caller turns this into the run's error record
        return e
    return None


def scrub(text: str) -> str:
    """Mask the password of any connection URI, even an empty one, so no message or record carries one."""
    return re.sub(r"(://[^:/@\s]+):[^@/\s]*@", r"\1:***@", text)


def task_config(backend: int, db: str, suite: dict) -> BenchmarkConfig:
    task = tp.TaskConfig(run_id=db, backend=backend, table_name=suite["table"], num_threads=1)
    task.database_setup.db_name = db
    # Validation wants one operation; every row brings its own operation and batch.
    task.operation_benchmark.operation = tp.OperationType.READ
    task.operation_benchmark.num_ops = 1
    task.operation_benchmark.range_config.range_size = suite["range_size"]
    return BenchmarkConfig(task)


def root(ctx: WorkerContext) -> str:
    """The branch a run starts on: main or the backend's default branch."""
    return ctx.backend_info.default_branch_name


def count_rows(ctx: WorkerContext, tables: dict[str, int]) -> dict[str, int]:
    return {t: ctx.run_sql(f"SELECT count(*) FROM {t}", timed=False)[0][0] for t in tables}


def round_trip_ms(ctx: WorkerContext) -> float:
    """The median of 20 SELECT 1 round trips: the network cost every hosted operation pays."""
    times = []
    for _ in range(20):
        start = time.perf_counter()
        ctx.run_sql("SELECT 1", timed=False)
        times.append(time.perf_counter() - start)
    return round(statistics.median(times) * 1000, 3)


def drop_branch(ctx: WorkerContext, name: str) -> None:
    """Delete a branch from the root, whether or not the step that made it finished."""
    ctx.connect(root(ctx), timed=False)
    if name in ctx.db_tools.list_branches():
        ctx.db_tools.delete(name, timed=False, raise_on_error=True)


def verify(ctx: WorkerContext, expected: dict[str, int], root_ref: str) -> None:
    """Row counts on the root and on a new branch must match the frozen dump."""
    if (on_root := count_rows(ctx, expected)) != expected:
        raise RunError(f"row counts on the root differ from the dump: {on_root}")
    ctx.db_tools.branch("probe", from_ref=root_ref, timed=False, raise_on_error=True)
    ctx.connect("probe", timed=False)
    on_probe = count_rows(ctx, expected)
    drop_branch(ctx, "probe")
    if on_probe != expected:
        raise RunError(f"a new branch does not inherit the loaded data: {on_probe}")


def build_chain(ctx: WorkerContext, length: int, root_ref: str) -> list[str]:
    """A chain of branches, each created untimed from the one before."""
    chain, parent = [], root_ref
    for i in range(1, length + 1):
        name = f"chain_{i}"
        ctx.db_tools.branch(name, from_ref=parent, timed=False, raise_on_error=True)
        ctx.connect(name, timed=False)
        parent = name
        chain.append(name)
    ctx.connect(root(ctx), timed=False)
    return chain


def batch_total(collector: rc.ResultCollector, expected: int, op_type: int) -> float | None:
    """A try's cell: the summed timed calls, or None unless every call succeeded."""
    if collector.failed_operations or any(r.status != rslt.OpStatus.OK for r in collector.results):
        return None
    rows = [r for r in collector.results if r.op_type == op_type]
    if len(rows) != expected:
        return None
    return round(sum(r.latency for r in rows), 6)


# Each cell function below turns any failure into a null cell. That is the
# recovery: the try is recorded as failed, and its branch is removed.


def first_query(ctx: WorkerContext, name: str, tip_ref: str, table: str) -> float | None:
    """Create, connect, and read one row, as one span."""
    try:
        start = time.perf_counter()
        ctx.db_tools.branch(name, from_ref=tip_ref, timed=False, raise_on_error=True)
        ctx.connect(name, timed=False)
        ctx.run_sql(f"SELECT * FROM {table} LIMIT 1", timed=False)
        return round(time.perf_counter() - start, 6)
    except Exception:
        return None
    finally:
        drop_branch(ctx, name)


def timed_create(ctx: WorkerContext, name: str, tip_ref: str) -> float | None:
    ctx.result_collector.reset()
    try:
        ctx.db_tools.branch(name, from_ref=tip_ref, timed=True, raise_on_error=True)
        return batch_total(ctx.result_collector, 1, rslt.OpType.BRANCH)
    except Exception:
        return None
    finally:
        drop_branch(ctx, name)


def timed_connects(ctx: WorkerContext, chain: list[str], batch: int) -> float | None:
    ctx.result_collector.reset()
    try:
        for k in range(batch):
            ctx.connect(chain[k % len(chain)], timed=True)
        return batch_total(ctx.result_collector, batch, rslt.OpType.CONNECT)
    except Exception:
        return None
    finally:
        ctx.connect(root(ctx), timed=False)


def data_row(ctx: WorkerContext, operation, name: str, tip_ref: str, batch: int, tries: int) -> list[float | None]:
    """All tries of one data operation, on one fresh branch off the tip."""
    ctx.db_tools.branch(name, from_ref=tip_ref, timed=False, raise_on_error=True)
    try:
        ctx.connect(name, timed=False)
        ctx.clear_pk_cache()
        cells = []
        for trial in range(tries):
            # Fixes this try's key choices. main's row generator is unseeded, so values still vary.
            ctx.rnd.seed(f"{ctx.seed}:{name}:{trial}")
            ctx._modified_keys.clear()
            ctx.result_collector.reset()
            try:
                for _ in range(batch):
                    operation.execute(ctx)
                cells.append(batch_total(ctx.result_collector, batch, operation.get_operation_type()))
            except Exception:
                cells.append(None)
        return cells
    finally:
        drop_branch(ctx, name)


def run_row(ctx: WorkerContext, row: dict, chain: list, suite: dict, max_batch: int | None) -> list[float | None]:
    op, tries, tip_ref = row["op"], suite["tries"], chain[-1]
    batch = min(row["batch"], max_batch) if max_batch else row["batch"]
    if op == "time_to_first_query":
        return [first_query(ctx, f"ttfq_{t}", tip_ref, suite["table"]) for t in range(1, tries + 1)]
    if op == "branch_create":
        return [timed_create(ctx, f"create_{t}", tip_ref) for t in range(1, tries + 1)]
    if op == "branch_connect":
        return [timed_connects(ctx, chain, batch) for _ in range(tries)]
    if op in POINT_OPS:
        operation = POINT_OPS[op](suite["table"])
    else:
        operation = RANGE_OPS[op](suite["table"], suite["range_size"])
    return data_row(ctx, operation, f"row_{op}", tip_ref, batch, tries)


def measure(server: Connector, info: BackendInfo, db: str, suite: dict, max_batch: int | None,
            deadline: float) -> tuple[str, float, list]:
    """Verify the load, build the chain, and run every row.
    Returns the server version, the round-trip baseline in ms, and the cells."""
    register_all_operations()
    collector = rc.ResultCollector(run_id=db, output_dir=str(RUNS))
    ctx = WorkerContext(task_config(server.backend, db, suite), info, 0, suite["seed"], collector,
                        SharedBranchManager(), None, [])
    with ctx:
        tools = ctx.db_tools
        version = str(ctx.run_sql(server.version_sql, timed=False)[0][0])
        rtt_ms = round_trip_ms(ctx)
        root_ref = ctx.current_branch
        expected = suite["dataset"]["row_counts"]
        verify(ctx, expected, root_ref)
        chain = build_chain(ctx, suite["chain_length"], root_ref)
        cells = []
        for row in suite["rows"]:
            if time.monotonic() > deadline:
                raise RunError("the run passed its deadline")
            cells.append(run_row(ctx, row, chain, suite, max_batch))
        ctx.connect(chain[-1], timed=False)
        if (on_tip := count_rows(ctx, expected)) != expected:
            raise RunError(f"row writes reached the chain's tip: {on_tip}")
        ctx.connect(root(ctx), timed=False)
        for name in reversed(chain):  # leaf first
            tools.delete(name, timed=False, raise_on_error=True)
    return version, rtt_ms, cells


def to_json(payload: dict) -> str:
    """Pretty JSON with one result row per line, as ClickBench writes it."""
    if "result" not in payload:
        return json.dumps(payload, indent=2) + "\n"
    head = json.dumps({k: v for k, v in payload.items() if k != "result"}, indent=2)[:-2]
    rows = ",\n".join(f"    {json.dumps(r)}" for r in payload["result"])
    return f'{head},\n  "result": [\n{rows}\n  ]\n}}\n'


def publish(out: Path, system: str, machine: str, day: dt.date, payload: dict) -> Path:
    path = out / system / "results" / day.strftime("%Y%m%d") / f"{machine}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(to_json(payload))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # gone after a successful replace; a partial file otherwise
    return path


def source_snapshot(code_paths: list[str], *extra: Path) -> dict:
    """Capture effective inputs, including edits to an already-dirty working tree."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                              check=True, timeout=30).stdout

    files = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *code_paths)
    paths = {ROOT / name for name in files.split("\0") if name} | set(extra)
    return {"commit": git("rev-parse", "HEAD").strip(),
            "status": git("status", "--porcelain", "--", *code_paths),
            "files": {str(p): sha256(p) if p.is_file() else None for p in paths}}


def run(system: str, machine: str, out: Path, max_batch: int | None, allow_dirty: bool, deadline_min: float,
        client_location: str | None) -> Path:
    if not MACHINE_RE.fullmatch(machine or ""):
        raise RunError(f"machine label {machine!r} must match {MACHINE_RE.pattern}")
    if not math.isfinite(deadline_min) or deadline_min <= 0:
        raise RunError("--deadline-min must be finite and greater than zero")
    if os.environ.get("DBFORK_NATIVE_ONLY", "").lower() in ("1", "true", "yes"):
        raise RunError("DBFORK_NATIVE_ONLY is set; unset it so every run uses the standard benchmark mode")
    server, sysdir = load_connector(system), system_dir(system, BOARD)
    meta_path = sysdir / "system.json"
    meta_bytes = meta_path.read_bytes()
    meta = parse_system(meta_bytes, meta_path)
    hosted = meta["hosted"] == "yes"
    if hosted and not (client_location or "").strip():
        raise RunError("a hosted system needs --client-location, where this machine runs, such as 'US East'")
    suite_path = BOARD / "suite.json"
    suite_bytes = suite_path.read_bytes()
    suite = json.loads(suite_bytes)
    dump = ROOT / suite["dataset"]["path"]
    snapshot = source_snapshot(CODE_PATHS, dump)
    digests = snapshot["files"]
    if (digests[str(suite_path)] != hashlib.sha256(suite_bytes).hexdigest()
            or digests[str(meta_path)] != hashlib.sha256(meta_bytes).hexdigest()):
        raise RunError("suite or system metadata changed during preflight")
    if digests[str(dump)] != suite["dataset"]["sha256"]:
        raise RunError(f"{dump} does not match the digest in suite.json")
    if server.needs_psql and shutil.which("psql") is None:
        raise RunError("psql is not on PATH")
    dirty = bool(snapshot["status"].strip())
    if dirty and not allow_dirty:
        raise RunError("the working tree has uncommitted changes")
    if hosted and (missing := [key for key in meta["credentials"] if not os.environ.get(key)]):
        raise RunError(f"{', '.join(missing)} not set in the process environment")
    if hosted:
        try:  # the keys work and the account reaches its project, before anything is created
            with bounded(VENDOR_TIMEOUT, "preflight"):
                server.preflight()
        except Exception as e:
            raise RunError(f"{system} preflight failed: {scrub(str(e))}") from e
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    if hosted:  # bound every statement, since a stalled network call would block the run forever
        os.environ["PGOPTIONS"] = os.environ.get("PGOPTIONS", "") + " -c statement_timeout=120s"

    RUNS.mkdir(parents=True, exist_ok=True)
    run_id = secrets.token_hex(4)
    db, started = f"bb_{run_id}", dt.datetime.now(dt.UTC)
    failure: Exception | None = None
    with catch_sigterm():
        try:
            deadline = time.monotonic() + deadline_min * 60
            with bounded(deadline_min * 60, "run"):
                start_server(sysdir)
                if leftover := clean_up(system, server):
                    raise RunError(f"cannot delete what an earlier run left behind: {leftover}")
                # Private, scoped, and durable before any create; a killed run remains recoverable.
                with open(journal(system), "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as record:
                    json.dump({"run_id": run_id, "name": db, "scope": server.scope()}, record)
                    record.flush()
                    os.fsync(record.fileno())
                t0 = time.perf_counter()
                info = server.provision(db)
                provision_time = time.perf_counter() - t0
                t0 = time.perf_counter()
                server.load(info, db, dump)
                load_time = time.perf_counter() - t0
                version, rtt_ms, cells = measure(server, info, db, suite, max_batch, deadline)
        except Exception as e:  # recorded below as the run's error, after cleanup
            failure = e
        finally:
            try:
                with bounded(VENDOR_TIMEOUT, "cleanup"):
                    cleanup_failure = clean_up(system, server)
                    failure = failure or cleanup_failure
            except Exception as e:
                failure = failure or e
            finally:
                try:
                    with bounded(STOP_TIMEOUT, "server stop"):
                        if not lifecycle(sysdir, "stop"):
                            raise RunError(f"{system}: the server did not stop")
                except Exception as e:
                    failure = failure or e

    if failure is None:
        try:
            if source_snapshot(CODE_PATHS, dump) != snapshot:
                raise RunError("source, dataset, or Git HEAD changed during the run; results were not published")
        except Exception as e:
            failure = e

    if failure:
        detail = (scrub(str(failure)).splitlines() or [""])[0][:300]
        publish(
            out,
            system,
            machine,
            started.date(),
            {
                "error": f"{type(failure).__name__}: {detail}",
                "execution_profile": os.environ.get("DBFORK_EXECUTION_PROFILE") or "local_single_host",
            },
        )
        raise RunError(f"{system} failed: {detail}") from failure
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "system": meta["system"],
        "date": started.date().isoformat(),
        "machine": machine,
        "proprietary": meta["proprietary"],
        "hosted": meta["hosted"],
        "tuned": meta["tuned"],
        "tags": meta["tags"],
        "version": version,
        "suite": suite["suite"],
        "suite_sha256": digests[str(suite_path)],
        "seed": suite["seed"],
        "commit": snapshot["commit"],
        "dirty": dirty,
        "dataset_sha256": suite["dataset"]["sha256"],
        "python": platform.python_version(),
        "lock_sha256": digests[str(ROOT / "uv.lock")],
        "execution_profile": os.environ.get("DBFORK_EXECUTION_PROFILE") or "local_single_host",
        "provision_time": round(provision_time, 6),
        "load_time": round(load_time, 6),
    }
    if hosted:
        payload |= {"region": server.region, "rtt_ms": rtt_ms, "client_location": client_location.strip(),
                    "service": server.service}
    if max_batch:
        payload["max_batch"] = max_batch
    payload["result"] = cells
    return publish(out, system, machine, started.date(), payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the leaderboard suite on one system.")
    parser.add_argument("system", choices=connector_names())
    parser.add_argument("--machine", default=os.environ.get("machine"), help="machine label; defaults to $machine")
    parser.add_argument("--client-location", default=os.environ.get("client_location"),
                        help="where this machine runs, recorded for hosted systems; defaults to $client_location")
    parser.add_argument("--out", type=Path, default=BOARD, help="directory that receives <system>/results/")
    parser.add_argument("--max-batch", type=int, help="cap every batch for a short run; the validator rejects it")
    parser.add_argument("--allow-dirty", action="store_true", help="allow uncommitted changes; the validator rejects it")
    parser.add_argument("--deadline-min", type=float, default=120.0,
                        help="bound startup through measurement; cleanup has a separate deadline")
    args = parser.parse_args()
    try:
        with exclusive(args.system):
            path = run(args.system, args.machine, args.out, args.max_batch, args.allow_dirty, args.deadline_min,
                       args.client_location)
    except RunError as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
