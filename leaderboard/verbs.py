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
import re
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from leaderboard import run as micro
from leaderboard import run_macro
from leaderboard.connector import connector_names, load_connector
from leaderboard.run import RunError, scrub
from leaderboard.validate import BOARD, MACHINE_RE, parse_day
from leaderboard.validate_macro import ATTEMPT_RE as RUN_ID_RE, HEX, SECRET_KEY, SECRET_VALUE
from microbench.verbs.grid import AXES, BASE, VERBS, points_for_run
import math

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA40_RE = HEX[40]
SHA64_RE = HEX[64]

ROOT = BOARD.parent
VERB_RUNS = micro.RUNS / "verbs"
VERBS_SUITE = "verbs-1"
VERBS_RE = re.compile(r"[a-z0-9_]+/verbs/\d{8}/bb_[0-9a-f]{8}\.json")
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


def _scrub_deep(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_deep(v) for v in value]
    if isinstance(value, dict):
        return {
            str(k): ("[REDACTED]" if SECRET_KEY.search(str(k)) and v not in ("", None, False) else _scrub_deep(v))
            for k, v in value.items()
        }
    return value


def _has_secret(value) -> bool:
    if isinstance(value, str):
        return bool(SECRET_VALUE.search(value)) or scrub(value) != value
    if isinstance(value, list):
        return any(_has_secret(v) for v in value)
    if isinstance(value, dict):
        for k, v in value.items():
            if SECRET_KEY.search(str(k)) and v not in ("", None, False, "[REDACTED]"):
                return True
            if _has_secret(v):
                return True
    return False


def _terminate_verbs_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def launch_verbs_child(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout=out, stderr="")
    except subprocess.TimeoutExpired:
        _terminate_verbs_child(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        raise subprocess.TimeoutExpired(cmd, timeout or 0.0, output=out)
    except BaseException:
        _terminate_verbs_child(proc)
        raise


def _is_finite_nonneg(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val >= 0


def classify_verbs_stats(
    raw_stats: dict | None,
    *,
    expected_point_ids: list[str],
    exit_code: int,
    timed_out: bool = False,
    scale: int | None = None,
    native_only: bool | None = None,
    reps: int | None = None,
    warmup: int | None = None,
) -> dict:
    reasons: list[str] = []
    if timed_out:
        reasons.append("verb scaling child exceeded deadline")
    if exit_code != 0:
        reasons.append(f"child exited with code {exit_code}")
    if not expected_point_ids:
        reasons.append("expected_points must be a non-empty list of grid point IDs")
    if not isinstance(raw_stats, dict):
        reasons.append("missing or unreadable stats JSON")
        return {"status": "failed" if (timed_out or exit_code != 0) else "unverified", "reasons": reasons}
    if raw_stats.get("status") != "completed":
        reasons.append(f"stats.status is {raw_stats.get('status')!r}, expected 'completed'")
    if scale is not None and "scale" in raw_stats and raw_stats.get("scale") != scale:
        reasons.append(f"stats.scale is {raw_stats.get('scale')!r}, expected {scale!r}")
    if native_only is not None and "native_only" in raw_stats and raw_stats.get("native_only") is not native_only:
        reasons.append(f"stats.native_only is {raw_stats.get('native_only')!r}, expected {native_only!r}")
    if reps is not None and "reps" in raw_stats and raw_stats.get("reps") != reps:
        reasons.append(f"stats.reps is {raw_stats.get('reps')!r}, expected {reps!r}")
    if warmup is not None and "warmup" in raw_stats and raw_stats.get("warmup") != warmup:
        reasons.append(f"stats.warmup is {raw_stats.get('warmup')!r}, expected {warmup!r}")
    points = raw_stats.get("points")
    has_point_failure = False
    allowed_ids = {"control", *expected_point_ids}
    if not isinstance(points, list) or not points:
        reasons.append("stats.points must be a non-empty list")
    else:
        by_id: dict[str, dict] = {}
        for idx, pt in enumerate(points):
            if not isinstance(pt, dict) or not isinstance(pt.get("id"), str):
                reasons.append(f"stats.points[{idx}] is not a valid point object")
                continue
            pid = pt["id"]
            if pid in by_id:
                reasons.append(f"duplicate point {pid!r} in stats.points")
                continue
            if pid not in allowed_ids:
                reasons.append(f"unexpected point {pid!r} in stats.points")
                continue
            by_id[pid] = pt
            st = pt.get("status")
            failed_reps = pt.get("failed", 0)
            if st not in ("ok", "unsupported"):
                has_point_failure = True
                reasons.append(f"point {pid} had status {st!r}")
            elif not isinstance(failed_reps, int) or isinstance(failed_reps, bool) or failed_reps != 0:
                has_point_failure = True
                reasons.append(f"point {pid} had {failed_reps} failed repetition(s)")
            elif st == "ok" and pid != "control":
                lat = pt.get("latency")
                n_val = lat.get("n") if isinstance(lat, dict) else None
                if not isinstance(lat, dict) or not isinstance(n_val, int) or isinstance(n_val, bool) or n_val <= 0:
                    reasons.append(f"point {pid} recorded no timed latencies")
                else:
                    if reps is not None and n_val != reps:
                        reasons.append(f"point {pid} latency.n is {n_val}, expected {reps}")
                    if not _is_finite_nonneg(lat.get("p50")):
                        reasons.append(f"point {pid} latency.p50 must be a finite non-negative number")
                    for qk in ("min", "p10", "p50", "p90", "max"):
                        if qk in lat and not _is_finite_nonneg(lat[qk]):
                            reasons.append(f"point {pid} latency.{qk} must be a finite non-negative number")
                    if all(qk in lat and _is_finite_nonneg(lat[qk]) for qk in ("min", "p10", "p50", "p90", "max")):
                        if not (lat["min"] <= lat["p10"] <= lat["p50"] <= lat["p90"] <= lat["max"]):
                            reasons.append(f"point {pid} latency quantiles are out of order")
                pt_reps = pt.get("reps")
                if pt_reps is not None:
                    if not isinstance(pt_reps, list) or (reps is not None and len(pt_reps) != reps):
                        reasons.append(f"point {pid} reps list must have {reps} timed entries")
                    else:
                        for r_idx, rep in enumerate(pt_reps):
                            if not isinstance(rep, dict) or rep.get("status") not in (None, "OK") or not _is_finite_nonneg(rep.get("latency", 0.0)):
                                reasons.append(f"point {pid} rep[{r_idx}] is invalid")
                                break
                            if "per_delete" in rep:
                                pd = rep["per_delete"]
                                if not isinstance(pd, list) or not pd or not all(_is_finite_nonneg(x) for x in pd):
                                    reasons.append(f"point {pid} rep[{r_idx}].per_delete must be a non-empty list of finite non-negative floats")
                                    break
        missing = [pid for pid in expected_point_ids if pid not in by_id]
        if missing:
            reasons.append(f"missing {len(missing)} expected grid point(s): {', '.join(missing[:5])}")
    if not reasons:
        return {"status": "verified", "reasons": []}
    status = "failed" if (timed_out or exit_code != 0 or has_point_failure) else "unverified"
    return {"status": status, "reasons": reasons}


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
    deadline_min: float = 180.0,
    allow_hosted: bool = False,
    native_only: bool = False,
) -> int:
    if not MACHINE_RE.fullmatch(machine or ""):
        raise RunError(f"machine label {machine!r} must match {MACHINE_RE.pattern}")
    if scale < 1:
        raise RunError("--scale must be a positive integer")
    if reps < 1:
        raise RunError("--reps must be a positive integer")
    if warmup < 0:
        raise RunError("--warmup must be a non-negative integer")
    if deadline_min <= 0:
        raise RunError("--deadline-min must be positive")
    if not native_only and os.environ.get("DBFORK_NATIVE_ONLY"):
        raise RunError("DBFORK_NATIVE_ONLY is set in the environment; unset it or pass --native-only")
    for v in verbs or ():
        if v not in VERBS:
            raise RunError(f"unknown verb {v!r}; choose from {', '.join(VERBS)}")
    if verbs and len(set(verbs)) != len(verbs):
        raise RunError("duplicate verb in --verbs")
    valid_axes = {a.name for a in AXES} | {a.key for a in AXES}
    for a in axes or ():
        if a not in valid_axes:
            raise RunError(f"unknown axis {a!r}")
    if axes and len(set(axes)) != len(axes):
        raise RunError("duplicate axis in --axes")
    expected_point_ids = [pt.id for pt in points_for_run(scale, verbs, axes)]
    if not expected_point_ids:
        raise RunError("no grid points selected for this scale, verbs, and axes")
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
    stop_status = "not_started"
    cleanup = {"status": "not_run", "detail": "run did not reach cleanup"}
    timed_out = False
    proc_rc = -1
    proc_out = ""
    with micro.catch_sigterm():
        try:
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
                else:
                    child_env.pop("DBFORK_NATIVE_ONLY", None)
                try:
                    try:
                        proc = launch_verbs_child(cmd, cwd=ROOT, env=child_env, timeout=deadline_min * 60.0)
                    except TypeError:
                        proc = launch_verbs_child(cmd, cwd=ROOT, env=child_env)
                    proc_rc = proc.returncode
                    proc_out = proc.stdout or ""
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    proc_rc = -signal.SIGALRM
                    proc_out = (exc.output if isinstance(exc.output, str) else (exc.output or b"").decode("utf-8", "replace"))
                finally:
                    cleanup = run_macro.verified_cleanup(system, server)
            finally:
                if started_here:
                    try:
                        with micro.bounded(micro.STOP_TIMEOUT, "server stop"):
                            stopped_ok = micro.lifecycle(sysdir, "stop")
                        stop_status = (
                            "stopped"
                            if (stopped_ok and not run_macro.pid_alive(system) and not run_macro.port_answers(server.scope()))
                            else "failed"
                        )
                    except BaseException:
                        stop_status = "failed"
        except KeyboardInterrupt:
            raise
        (outdir / "verbs.log").write_text(scrub(proc_out))
        stats_file = outdir / f"micro_verbs_{backend_name}_w{scale}_stats.json"
        raw_stats = None
        if stats_file.is_file():
            try:
                raw_stats = json.loads(stats_file.read_text())
            except (OSError, ValueError):
                raw_stats = None
        outcome = classify_verbs_stats(
            raw_stats,
            expected_point_ids=expected_point_ids,
            exit_code=proc_rc,
            timed_out=timed_out,
            scale=scale,
            native_only=native_only,
            reps=reps,
            warmup=warmup,
        )
        prov = run_macro.provenance(snapshot, env)
        if prov.get("source", {}).get("changed_during_run"):
            outcome = {
                "status": "unverified",
                "reasons": [*outcome["reasons"], "tracked sources changed during the run"],
            }
        payload = {
            "suite": VERBS_SUITE,
            "run_id": run_id,
            "system": meta["system"],
            "scale": scale,
            "base": dict(BASE),
            "verbs": list(verbs) if verbs else list(VERBS),
            "axes": list(axes) if axes else None,
            "reps": reps,
            "warmup": warmup,
            "expected_points": expected_point_ids,
            "machine": machine,
            "date": started.date().isoformat(),
            "started": started.isoformat(timespec="seconds"),
            "version": version,
            "native_only": native_only,
            "exit_code": proc_rc,
            "timed_out": timed_out,
            "outcome": outcome,
            "cleanup": cleanup,
            "server": {"started_here": started_here, "stop": stop_status},
            "stats": _scrub_deep(raw_stats) if isinstance(raw_stats, dict) else None,
            **prov,
        }
        dest = run_macro.write_json(out / system / "verbs" / started.strftime("%Y%m%d") / f"{run_id}.json", payload)
        print(
            f"verbs {run_id}: {outcome['status']} (exit {proc_rc}, cleanup {cleanup['status']}, stop {stop_status}); {dest}",
            flush=True,
        )
        expected_stop = "not_started" if is_hosted else "stopped"
        return 0 if (
            proc_rc == 0
            and not timed_out
            and outcome["status"] == "verified"
            and cleanup["status"] == "verified"
            and stop_status == expected_stop
        ) else 1


def validate_verbs(path: Path, board: Path = BOARD) -> list[str]:
    """Return every schema, provenance, cleanup, or completeness problem on a verbs record."""
    problems: list[str] = []
    resolved_board = board.resolve()
    for part in [path, *path.parents]:
        if part == board or part == resolved_board:
            break
        if part.is_symlink():
            return [f"{path}: reached through symlink {part}"]
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(resolved_board)
    except ValueError:
        return [f"{path}: not inside {resolved_board}"]
    board = resolved_board
    if not VERBS_RE.fullmatch(rel.as_posix()):
        return [f"{rel.as_posix()}: path must match <system>/verbs/<YYYYMMDD>/bb_<id>.json"]
    system_key, _, day_dir, filename = rel.parts
    meta_path = board / system_key / "system.json"
    if not meta_path.is_file() or meta_path.is_symlink():
        return [f"{rel.as_posix()}: missing regular system.json for {system_key!r}"]
    try:
        meta = json.loads(meta_path.read_text())
        data = json.loads(resolved.read_text())
    except (OSError, ValueError) as exc:
        return [f"{rel.as_posix()}: unreadable JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"{rel.as_posix()}: top-level value must be a JSON object"]
    if _has_secret(data):
        problems.append("record contains unscrubbed secret material")
    if data.get("suite") != VERBS_SUITE:
        problems.append(f"suite must be {VERBS_SUITE!r}")
    if not isinstance(data.get("run_id"), str) or not RUN_ID_RE.fullmatch(data["run_id"]) or f"{data['run_id']}.json" != filename:
        problems.append("run_id does not match filename")
    if data.get("system") != meta.get("system"):
        problems.append(f"system must match {meta.get('system')!r}")
    scale = data.get("scale")
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 1:
        problems.append("scale must be a positive integer")
    reps = data.get("reps")
    if reps is not None and (not isinstance(reps, int) or isinstance(reps, bool) or reps < 1):
        problems.append("reps must be a positive integer")
    warmup = data.get("warmup")
    if warmup is not None and (not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0):
        problems.append("warmup must be a non-negative integer")
    if not isinstance(data.get("machine"), str) or not MACHINE_RE.fullmatch(data["machine"]):
        problems.append("invalid machine label")
    if not isinstance(data.get("date"), str) or not DATE_RE.fullmatch(data["date"]) or data["date"].replace("-", "") != day_dir:
        problems.append("date does not match folder YYYYMMDD")
    if not isinstance(data.get("version"), str) or not data["version"].strip():
        problems.append("version must be a non-empty string")
    if not isinstance(data.get("native_only"), bool):
        problems.append("native_only must be a boolean")
    if data.get("exit_code") != 0 or data.get("timed_out") is True:
        problems.append("child process did not exit cleanly")
    cleanup = data.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "verified":
        problems.append("cleanup.status must be 'verified'")
    server = data.get("server")
    is_hosted = meta.get("hosted") == "yes"
    expected_stop = "not_started" if is_hosted else "stopped"
    if not isinstance(server, dict) or server.get("stop") != expected_stop or server.get("started_here") is not (not is_hosted):
        problems.append(f"server lifecycle must have started_here={not is_hosted} and stop={expected_stop!r}")
    source = data.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("commit"), str)
        or not SHA40_RE.fullmatch(source["commit"])
        or source.get("dirty") is not False
        or source.get("changed_during_run") is not False
        or not isinstance(source.get("source_sha256"), str)
        or not SHA64_RE.fullmatch(source["source_sha256"])
        or not isinstance(data.get("lock_sha256"), str)
        or not SHA64_RE.fullmatch(data["lock_sha256"])
    ):
        problems.append("source provenance must be clean and unchanged during the run")
    verbs_sel = data.get("verbs")
    axes_sel = data.get("axes")
    valid_axes = {a.name for a in AXES} | {a.key for a in AXES}
    verbs_ok = (
        isinstance(verbs_sel, list)
        and bool(verbs_sel)
        and all(isinstance(v, str) and v in VERBS for v in verbs_sel)
        and len(set(verbs_sel)) == len(verbs_sel)
    )
    if not verbs_ok:
        problems.append("verbs must be a non-empty list of valid verb names without duplicates")
    axes_ok = axes_sel is None or (
        isinstance(axes_sel, list)
        and bool(axes_sel)
        and all(isinstance(a, str) and a in valid_axes for a in axes_sel)
        and len(set(axes_sel)) == len(axes_sel)
    )
    if not axes_ok:
        problems.append("axes must be null or a non-empty list of valid axis names without duplicates")
    expected_pts = (
        [pt.id for pt in points_for_run(scale, verbs_sel, axes_sel)]
        if isinstance(scale, int) and not isinstance(scale, bool) and scale >= 1 and verbs_ok and axes_ok
        else []
    )
    if not expected_pts:
        problems.append("selected scale, verbs, and axes produce an empty grid")
    if "expected_points" in data and data.get("expected_points") != expected_pts:
        problems.append("expected_points does not match selected scale, verbs, and axes")
    check = classify_verbs_stats(
        data.get("stats") if isinstance(data.get("stats"), dict) else None,
        expected_point_ids=expected_pts,
        exit_code=int(data.get("exit_code", -1)) if isinstance(data.get("exit_code"), int) else -1,
        timed_out=bool(data.get("timed_out", False)),
        scale=scale if isinstance(scale, int) and not isinstance(scale, bool) else None,
        native_only=data.get("native_only") if isinstance(data.get("native_only"), bool) else None,
        reps=reps if isinstance(reps, int) and not isinstance(reps, bool) else None,
        warmup=warmup if isinstance(warmup, int) and not isinstance(warmup, bool) else None,
    )
    outcome = data.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("status") != "verified" or check["status"] != "verified":
        problems.append("; ".join(check["reasons"] or ["outcome is not verified"]))
    return problems


def build_verbs(board: Path = BOARD, sources: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Validate and select the newest verbs scaling record per comparable cohort."""
    board = board.resolve()
    candidates: list[Path] = []
    for sysdir in sorted(board.iterdir()):
        if not sysdir.is_dir() or sysdir.is_symlink() or not (sysdir / "system.json").is_file():
            continue
        verbs_dir = sysdir / "verbs"
        if not verbs_dir.is_dir() or verbs_dir.is_symlink():
            continue
        for day_dir in sorted(verbs_dir.iterdir()):
            if not day_dir.is_dir() or day_dir.is_symlink():
                continue
            for file in sorted(day_dir.glob("*.json")):
                rel = file.relative_to(board).as_posix()
                if sources is not None and rel not in sources:
                    continue
                candidates.append(file)
    by_slot: dict[tuple, list[tuple[tuple[str, str, int], str, Path]]] = {}
    excluded: list[dict] = []
    for order_idx, path in enumerate(candidates):
        rel = path.relative_to(board).as_posix()
        problems = validate_verbs(path, board)
        if problems:
            excluded.append({"source": rel, "reason": "; ".join(problems)})
            continue
        data = json.loads(path.read_text())
        slot = (
            data["system"],
            int(data["scale"]),
            data["machine"],
            bool(data.get("native_only", False)),
            tuple(data.get("verbs") or VERBS),
            tuple(data.get("axes") or ()),
            int(data.get("reps", 10)),
            int(data.get("warmup", 3)),
        )
        sort_key = (str(data.get("started") or data["date"]), str(data["date"]), order_idx)
        by_slot.setdefault(slot, []).append((sort_key, rel, path))
    entries: list[dict] = []
    for slot in sorted(by_slot):
        _, rel, path = sorted(by_slot[slot], key=lambda item: item[0])[-1]
        data = json.loads(path.read_text())
        entries.append({
            "source": rel,
            "run_id": data["run_id"],
            "system": data["system"],
            "scale": data["scale"],
            "verbs": data.get("verbs") or list(VERBS),
            "axes": data.get("axes"),
            "reps": int(data.get("reps", 10)),
            "warmup": int(data.get("warmup", 3)),
            "machine": data["machine"],
            "date": data["date"],
            "started": data.get("started"),
            "version": data["version"],
            "native_only": data["native_only"],
            "outcome": data["outcome"],
            "points": (data.get("stats") or {}).get("points", []),
        })
    return entries, excluded


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
    parser.add_argument("--deadline-min", type=float, default=180.0, help="child process deadline in minutes (default: 180)")
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
                deadline_min=args.deadline_min,
                allow_hosted=args.allow_hosted,
                native_only=args.native_only,
            )
    except RunError as e:
        print(f"verbs run failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
