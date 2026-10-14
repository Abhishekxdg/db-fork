"""The agent-workflow (macrobench) result contract, its classifier and its validator.

One file per attempt at <system>/macro/<YYYYMMDD>/<attempt id>.json. An attempt is
one scenario (S1-S6) of one campaign on one system; the attempt id is also the run
and database id of that scenario's child process, so it is unique and never
rewritten. The file carries the parent's verdict (outcome), the cleanup verdict,
how the child ended, the scrubbed evidence the child wrote, and provenance.

One campaign record per run_macro invocation at
<system>/macro/<YYYYMMDD>/campaign_<campaign id>.json, written before the server
starts and rewritten as attempts finish, so an aborted or interrupted campaign stays
visible even when no attempt was published.

From the repository root:
    python -m leaderboard.validate_macro [file ...]
Without arguments it checks every attempt and campaign file. Exits non-zero on any
problem. Malformed input yields problems, never an exception.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

from leaderboard.validate import BOARD, IDENTITY, MACHINE_RE, has_type, parse_day, sha256


def setup_outcome(stats: dict) -> str | None:
    """Why setup stats describe a failed setup, or None when they prove a
    complete one. Missing evidence is a failure too."""
    if "schema_failed" not in stats or "seed_commit" not in stats:
        return "setup produced no schema or seed-commit evidence"
    if stats["schema_failed"]:
        return f"{len(stats['schema_failed'])} schema statements failed"
    if stats["seed_commit"] != "OK":
        return f"seed commit {stats['seed_commit']}"
    return None

MACRO_SCHEMA_VERSION = 2
MACRO_SUITE = "macro-1"
CONFIGS = BOARD.parent / "macrobench" / "configs"
SCENARIOS = {"rl_env": "S1 RL environment", "context_mgmt": "S2 Context management",
             "multi_agent": "S3 Multi-agent", "dev_agent": "S4 Dev agent",
             "ops_agent": "S5 Ops agent", "data_agent": "S6 Data agent"}
PRESETS = {"mini": "Smoke", "full": "Full"}
OUTCOMES = ("verified", "incomplete", "failed", "timed_out", "unverified")
CLEANUPS = ("verified", "failed", "unknown")
ENDINGS = ("exited", "killed", "interrupted", "launch_failed")
STOPS = ("pending", "stopped", "failed", "not_started")
VERBS = ("branch", "commit", "diff", "log", "merge", "rebase", "revert", "reset", "delete")
ATTEMPT_RE = re.compile(r"bb_[0-9a-f]{8}")
CAMPAIGN_RE = re.compile(r"[0-9a-f]{8}")
CAMPAIGN_FILE_RE = re.compile(r"campaign_[0-9a-f]{8}")
HEX = {40: re.compile(r"[0-9a-f]{40}"), 64: re.compile(r"[0-9a-f]{64}")}
FIELDS = {
    "schema_version": int, "suite": str, "attempt_id": str, "campaign": dict, "system": str,
    "proprietary": str, "hosted": str, "tuned": str, "tags": list, "scenario": str, "date": str,
    "machine": str, "version": str, "outcome": dict, "cleanup": dict, "child": dict,
    "evidence": (dict, type(None)), "source": dict, "config": dict, "lock_sha256": str,
    "python": str, "platform": str, "environment": dict,
}
CAMPAIGN_FIELDS = {
    "schema_version": int, "suite": str, "record": str, "id": str, "system": str, "preset": str,
    "scenarios": list, "machine": str, "started": str, "finished": (str, type(None)),
    "aborted": (str, type(None)), "server": dict, "attempts": dict, "version": (str, type(None)),
    "source": dict, "lock_sha256": str, "python": str, "platform": str, "environment": dict,
}
ENVIRONMENT = ("cpu", "cpus", "memory_bytes")
# What the page may see of the child's e2e stats. Paths, logs and Parquet stay private.
EVIDENCE_KEYS = ("run_id", "backend", "scenario", "status", "elapsed_sec", "max_runtime_sec", "timed_out",
                 "seed", "schema", "workload", "setup", "capabilities", "implementation", "invariants",
                 "metrics", "work", "worker_errors", "failed_verbs", "workflow_supported", "unsupported_ops",
                 "op_status_counts", "commit_ref_fallbacks", "spine_load")
SAMPLE = 400  # scrubbed() keeps at most this many entries of any list or dict
URI_PASSWORD = re.compile(r"(://[^:/@\s]+):[^@/\s]*@")
# Evidence legitimately holds "passed" counters and masked URIs (user:***@host).
SECRET_KEY = re.compile(r"password|secret|token|api[-_]?key|credential", re.I)
SECRET_VALUE = re.compile(r"://[^/\s@]*:(?!\*\*\*@)[^/\s@]*@|(password|secret|token|api[-_]?key)\s*[=:]", re.I)


def credentials(value, where: str = "$") -> list[str]:
    """Where keys or strings look like credentials. Never includes the value itself."""
    if isinstance(value, dict):
        return [p for k, v in value.items()
                for p in ([f"{where}.{k}"] if isinstance(k, str) and SECRET_KEY.search(k) else []) + credentials(v, f"{where}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in credentials(v, f"{where}[{i}]")]
    return [where] if isinstance(value, str) and SECRET_VALUE.search(value) else []


def attempt_files(board: Path = BOARD) -> list[Path]:
    """Every attempt file of every system folder, never private .runs output."""
    return sorted(p for p in board.glob("*/macro/*/*.json")
                  if (board / p.parts[-4] / "system.json").is_file() and not p.stem.startswith("campaign_"))


def campaign_files(board: Path = BOARD) -> list[Path]:
    return sorted(p for p in board.glob("*/macro/*/campaign_*.json") if (board / p.parts[-4] / "system.json").is_file())


def config_path(key: str, preset: str) -> Path:
    """The frozen upstream preset of one scenario."""
    return CONFIGS / f"{key}{'_mini' if preset == 'mini' else ''}.textproto"


def workload_digest(effective: dict) -> str:
    """The digest of a complete normalized workload; the runner and the validator share it."""
    return hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def scrubbed(value, depth: int = 0):
    """JSON primitives only, URI passwords masked, non-finite numbers nulled, bounded depth and size."""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return URI_PASSWORD.sub(r"\1:***@", value)[:1000]
    if depth >= 6:
        return None
    if isinstance(value, dict):
        return {str(k)[:120]: scrubbed(v, depth + 1) for k, v in list(value.items())[:SAMPLE]}
    if isinstance(value, (list, tuple)):
        return [scrubbed(v, depth + 1) for v in list(value)[:SAMPLE]]
    return str(value)[:200]


def evidence_summary(e2e: dict) -> dict:
    """The allowlisted, scrubbed part of a child's e2e stats."""
    return {key: scrubbed(e2e[key]) for key in EVIDENCE_KEYS if key in e2e}


def non_finite(value, where: str = "$") -> list[str]:
    if isinstance(value, float) and not math.isfinite(value):
        return [where]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in non_finite(v, f"{where}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in non_finite(v, f"{where}[{i}]")]
    return []


def counter(value) -> bool:
    return type(value) is int and value >= 0


def seconds(value) -> bool:
    return has_type(value, float) and math.isfinite(value) and value >= 0


def strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def counters(value) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and counter(v) for k, v in value.items())


def timings(evidence: dict | None) -> tuple[float | None, float | None]:
    """The elapsed and setup seconds an attempt may publish: the evidence's own, or null."""
    if not isinstance(evidence, dict):
        return None, None
    setup = evidence.get("setup")
    setup_sec = setup.get("setup_sec") if isinstance(setup, dict) else None
    elapsed = evidence.get("elapsed_sec")
    return elapsed if seconds(elapsed) else None, setup_sec if seconds(setup_sec) else None


def evidence_problems(e: dict) -> list[str]:
    """Why the evidence cannot be consumed: every required part with its shape and
    agreeing totals. Empty means classify() may read it without further checks."""
    problems = []

    def need(ok: bool, what: str) -> None:
        if not ok:
            problems.append(what)

    need(isinstance(e.get("status"), str), "the result has no status")
    need(seconds(e.get("elapsed_sec")), "elapsed_sec is not a non-negative finite number")
    need(isinstance(e.get("timed_out"), bool), "timed_out is not a boolean")
    setup = e.get("setup")
    need(isinstance(setup, dict) and isinstance(setup.get("schema_failed", []), list)
         and isinstance(setup.get("seed_commit", ""), str) and ("setup_sec" not in setup or seconds(setup["setup_sec"])),
         "setup evidence is malformed")
    work = e.get("work")
    need(isinstance(work, dict) and all(counter(work.get(k)) for k in ("expected", "succeeded", "failed", "skipped"))
         and work["expected"] > 0, "logical work counters are missing, negative or expect no work")
    errors = e.get("worker_errors")
    need(isinstance(errors, list) and all(isinstance(w, dict) and isinstance(w.get("error"), str) for w in errors),
         "worker error log is missing or malformed")
    verbs = e.get("failed_verbs")
    need(isinstance(verbs, dict) and counters(verbs.get("expected")) and counters(verbs.get("unexpected")),
         "failed-operation accounting is missing or malformed")
    inv = e.get("invariants")
    if not (isinstance(inv, dict) and isinstance(inv.get("enabled"), bool)
            and all(counter(inv.get(k)) for k in ("checked", "passed", "failed", "not_applicable"))
            and isinstance(inv.get("results"), list)
            and all(isinstance(r, dict) and isinstance(r.get("name"), str) and r.get("passed") in (True, False, None)
                    and isinstance(r.get("detail", ""), str) for r in inv["results"])):
        problems.append("correctness check summary is missing or malformed")
    else:
        # The counts are the evidence and results must list every check, except that
        # scrubbed() keeps at most SAMPLE entries: up to that cap the list must match
        # the counts exactly; beyond it the list must be exactly the cap and must
        # not contradict the counts.
        results = inv["results"]
        tally = {k: sum(1 for r in results if r.get("passed") is v) for k, v in (("passed", True), ("failed", False), ("not_applicable", None))}
        total = inv["checked"] + inv["not_applicable"]
        complete = total <= SAMPLE
        need(inv["checked"] == inv["passed"] + inv["failed"] and len(results) == min(total, SAMPLE)
             and all(n == inv[k] if complete else n <= inv[k] for k, n in tally.items())
             and ("all_passed" not in inv or inv["all_passed"] == (inv["failed"] == 0)),
             "correctness check totals do not agree with their results")
    need(strings(e.get("unsupported_ops")), "operation support summary is missing or malformed")
    counts = e.get("op_status_counts")
    need(isinstance(counts, dict) and all(
        isinstance(k, str) and isinstance(c, dict) and all(counter(c.get(f)) for f in ("ok", "unsupported", "failed"))
        and seconds(c.get("ok_seconds")) for k, c in counts.items()), "per-operation counts are missing or malformed")
    need(counter(e.get("commit_ref_fallbacks")), "history-ref fallback count is missing")
    metrics = e.get("metrics")
    need(isinstance(metrics, dict) and all(k not in metrics or counter(metrics[k])
                                          for k in ("history_ref_fallbacks", "spine_attempted", "spine_failed", "integrity_mismatches")),
         "metrics are missing or malformed")
    need(isinstance(e.get("capabilities", {}), dict) and isinstance(e.get("implementation", {}), dict)
         and all(isinstance(e.get("implementation", {}).get(k, {}), dict) for k in ("verbs", "notes")),
         "declared implementation is malformed")
    return problems


def classify(evidence: dict | None, child: dict) -> tuple[str, list[str]]:
    """The attempt's outcome from the evidence the child left and how the child
    ended, never from its exit code alone.

    verified needs positive proof of every required part from a child that exited
    on its own; anything missing or malformed is unverified, a child ended at its
    deadline is timed_out, a setup, worker, unexplained operation or correctness
    failure is failed, and skipped or degraded work is incomplete."""
    ended = child.get("ended")
    if ended == "launch_failed":
        return "unverified", [f"the child could not be started: {child.get('error') or 'unknown error'}"]
    if ended == "interrupted":
        return "unverified", ["the parent was interrupted before the workflow finished"]
    if evidence is None:
        if ended == "killed":
            return "timed_out", ["the parent ended the child at its deadline before it wrote a result"]
        return "unverified", [f"the child left no readable result (exit code {child.get('exit_code')})"]
    if problems := evidence_problems(evidence):
        return "unverified", problems
    status = evidence["status"]
    failed, incomplete = [], []
    if status.startswith(("setup_failed", "crashed", "invariant_failed")):
        failed.append(status[:300])
    elif status == "incomplete":  # the runner's stall watchdog cancelled an operation that never finished
        incomplete.append("an operation stalled and was cancelled before it finished")
    elif status not in ("completed", "interrupted"):
        return "unverified", [f"unknown status {status[:60]!r}"]
    if problem := setup_outcome(evidence["setup"]):
        failed.append(problem)
    work = evidence["work"]
    done = work["succeeded"] + work["failed"] + work["skipped"]
    timed_out = evidence["timed_out"] or status == "interrupted" or ended == "killed"
    if done > work["expected"]:
        return "unverified", [f"{done} work units finished but only {work['expected']} were expected"]
    if work["failed"]:
        failed.append(f"{work['failed']} of {work['expected']} work units failed")
    if work["skipped"]:
        incomplete.append(f"{work['skipped']} of {work['expected']} work units skipped")
    if done < work["expected"] and not timed_out:
        failed.append(f"only {done} of {work['expected']} work units finished")
    if errors := evidence["worker_errors"]:
        failed.append(f"{len(errors)} worker error{'s' if len(errors) > 1 else ''}: {errors[0]['error']}"[:300])
    if unexpected := evidence["failed_verbs"]["unexpected"]:
        failed.append("unexplained failed operations: " + ", ".join(f"{k} ×{v}" for k, v in sorted(unexpected.items())))
    if mismatches := evidence["metrics"].get("integrity_mismatches", 0):
        failed.append(f"{mismatches} ingest integrity mismatch{'es' if mismatches > 1 else ''}: derived tables disagree with the base rows")
    # Expected background (spine) failures are recovered only while other spine
    # transactions succeed; all of production failing is a failure, not a diagnostic.
    metrics = evidence["metrics"]
    if evidence["failed_verbs"]["expected"].get("spine") and metrics.get("spine_attempted", 0) - metrics.get("spine_failed", 0) <= 0:
        failed.append(f"every background spine transaction failed ({metrics.get('spine_failed', 0)} of {metrics.get('spine_attempted', 0)} attempted)")
    inv = evidence["invariants"]
    if not inv["enabled"]:
        incomplete.append("correctness checks were disabled")
    if inv["failed"]:
        names = [r["name"] for r in inv["results"] if r.get("passed") is False]
        failed.append(f"{inv['failed']} correctness check{'s' if inv['failed'] > 1 else ''} failed: " + "; ".join(names[:3]))
    elif inv["checked"] == 0 and inv["enabled"]:
        incomplete.append("no correctness check was applicable")
    if inv["not_applicable"]:
        names = [r["name"] for r in inv["results"] if r.get("passed") is None]
        incomplete.append(f"{inv['not_applicable']} check{'s' if inv['not_applicable'] > 1 else ''} not applicable: " + "; ".join(names[:3]))
    if unsupported := evidence["unsupported_ops"]:
        incomplete.append("unsupported operations: " + ", ".join(u[:60] for u in unsupported[:5]))
    fallbacks = evidence["commit_ref_fallbacks"] + metrics.get("history_ref_fallbacks", 0)
    if fallbacks:
        incomplete.append(f"{fallbacks} history reference{'s' if fallbacks > 1 else ''} fell back to a branch head")
    if timed_out:
        reason = ("the parent ended the child at its deadline" if ended == "killed"
                  else "the runtime cap ended the workflow before it finished")
        return "timed_out", [reason, *failed, *incomplete]
    if failed:
        return "failed", failed + incomplete
    if incomplete:
        return "incomplete", incomplete
    return "verified", []


def expected_outcome(data: dict) -> dict:
    """The outcome block an attempt file must carry, recomputed from its evidence and child."""
    status, reasons = classify(data["evidence"], data["child"])
    elapsed, setup = timings(data["evidence"])
    return {"status": status, "reasons": reasons, "elapsed_sec": elapsed, "setup_sec": setup,
            "completion_sec": elapsed if status == "verified" else None}


def load(path: Path, board: Path, fields: dict) -> tuple[list[str], dict | None, dict | None, list]:
    """Shared head of both validators: location, system metadata, JSON object, top-level
    field types, credentials and non-finite numbers."""
    try:
        sysdir, macro, day_dir, name = path.resolve().relative_to(board.resolve()).parts
    except ValueError:
        return [f"not at <system>/macro/<YYYYMMDD>/<file>.json under {board}"], None, None, []
    problems = []
    if macro != "macro" or not name.endswith(".json"):
        problems.append("not at <system>/macro/<YYYYMMDD>/<file>.json")
    if (day := parse_day(day_dir)) is None:
        problems.append(f"date directory {day_dir!r} is not a YYYYMMDD date")
    try:
        meta = json.loads((board / sysdir / "system.json").read_text())
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return problems + [f"cannot read {sysdir}/system.json or the file as JSON: {e}"], None, None, []
    if not isinstance(data, dict) or not isinstance(meta, dict):
        return problems + ["not a JSON object"], None, None, []
    problems += [f"{where} looks like a credential" for where in credentials(data)]
    problems += [f"{where} is not a finite number" for where in non_finite(data)]
    if missing := sorted(fields.keys() - data.keys()):
        problems.append(f"missing keys: {missing}")
    if unexpected := sorted(data.keys() - fields.keys()):
        problems.append(f"unexpected keys: {unexpected}")
    problems += [f"{k} must be {getattr(t, '__name__', 'string or null')}" for k, t in fields.items()
                 if k in data and not (isinstance(data[k], t) if isinstance(t, tuple) else has_type(data[k], t))]
    if not problems and (data["schema_version"] != MACRO_SCHEMA_VERSION or data["suite"] != MACRO_SUITE):
        problems.append(f"not schema {MACRO_SCHEMA_VERSION} of suite {MACRO_SUITE}")
    return problems, data, meta, [sysdir, day]


def campaign_problems(c, data: dict, scenario: str | None) -> list[str]:
    """The campaign block an attempt carries, or the head of a campaign record."""
    if (not isinstance(c, dict) or not isinstance(c.get("id"), str) or not CAMPAIGN_RE.fullmatch(c["id"])
            or not isinstance(c.get("preset"), str) or c["preset"] not in PRESETS or not strings(c.get("scenarios")) or not c["scenarios"]
            or len(set(c["scenarios"])) != len(c["scenarios"]) or any(s not in SCENARIOS for s in c["scenarios"])
            or c.get("machine") != data["machine"] or not isinstance(c.get("started"), str)):
        return ["campaign must hold id, preset, distinct known scenarios, machine and started"]
    if scenario is not None and scenario not in c["scenarios"]:
        return ["the attempt's scenario is not in its campaign"]
    return []


def provenance_problems(data: dict) -> list[str]:
    problems = [f"{k} is empty" for k in ("python", "platform") if not data[k].strip()]
    if not HEX[64].fullmatch(data["lock_sha256"]):
        problems.append("lock_sha256 is not a SHA-256")
    s = data["source"]
    if (not isinstance(s.get("commit"), str) or not HEX[40].fullmatch(s["commit"]) or type(s.get("dirty")) is not bool
            or type(s.get("changed_during_run")) is not bool or not isinstance(s.get("source_sha256"), str)
            or not HEX[64].fullmatch(s["source_sha256"])):
        problems.append("source must hold commit, dirty, changed_during_run and source_sha256")
    env = data["environment"]
    if (set(env) != set(ENVIRONMENT) or not (env["cpu"] is None or isinstance(env["cpu"], str))
            or not all(env[k] is None or counter(env[k]) for k in ("cpus", "memory_bytes"))):
        problems.append("environment must hold cpu, cpus and memory_bytes, each known or null")
    if not MACHINE_RE.fullmatch(data["machine"]):
        problems.append("machine label is malformed")
    return problems


def validate(path: Path, board: Path = BOARD) -> list[str]:
    """Every problem with one attempt file. An empty list means the file is valid."""
    problems, data, meta, where = load(path, board, FIELDS)
    if not ATTEMPT_RE.fullmatch(path.stem):
        problems.append(f"attempt {path.stem!r} must match {ATTEMPT_RE.pattern}")
    if problems or data is None:
        return problems
    sysdir, day = where
    if data["attempt_id"] != path.stem:
        problems.append("attempt_id differs from the file name")
    problems += [f"{k} differs from {sysdir}/system.json" for k in IDENTITY if data[k] != meta.get(k)]
    if day and data["date"] != day.isoformat():
        problems.append(f"date {data['date']} differs from its directory")
    if data["scenario"] not in SCENARIOS:
        problems.append(f"unknown scenario {data['scenario']!r}")
    if not data["version"].strip():
        problems.append("version is empty")
    problems += provenance_problems(data)
    c = data["campaign"]
    problems += campaign_problems(c, data, data["scenario"])
    o = data["outcome"]
    if (o.get("status") not in OUTCOMES or not strings(o.get("reasons"))
            or any(not (o.get(k) is None or seconds(o.get(k))) for k in ("elapsed_sec", "setup_sec", "completion_sec"))):
        problems.append("outcome must hold a known status, string reasons and non-negative or null timings")
    if data["cleanup"].get("status") not in CLEANUPS or not isinstance(data["cleanup"].get("detail"), str):
        problems.append("cleanup must hold a known status and a detail string")
    ch = data["child"]
    if (not (ch.get("exit_code") is None or type(ch.get("exit_code")) is int) or ch.get("ended") not in ENDINGS
            or not (ch.get("error") is None or isinstance(ch["error"], str))
            or not seconds(ch.get("deadline_sec")) or not isinstance(ch.get("log_tail"), str)):
        problems.append("child must hold exit_code, ended, error, deadline_sec and log_tail")
    cfg = data["config"]
    if (not isinstance(cfg.get("preset"), str) or cfg["preset"] not in PRESETS or cfg["preset"] != c.get("preset")
            or not isinstance(cfg.get("effective"), dict)
            or any(not (isinstance(cfg.get(k), str) and HEX[64].fullmatch(cfg[k])) for k in ("base_config_sha256", "config_sha256"))):
        problems.append("config must hold the campaign preset, both digests and the effective config")
    elif data["scenario"] in SCENARIOS:
        effective = cfg["effective"]
        if cfg["config_sha256"] != workload_digest(effective):
            problems.append("config_sha256 is not the digest of the effective config")
        setup = effective.get("database_setup")
        if ("run_id" in effective or "backend" in effective or not isinstance(setup, dict) or setup.get("db_name")
                or setup.get("cleanup") or "generated" not in setup):
            problems.append("the effective config must clear run id, backend and database name, keep cleanup off and generate its data")
        if not (isinstance(effective.get("workload"), dict) and data["scenario"] in effective["workload"]):
            problems.append("the effective config does not describe the attempt's scenario")
        base = config_path(data["scenario"], cfg["preset"])
        if not base.is_file() or sha256(base) != cfg["base_config_sha256"]:
            problems.append(f"base_config_sha256 is not the digest of the frozen preset {base.name}")
    if data["evidence"] is not None:
        if set(data["evidence"]) - set(EVIDENCE_KEYS):
            problems.append("evidence holds keys outside the allowlist")
        if data["evidence"].get("run_id") != data["attempt_id"]:
            problems.append("evidence does not belong to this attempt")
        if data["evidence"].get("scenario") != data["scenario"]:
            problems.append("evidence describes another scenario")
    if problems:
        return problems
    expected = expected_outcome(data)
    if o["status"] != expected["status"]:
        problems.append(f"outcome {o['status']} does not follow from the evidence ({expected['status']})")
    if any(o[k] != expected[k] for k in ("elapsed_sec", "setup_sec", "completion_sec")):
        problems.append("published timings do not agree with the evidence")
    if o["status"] != "verified" and not o["reasons"]:
        problems.append("a non-verified outcome needs its reasons")
    return problems


def validate_campaign(path: Path, board: Path = BOARD) -> list[str]:
    """Every problem with one campaign record."""
    problems, data, meta, where = load(path, board, CAMPAIGN_FIELDS)
    if not CAMPAIGN_FILE_RE.fullmatch(path.stem):
        problems.append(f"campaign file {path.stem!r} must match {CAMPAIGN_FILE_RE.pattern}")
    if problems or data is None:
        return problems
    if data["record"] != "campaign" or f"campaign_{data['id']}" != path.stem:
        problems.append("record must be a campaign whose id is in the file name")
    if data["system"] != meta.get("system"):
        problems.append(f"system differs from {where[0]}/system.json")
    problems += campaign_problems(data, data, None)
    problems += provenance_problems(data)
    if data["version"] is not None and not data["version"].strip():
        problems.append("version is empty")
    s = data["server"]
    if set(s) != {"started_here", "stop"} or type(s["started_here"]) is not bool or s["stop"] not in STOPS:
        problems.append("server must hold started_here and a known stop verdict")
    a = data["attempts"]
    if (not all(isinstance(k, str) and k in SCENARIOS and isinstance(v, str) and ATTEMPT_RE.fullmatch(v) for k, v in a.items())
            or len(set(a.values())) != len(a) or (not problems and any(k not in data["scenarios"] for k in a))):
        problems.append("attempts must map selected scenarios to distinct attempt ids")
    return problems


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or attempt_files() + campaign_files()
    failed = {}
    for path in paths:
        check = validate_campaign if path.stem.startswith("campaign_") else validate
        if problems := check(path):
            failed[path] = problems
    for path, problems in failed.items():
        for problem in problems:
            print(f"{path}: {problem}", file=sys.stderr)
    print(f"{len(paths) - len(failed)} of {len(paths)} files valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
