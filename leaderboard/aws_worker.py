from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator

from leaderboard.connector import connector_names, load_connector
from leaderboard.run import (
    MACHINE_RE,
    RUNS,
    RunError,
    bounded,
    catch_sigterm,
    check_alpha_lease,
)
from leaderboard.run_macro import port_answers
from leaderboard.validate import BOARD, validate as validate_micro
from leaderboard.validate_macro import (
    validate as validate_macro_attempt,
    validate_campaign as validate_macro_campaign,
)


DEFAULT_LOCAL_SYSTEMS = ("dolt", "dolt_mysql", "matrixone", "seekdb", "file_copy", "chronos")
DEFAULT_HOSTED_SYSTEMS = ("neon", "xata")
DEFAULT_BATCH_DEADLINE_MIN = 720.0
POWEROFF_TIMEOUT_SEC = 60.0
INTERRUPT_EXIT_CODES = {130, 137, 143}
SYSTEMD_CONFLICT_UNITS = (
    "postgresql",
    "pg18-bench",
    "mysql",
    "mariadb",
    "dolt",
    "doltgres",
    "matrixone",
    "seekdb",
    "unattended-upgrades",
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
)


class DeadlineExceededError(RuntimeError):
    """Raised when the batch wall-clock deadline expires."""


def runnable_systems(board: Path = BOARD) -> dict[str, dict]:
    return {
        name: json.loads((board / name / "system.json").read_text(encoding="utf-8"))
        for name in connector_names(board)
    }


def parse_systems_arg(raw: str) -> list[str]:
    items = [tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()]
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def resolve_systems(
    systems_arg: str,
    lane: str,
    client_location: str,
    known: dict[str, dict],
) -> list[str]:
    if systems_arg.strip():
        selected = parse_systems_arg(systems_arg)
    else:
        selected = [s for s in DEFAULT_LOCAL_SYSTEMS if s in known]
        if client_location.strip():
            if lane == "macro":
                selected.extend(s for s in ("neon",) if s in known and s not in selected)
            else:
                selected.extend(s for s in DEFAULT_HOSTED_SYSTEMS if s in known and s not in selected)
    if not selected:
        raise RuntimeError("no systems selected")
    unknown = [s for s in selected if s not in known]
    if unknown:
        raise RuntimeError(f"unknown system(s): {', '.join(unknown)}")
    hosted_selected = [s for s in selected if known[s].get("hosted") == "yes"]
    if hosted_selected and not client_location.strip():
        raise RuntimeError(
            f"hosted system(s) {', '.join(hosted_selected)} require --client-location"
        )
    return selected


def configure_data_mount(data_mount: str, env: dict[str, str] | None = None) -> None:
    if not data_mount.strip():
        return
    target_env = os.environ if env is None else env
    mount = Path(data_mount).expanduser().resolve()
    mount.mkdir(parents=True, exist_ok=True)
    defaults = {
        "DOLT_DATA_DIR": str(mount / "doltgres" / "databases"),
        "DOLT_MYSQL_DATA_DIR": str(mount / "dolt" / "databases"),
        "MO_DATA_DIR": str(mount / "mo" / "matrixone" / "mo-data"),
        "SEEKDB_DATA_DIR": str(mount / "seekdb" / "store"),
        "PGSQL_DATA_DIR": str(mount / "pg_file_copy" / "data"),
        "CHRONOS_DATA_DIR": str(mount / "pg_chronos" / "data"),
    }
    for key, val in defaults.items():
        if not target_env.get(key):
            target_env[key] = val


@contextmanager
def batch_lock(runs_dir: Path = RUNS) -> Iterator[str]:
    """Acquire an exclusive batch lock and verify no single-system run.lock or foreign alpha lease is active."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        check_alpha_lease(runs_dir)
    except RunError as exc:
        raise RuntimeError(str(exc)) from None
    bpath = runs_dir / "batch.lock"
    rpath = runs_dir / "run.lock"
    token = secrets.token_hex(16)
    with open(bpath, "a+", encoding="utf-8") as bfh:
        try:
            fcntl.flock(bfh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another leaderboard batch worker holds {bpath}") from None
        with open(rpath, "a+", encoding="utf-8") as rfh:
            try:
                fcntl.flock(rfh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"another leaderboard benchmark run holds {rpath}") from None
            finally:
                try:
                    fcntl.flock(rfh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        bfh.seek(0)
        bfh.truncate()
        bfh.write(f"{os.getpid()} {token}\n")
        bfh.flush()
        os.fsync(bfh.fileno())
        try:
            yield token
        finally:
            try:
                bfh.seek(0)
                bfh.truncate()
                bfh.flush()
            except OSError:
                pass


def resolve_shutdown_bin() -> str:
    found = shutil.which("shutdown")
    if found:
        return found
    for candidate in ("/sbin/shutdown", "/usr/sbin/shutdown"):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("--poweroff requires 'shutdown' on PATH or in /sbin or /usr/sbin")


def _runas_permits_root(runas_spec: str) -> bool:
    user_part = runas_spec.split(":", 1)[0]
    users = [u.strip() for u in user_part.split(",") if u.strip()]
    return any(u in ("root", "ALL", "0", "#0") for u in users)


def _sudo_rule_matches_shutdown(cmd_spec: str, shutdown_bin: str) -> bool:
    spec = cmd_spec.strip()
    if not spec:
        return False
    if spec == "ALL":
        return True
    parts = spec.split()
    cmd_path, cmd_args = parts[0], parts[1:]
    if cmd_path != shutdown_bin:
        return False
    if not cmd_args or cmd_args == ["-h", "now"] or cmd_args == ["*"]:
        return True
    return False


def _sudo_output_permits_shutdown(stdout: str, shutdown_bin: str = "/sbin/shutdown") -> bool:
    """Return True only if command-scoped sudo output proves an effective root NOPASSWD / !authenticate rule for shutdown_bin -h now."""
    text = stdout.strip()
    if not text or "not allowed" in text.lower():
        return False

    matched: bool | None = None

    # 1. Verbose `sudo -ll` format (`Sudoers entry:` / `RunAsUsers:` / `Options:` / `Commands:`)
    if "Options:" in text or "Sudoers entry" in text:
        blocks = re.split(r"(?=^Sudoers entry:?)", text, flags=re.MULTILINE)
        for block in blocks:
            if not block.strip():
                continue
            auth_free: bool | None = None
            runas_ok = True
            in_commands = False
            block_matches = False
            for raw_line in block.splitlines():
                line = raw_line.strip()
                if line.startswith("RunAsUsers:"):
                    in_commands = False
                    runas_ok = _runas_permits_root(line.split(":", 1)[1])
                elif line.startswith("Options:"):
                    in_commands = False
                    opts = [o.strip() for o in line.split(":", 1)[1].split(",") if o.strip()]
                    for opt in opts:
                        if opt in ("!authenticate", "NOPASSWD"):
                            auth_free = True
                        elif opt in ("authenticate", "PASSWD"):
                            auth_free = False
                elif line.startswith(("Commands:", "Matched command:")):
                    in_commands = True
                    rest = line.split(":", 1)[1].strip()
                    if rest and _sudo_rule_matches_shutdown(rest, shutdown_bin):
                        block_matches = True
                elif in_commands and line:
                    if re.match(r"^[A-Za-z][A-Za-z0-9_ ]*:", line):
                        in_commands = False
                    elif _sudo_rule_matches_shutdown(line, shutdown_bin):
                        block_matches = True
            if runas_ok and block_matches and auth_free is not None:
                matched = auth_free

    # 2. Standard `(runas) NOPASSWD: <shutdown_bin> -h now` format
    tag_re = re.compile(r"^([A-Z_]+):\s*(.*)$")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m_runas = re.match(r"^\(([^)]+)\)\s*(.+)$", line)
        if not m_runas:
            continue
        if not _runas_permits_root(m_runas.group(1)):
            continue
        remainder = m_runas.group(2)
        nopasswd: bool | None = None
        for segment in remainder.split(","):
            seg = segment.strip()
            while True:
                m_tag = tag_re.match(seg)
                if not m_tag:
                    break
                tag_name, seg = m_tag.group(1), m_tag.group(2).strip()
                if tag_name == "NOPASSWD":
                    nopasswd = True
                elif tag_name == "PASSWD":
                    nopasswd = False
            if seg and _sudo_rule_matches_shutdown(seg, shutdown_bin):
                matched = bool(nopasswd)

    return bool(matched)


def check_poweroff_capable(run_cmd: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    if shutil.which("sudo") is None:
        raise RuntimeError("--poweroff requires 'sudo' on PATH")
    shutdown_bin = resolve_shutdown_bin()
    probe = run_cmd(
        ["sudo", "-n", "-l", "-l", shutdown_bin, "-h", "now"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and _sudo_output_permits_shutdown(probe.stdout or "", shutdown_bin):
        return shutdown_bin
    detail = (probe.stderr or probe.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(
        f"--poweroff requires passwordless sudo (NOPASSWD) authorization for '{shutdown_bin} -h now' (sudo -n -l -l failed{suffix})"
    )


def poweroff_vm(
    run_cmd: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    shutdown_bin: str | None = None,
) -> None:
    target_bin = shutdown_bin or resolve_shutdown_bin()
    print("Batch finished; powering off worker VM...", flush=True)
    try:
        res = run_cmd(
            ["sudo", "-n", target_bin, "-h", "now"],
            capture_output=True,
            text=True,
            timeout=POWEROFF_TIMEOUT_SEC,
        )
    except TypeError:
        res = run_cmd(["sudo", "-n", target_bin, "-h", "now"], capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"failed to power off worker VM via 'sudo -n {target_bin} -h now': timed out after {POWEROFF_TIMEOUT_SEC:g}s"
        ) from exc
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        suffix = f": {detail}" if detail else f" (exit {res.returncode})"
        raise RuntimeError(f"failed to power off worker VM via 'sudo -n {target_bin} -h now'{suffix}")


def check_linux_systemd_conflicts(
    sys_platform: str | None = None,
    run_cmd: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    plat = sys_platform or platform.system()
    if plat != "Linux" or shutil.which("systemctl") is None:
        return
    for unit in SYSTEMD_CONFLICT_UNITS:
        for flag in ([], ["--user"]):
            res = run_cmd(
                ["systemctl", *flag, "is-active", "--quiet", unit],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                scope_flag = " --user" if flag else ""
                raise RuntimeError(
                    f"systemd unit '{unit}' is active; stop and disable it before benchmarking: "
                    f"sudo systemctl{scope_flag} disable --now {unit}"
                )


def check_linux_reflink(
    selected_systems: list[str],
    env: dict[str, str] | None = None,
    sys_platform: str | None = None,
    run_cmd: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    plat = sys_platform or platform.system()
    if plat != "Linux" or "file_copy" not in selected_systems:
        return
    target_env = os.environ if env is None else env
    pg_data = Path(target_env.get("PGSQL_DATA_DIR") or "~/pg_file_copy/data").expanduser()
    pg_parent = pg_data.parent
    pg_parent.mkdir(parents=True, exist_ok=True)
    src_probe = pg_parent / f".reflink_probe_{os.getpid()}"
    dst_probe = pg_parent / f".reflink_probe_copy_{os.getpid()}"
    try:
        src_probe.write_text("reflink\n", encoding="utf-8")
        res = run_cmd(
            ["cp", "--reflink=always", str(src_probe), str(dst_probe)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"{pg_parent} does not support copy-on-write reflinks (cp --reflink=always failed); "
                "mount an XFS (crc=1,reflink=1) volume for PGSQL_DATA_DIR"
            )
    finally:
        src_probe.unlink(missing_ok=True)
        dst_probe.unlink(missing_ok=True)


def verify_host_clean(
    board: Path = BOARD,
    runs_dir: Path = RUNS,
    check_port: Callable[[dict], bool] = port_answers,
    connector_loader: Callable[[str], object] = load_connector,
) -> None:
    """Ensure no local benchmark server PID, TCP port, or crash recovery journal is active."""
    known = runnable_systems(board)
    for sys_name, meta in known.items():
        if meta.get("hosted") != "no":
            continue
        pid_file = runs_dir / f"{sys_name}.pid"
        if pid_file.is_file():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                raise RuntimeError(f"local system '{sys_name}' pidfile {pid_file} is still alive")
            except (ValueError, ProcessLookupError):
                pass
            except PermissionError:
                raise RuntimeError(f"local system '{sys_name}' pidfile {pid_file} is still alive") from None
        for suffix in ("journal.json", "active.json"):
            jpath = runs_dir / f"{sys_name}.{suffix}"
            if jpath.exists():
                raise RuntimeError(f"local system '{sys_name}' has an uncleaned recovery journal {jpath}")
        srv = connector_loader(sys_name)
        scope = srv.scope()
        if check_port(scope):
            raise RuntimeError(
                f"local system '{sys_name}' port {scope.get('host')}:{scope.get('port')} is already accepting connections"
            )


def _snapshot_files(directory: Path) -> dict[Path, int]:
    if not directory.is_dir():
        return {}
    return {
        p.resolve(): p.stat().st_mtime_ns
        for p in directory.rglob("*.json")
        if p.is_file()
    }


def _changed_files(before: dict[Path, int], directory: Path) -> list[Path]:
    after = _snapshot_files(directory)
    return sorted(p for p, mtime in after.items() if before.get(p) != mtime)


def is_interrupted_returncode(returncode: int) -> bool:
    if returncode < 0:
        return True
    if returncode in INTERRUPT_EXIT_CODES or (129 <= returncode <= 192):
        return True
    return False


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(proc: subprocess.Popen, grace_sec: float = 10.0) -> None:
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    now = time.monotonic()
    grace_deadline = now + grace_sec
    leader_done_deadline: float | None = None
    while time.monotonic() < grace_deadline:
        leader_exited = proc.poll() is not None
        group_alive = _pgid_alive(pgid)
        if leader_exited and not group_alive:
            return
        if leader_exited and leader_done_deadline is None:
            leader_done_deadline = min(grace_deadline, time.monotonic() + 0.5)
        if leader_done_deadline is not None and time.monotonic() >= leader_done_deadline:
            break
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    kill_deadline = time.monotonic() + 5.0
    while time.monotonic() < kill_deadline and _pgid_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.02)


def execute_step(
    cmd: list[str],
    remaining_sec: float,
    env: dict[str, str],
) -> tuple[int, bool, bool]:
    """Run one benchmark command with a timeout; return (returncode, interrupted, timed_out)."""
    if remaining_sec <= 0:
        return 124, False, True
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    try:
        rc = proc.wait(timeout=remaining_sec)
        if _pgid_alive(proc.pid):
            _terminate_process_group(proc, grace_sec=1.0)
        return rc, is_interrupted_returncode(rc), False
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        return 124, False, True
    except KeyboardInterrupt:
        _terminate_process_group(proc)
        return 130, True, False


def collect_macro_artifacts(changed: list[Path]) -> list[Path]:
    """Collect campaign and attempt JSON files produced during a macro step."""
    campaigns = sorted(p for p in changed if p.stem.startswith("campaign_"))
    artifacts: list[Path] = []
    seen: set[Path] = set()
    for camp in campaigns:
        for candidate in changed:
            if candidate.parent == camp.parent and not candidate.stem.startswith("campaign_"):
                if candidate not in seen:
                    seen.add(candidate)
                    artifacts.append(candidate)
        try:
            data = json.loads(camp.read_text(encoding="utf-8"))
            attempts = data.get("attempts") if isinstance(data, dict) else None
            if isinstance(attempts, dict):
                for scen, aid in attempts.items():
                    for candidate_name in (f"{aid}.json", f"{scen}_{aid}.json"):
                        apath = (camp.parent / candidate_name).resolve()
                        if apath.is_file() and apath not in seen:
                            seen.add(apath)
                            artifacts.append(apath)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        if camp not in seen:
            seen.add(camp)
            artifacts.append(camp)
    return artifacts


@contextmanager
def _maybe_bounded(seconds: float | None, label: str, clock: Callable[[], float]) -> Iterator[None]:
    if (
        seconds is not None
        and seconds > 0
        and clock is time.monotonic
        and threading.current_thread() is threading.main_thread()
    ):
        try:
            with bounded(seconds, label):
                yield
        except RunError as exc:
            raise DeadlineExceededError(str(exc)) from exc
    else:
        yield


def _bounded_sync(
    deadline: float,
    clock: Callable[[], float],
    batch_deadline_min: float,
) -> None:
    rem = deadline - clock()
    if rem <= 0:
        raise DeadlineExceededError(
            f"batch deadline ({batch_deadline_min:g} min) exceeded before filesystem sync"
        )
    if hasattr(os, "sync"):
        with _maybe_bounded(rem, "filesystem sync", clock):
            os.sync()
    if deadline - clock() <= 0:
        raise DeadlineExceededError(
            f"batch deadline ({batch_deadline_min:g} min) exceeded during filesystem sync"
        )


def validate_batch_artifacts(
    micro_files: list[Path],
    macro_files: list[Path],
    board: Path = BOARD,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    failed = 0
    for path in micro_files:
        rem = (deadline - clock()) if deadline is not None else None
        if rem is not None and rem <= 0:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        try:
            with _maybe_bounded(rem, "artifact validation", clock):
                problems = list(validate_micro(path, board))
                if not problems:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "error" in data:
                        problems.append(f"error record produced: {data['error']}")
        except DeadlineExceededError:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        except Exception as exc:
            problems = [f"unreadable or invalid artifact ({type(exc).__name__}: {exc})"]
        if deadline is not None and deadline - clock() <= 0:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        if problems:
            failed += 1
            for problem in problems:
                print(f"{path}: {problem}", file=sys.stderr)
    for path in macro_files:
        rem = (deadline - clock()) if deadline is not None else None
        if rem is not None and rem <= 0:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        try:
            with _maybe_bounded(rem, "artifact validation", clock):
                checker = validate_macro_campaign if path.stem.startswith("campaign_") else validate_macro_attempt
                problems = list(checker(path, board))
                if not problems and path.stem.startswith("campaign_"):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("aborted"):
                        problems.append(f"campaign aborted: {data['aborted']}")
        except DeadlineExceededError:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        except Exception as exc:
            problems = [f"unreadable or invalid artifact ({type(exc).__name__}: {exc})"]
        if deadline is not None and deadline - clock() <= 0:
            print(f"{path}: batch deadline exceeded during artifact validation", file=sys.stderr)
            return 124
        if problems:
            failed += 1
            for problem in problems:
                print(f"{path}: {problem}", file=sys.stderr)
    if deadline is not None and deadline - clock() <= 0:
        print("batch deadline exceeded during artifact validation", file=sys.stderr)
        return 124
    total = len(micro_files) + len(macro_files)
    print(f"Batch artifact validation: {total - failed} of {total} files valid", flush=True)
    return 1 if failed else 0


def _make_bounded_cmd_runner(
    cmd_runner: Callable[..., subprocess.CompletedProcess],
    deadline: float,
    clock: Callable[[], float],
    batch_deadline_min: float,
) -> Callable[..., subprocess.CompletedProcess]:
    def _runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        rem = deadline - clock()
        if rem <= 0:
            raise DeadlineExceededError(
                f"batch deadline ({batch_deadline_min:g} min) exceeded during preflight"
            )
        try:
            res = cmd_runner(cmd, timeout=rem, **kwargs)
        except TypeError:
            res = cmd_runner(cmd, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise DeadlineExceededError(
                f"batch deadline ({batch_deadline_min:g} min) exceeded during preflight command {cmd[0]!r}"
            ) from exc
        if deadline - clock() <= 0:
            raise DeadlineExceededError(
                f"batch deadline ({batch_deadline_min:g} min) exceeded during preflight"
            )
        return res

    return _runner


def run_batch(
    *,
    machine: str,
    preset: str = "full",
    lane: str = "all",
    systems_arg: str = "",
    client_location: str = "",
    data_mount: str = "",
    batch_deadline_min: float = DEFAULT_BATCH_DEADLINE_MIN,
    poweroff: bool = False,
    profile_mode: str = "economic",
    board: Path = BOARD,
    runs_dir: Path = RUNS,
    step_runner: Callable[[list[str], float, dict[str, str]], tuple[int, bool, bool]] = execute_step,
    cmd_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    port_checker: Callable[[dict], bool] = port_answers,
    connector_loader: Callable[[str], object] = load_connector,
    sys_platform: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    if not machine or not MACHINE_RE.fullmatch(machine):
        print(f"error: invalid or missing --machine {machine!r}", file=sys.stderr)
        return 2
    if preset not in ("mini", "full"):
        print(f"error: --preset must be mini or full, got {preset!r}", file=sys.stderr)
        return 2
    if lane not in ("all", "micro", "macro"):
        print(f"error: --lane must be all, micro, or macro, got {lane!r}", file=sys.stderr)
        return 2
    if profile_mode not in ("economic", "economic_single_vm"):
        print(
            f"error: aws_worker implements the single-VM colocated economic topology (--profile-mode economic), not {profile_mode!r}; "
            "separated client/server paper topology requires dedicated VMs",
            file=sys.stderr,
        )
        return 2
    if not (math.isfinite(batch_deadline_min) and batch_deadline_min > 0):
        print("error: --batch-deadline-min must be a finite positive number", file=sys.stderr)
        return 2

    deadline = clock() + batch_deadline_min * 60.0
    os.environ.pop("DBFORK_NATIVE_ONLY", None)
    known = runnable_systems(board)
    try:
        selected = resolve_systems(systems_arg, lane, client_location, known)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        with batch_lock(runs_dir) as batch_token:
            bounded_cmd = _make_bounded_cmd_runner(cmd_runner, deadline, clock, batch_deadline_min)
            configure_data_mount(data_mount, os.environ)
            shutdown_bin: str | None = None
            should_poweroff = False
            status = 0
            if poweroff:
                shutdown_bin = check_poweroff_capable(bounded_cmd)
            try:
                try:
                    check_linux_systemd_conflicts(sys_platform=sys_platform, run_cmd=bounded_cmd)
                    check_linux_reflink(
                        selected,
                        env=os.environ,
                        sys_platform=sys_platform,
                        run_cmd=bounded_cmd,
                    )
                    if deadline - clock() <= 0:
                        raise DeadlineExceededError(
                            f"batch deadline ({batch_deadline_min:g} min) exceeded during preflight"
                        )
                    verify_host_clean(
                        board=board,
                        runs_dir=runs_dir,
                        check_port=port_checker,
                        connector_loader=connector_loader,
                    )
                    if deadline - clock() <= 0:
                        raise DeadlineExceededError(
                            f"batch deadline ({batch_deadline_min:g} min) exceeded after preflight"
                        )
                except DeadlineExceededError:
                    status = status or 124
                    should_poweroff = poweroff
                    raise
                should_poweroff = poweroff

                micro_artifacts: list[Path] = []
                macro_artifacts: list[Path] = []
                env = dict(os.environ)
                env.pop("DBFORK_NATIVE_ONLY", None)
                env["DBFORK_BATCH_TOKEN"] = batch_token
                env["DBFORK_EXECUTION_PROFILE"] = "economic_single_vm"

                try:
                    with catch_sigterm():
                        for sys_name in selected:
                            hosted = known[sys_name].get("hosted") == "yes"

                            if lane in ("all", "micro"):
                                remaining = deadline - clock()
                                if remaining <= 0:
                                    print(
                                        f"error: batch deadline ({batch_deadline_min:g} min) exceeded before [micro] {sys_name}",
                                        file=sys.stderr,
                                    )
                                    status = status or 124
                                    break
                                print(f"=== [micro] {sys_name} (machine={machine}) ===", flush=True)
                                res_dir = board / sys_name / "results"
                                before = _snapshot_files(res_dir)
                                cmd = [
                                    sys.executable,
                                    "-m",
                                    "leaderboard.run",
                                    sys_name,
                                    "--machine",
                                    machine,
                                    "--out",
                                    str(board),
                                ]
                                if hosted:
                                    cmd.extend(["--client-location", client_location])
                                rc, interrupted, timed_out = step_runner(cmd, remaining, env)
                                changed = _changed_files(before, res_dir)
                                for p in changed:
                                    if p.name == f"{machine}.json" and p not in micro_artifacts:
                                        micro_artifacts.append(p)
                                if rc == 0 and not any(p.name == f"{machine}.json" for p in changed):
                                    print(
                                        f"error: [micro] {sys_name} exited 0 but wrote no {machine}.json under {res_dir}",
                                        file=sys.stderr,
                                    )
                                    status = status or 1
                                elif rc != 0:
                                    status = status or (130 if rc < 0 else rc)
                                try:
                                    if not timed_out and not interrupted:
                                        _bounded_sync(deadline, clock, batch_deadline_min)
                                except DeadlineExceededError:
                                    timed_out = True
                                try:
                                    verify_host_clean(
                                        board=board,
                                        runs_dir=runs_dir,
                                        check_port=port_checker,
                                        connector_loader=connector_loader,
                                    )
                                except RuntimeError as exc:
                                    print(f"error: host dirty after [micro] {sys_name}: {exc}", file=sys.stderr)
                                    status = status or 1
                                    break
                                if timed_out or deadline - clock() <= 0:
                                    print(
                                        f"error: batch deadline ({batch_deadline_min:g} min) exceeded during [micro] {sys_name}",
                                        file=sys.stderr,
                                    )
                                    status = status or 124
                                    break
                                if interrupted:
                                    print(f"error: [micro] {sys_name} interrupted (rc={rc}); aborting batch", file=sys.stderr)
                                    status = status or 130
                                    break

                            if lane in ("all", "macro") and (not hosted or sys_name != "xata" or systems_arg.strip()):
                                remaining = deadline - clock()
                                if remaining <= 0:
                                    print(
                                        f"error: batch deadline ({batch_deadline_min:g} min) exceeded before [macro:{preset}] {sys_name}",
                                        file=sys.stderr,
                                    )
                                    status = status or 124
                                    break
                                print(f"=== [macro:{preset}] {sys_name} (machine={machine}) ===", flush=True)
                                macro_dir = board / sys_name / "macro"
                                before = _snapshot_files(macro_dir)
                                cmd = [
                                    sys.executable,
                                    "-m",
                                    "leaderboard.run_macro",
                                    sys_name,
                                    "--preset",
                                    preset,
                                    "--machine",
                                    machine,
                                    "--out",
                                    str(board),
                                ]
                                if hosted:
                                    cmd.append("--allow-hosted")
                                rc, interrupted, timed_out = step_runner(cmd, remaining, env)
                                changed = _changed_files(before, macro_dir)
                                produced = collect_macro_artifacts(changed)
                                for p in produced:
                                    if p not in macro_artifacts:
                                        macro_artifacts.append(p)
                                if rc == 0 and not any(p.stem.startswith("campaign_") for p in produced):
                                    print(
                                        f"error: [macro:{preset}] {sys_name} exited 0 but wrote no campaign file under {macro_dir}",
                                        file=sys.stderr,
                                    )
                                    status = status or 1
                                elif rc != 0:
                                    status = status or (130 if rc < 0 else rc)
                                try:
                                    if not timed_out and not interrupted:
                                        _bounded_sync(deadline, clock, batch_deadline_min)
                                except DeadlineExceededError:
                                    timed_out = True
                                try:
                                    verify_host_clean(
                                        board=board,
                                        runs_dir=runs_dir,
                                        check_port=port_checker,
                                        connector_loader=connector_loader,
                                    )
                                except RuntimeError as exc:
                                    print(f"error: host dirty after [macro:{preset}] {sys_name}: {exc}", file=sys.stderr)
                                    status = status or 1
                                    break
                                if timed_out or deadline - clock() <= 0:
                                    print(
                                        f"error: batch deadline ({batch_deadline_min:g} min) exceeded during [macro:{preset}] {sys_name}",
                                        file=sys.stderr,
                                    )
                                    status = status or 124
                                    break
                                if interrupted:
                                    print(f"error: [macro:{preset}] {sys_name} interrupted (rc={rc}); aborting batch", file=sys.stderr)
                                    status = status or 130
                                    break
                except KeyboardInterrupt:
                    print("error: batch interrupted by signal", file=sys.stderr)
                    status = status or 130
                finally:
                    if micro_artifacts or macro_artifacts:
                        try:
                            val_rc = validate_batch_artifacts(
                                micro_artifacts,
                                macro_artifacts,
                                board=board,
                                deadline=deadline,
                                clock=clock,
                            )
                            if val_rc != 0:
                                status = status or val_rc
                        except Exception as exc:
                            print(f"error: artifact validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                            status = status or 1
                    if status == 0 and deadline - clock() <= 0:
                        print(
                            f"error: batch deadline ({batch_deadline_min:g} min) exceeded before completion",
                            file=sys.stderr,
                        )
                        status = 124
            finally:
                if should_poweroff:
                    try:
                        poweroff_vm(cmd_runner, shutdown_bin=shutdown_bin)
                    except Exception as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        status = status or 1
            return status
    except DeadlineExceededError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 124
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws_worker",
        description="Run the BranchBench leaderboard sequentially on a single AWS worker VM (economic 1-VM model).",
    )
    parser.add_argument(
        "--machine",
        default=os.environ.get("machine", ""),
        help="Machine label recorded in result files (default: $machine)",
    )
    parser.add_argument(
        "--preset",
        default="full",
        help="Macrobench preset: mini or full (default: full)",
    )
    parser.add_argument(
        "--lane",
        default="all",
        help="Which benchmark lanes to execute: all, micro, or macro (default: all)",
    )
    parser.add_argument(
        "--systems",
        default="",
        help="Space- or comma-separated systems (default: all local systems, plus hosted systems when --client-location is given)",
    )
    parser.add_argument(
        "--client-location",
        default=os.environ.get("client_location", ""),
        help="Client location for hosted systems (e.g., 'US East (N. Virginia)')",
    )
    parser.add_argument(
        "--data-mount",
        default="",
        help="Base directory on an XFS (reflink=1) volume for database storage",
    )
    parser.add_argument(
        "--batch-deadline-min",
        type=float,
        default=DEFAULT_BATCH_DEADLINE_MIN,
        help=f"Wall-clock timeout in minutes for the entire batch (default: {DEFAULT_BATCH_DEADLINE_MIN:g})",
    )
    parser.add_argument(
        "--poweroff",
        action="store_true",
        help="Power off the VM (sudo -n shutdown -h now) when the batch completes",
    )
    parser.add_argument(
        "--profile-mode",
        default=os.environ.get("DBFORK_EXECUTION_PROFILE", "economic"),
        choices=("economic", "paper"),
        help="Execution profile: 'economic' (default 1-VM sequential) or 'paper' (paper scale>=10 contract)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=BOARD,
        help="Leaderboard directory (default: leaderboard/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    board = args.out.resolve()
    return run_batch(
        machine=args.machine,
        preset=args.preset,
        lane=args.lane,
        systems_arg=args.systems,
        client_location=args.client_location,
        data_mount=args.data_mount,
        batch_deadline_min=args.batch_deadline_min,
        poweroff=args.poweroff,
        profile_mode=args.profile_mode,
        board=board,
        runs_dir=RUNS,
    )


if __name__ == "__main__":
    sys.exit(main())
