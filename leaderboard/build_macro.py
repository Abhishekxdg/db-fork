"""Collect agent-workflow attempts into campaigns for the page.

leaderboard.site writes the result as macro-data.js next to data.js;
`python -m leaderboard.build_macro` delegates to it and builds the development
site. Nothing here reads the microbench results, and the microbench build never
reads these files.

A campaign is one run_macro invocation: one system, one preset, all six
scenarios, one attempt per scenario, and one campaign record that exists from
before the server started. Only a campaign that passes validation is a result:
committed and unchanged sources, a finished run with a stopped server, every
attempt present, readable and cleaned up, and no conflicting record. Any other
campaign is excluded with its reasons, never shown with a caveat. A failed,
incomplete or timed-out workflow is a result, not a validation failure.
Campaigns are listed newest first.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from leaderboard.validate import BOARD
from leaderboard.validate_macro import (
    MACRO_SUITE,
    SCENARIOS,
    attempt_files,
    campaign_files,
    validate,
    validate_campaign,
)

PROVENANCE = ("version", "lock_sha256", "python", "platform", "environment")


def group_key(campaign: dict) -> str:
    """Campaigns compare only within the same protocol, workload, source revision,
    lock, runtime and concrete environment. The system under test stays per row."""
    identity = {"suite": MACRO_SUITE, "preset": campaign["preset"], "scenarios": sorted(campaign["scenarios"]),
                "source_sha256": campaign["source_sha256"], "lock_sha256": campaign["lock_sha256"],
                "python": campaign["python"], "platform": campaign["platform"], "machine": campaign["machine"],
                "environment": campaign["environment"],
                "execution_profile": campaign.get("execution_profile", "local_single_host"),
                "native_only": bool(campaign.get("native_only")) or any((a.get("evidence") or {}).get("native_only") is True for a in campaign["attempts"].values()),
                "configs": {k: a["config_sha256"] for k, a in sorted(campaign["attempts"].items()) if a["config_sha256"]}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def failed_checks(campaign: dict) -> list[str]:
    """Why a campaign fails validation; empty means it is a result."""
    reasons = []
    if campaign["dirty"]:
        reasons.append("run from uncommitted sources")
    if campaign["changed_during_run"]:
        reasons.append("sources changed during the run")
    if campaign["aborted"]:
        reasons.append(f"campaign aborted: {campaign['aborted']}")
    if not campaign["finished"]:
        reasons.append("campaign did not finish")
    is_hosted = campaign.get("hosted") == "yes"
    if is_hosted:
        if campaign["server"]["started_here"] or campaign["server"]["stop"] != "not_started":
            reasons.append(f"server stop {campaign['server']['stop'].replace('_', ' ')}")
    elif campaign["server"]["stop"] != "stopped":
        reasons.append(f"server stop {campaign['server']['stop'].replace('_', ' ')}")
    reasons += campaign["conflicts"]
    if set(campaign["scenarios"]) != set(SCENARIOS):
        reasons.append(f"subset run: {len(campaign['scenarios'])} of {len(SCENARIOS)} workflows selected")
    if missing := [k for k in campaign["scenarios"] if k not in campaign["attempts"] and k not in campaign["duplicates"]]:
        reasons.append("not run: " + ", ".join(SCENARIOS[k] for k in missing))
    if unread := [SCENARIOS[k] for k, a in campaign["attempts"].items() if a["outcome"]["status"] == "unverified"]:
        reasons.append("evidence missing or unreadable: " + ", ".join(unread))
    if unclean := [SCENARIOS[k] for k, a in campaign["attempts"].items() if a["cleanup"]["status"] != "verified"]:
        reasons.append("database cleanup not verified: " + ", ".join(unclean))
    return reasons


def missing_attempt(attempt_id: str, reason: str, source: str | None) -> dict:
    """The cell for an attempt the campaign record names but no valid file backs."""
    return {"attempt_id": attempt_id, "date": None, "source": source, "missing": True,
            "outcome": {"status": "unverified", "reasons": [reason], "elapsed_sec": None, "setup_sec": None, "completion_sec": None},
            "cleanup": {"status": "unknown", "detail": "no valid attempt record"}, "child": None, "evidence": None,
            "config_sha256": None, "base_config_sha256": None, "effective": None}


def build(board: Path = BOARD, sources: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Valid campaigns newest first, and excluded files and campaigns with their reasons. With sources,
    only those board-relative campaign records are heads and a campaign named by
    two of them is excluded rather than silently overwritten; every attempt file
    is still ingested, so duplicates and conflicts of a selected campaign remain."""
    campaigns: dict[tuple[str, str], dict] = {}
    excluded, collided = [], set()
    for path in campaign_files(board):
        source = path.relative_to(board).as_posix()
        if sources is not None and source not in sources:
            continue
        if problems := validate_campaign(path, board):
            excluded.append({"system": path.parts[-4], "source": source, "reason": "invalid campaign record: " + "; ".join(problems)})
            continue
        data = json.loads(path.read_text())
        if sources is not None and (path.parts[-4], data["id"]) in campaigns:
            collided.add((path.parts[-4], data["id"]))
            excluded.append({"system": path.parts[-4], "source": source, "reason": f"campaign {data['id']} is also selected at {campaigns[path.parts[-4], data['id']]['source']}; a campaign is selected once"})
            continue
        meta_path = board / path.parts[-4] / "system.json"
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        campaigns[(path.parts[-4], data["id"])] = {
            "campaign_id": data["id"], "system": data["system"], "folder": path.parts[-4], "machine": data["machine"],
            "hosted": meta.get("hosted", "no"), "native_only": bool(data.get("native_only", False)),
            "execution_profile": data.get("execution_profile", "local_single_host"),
            "preset": data["preset"], "scenarios": data["scenarios"], "started": data["started"], "finished": data["finished"],
            "aborted": data["aborted"], "server": data["server"], "source": source, "expected": data["attempts"],
            "version": data["version"], "dirty": data["source"]["dirty"], "changed_during_run": data["source"]["changed_during_run"],
            "commit": data["source"]["commit"], "source_sha256": data["source"]["source_sha256"],
            **{k: data[k] for k in PROVENANCE if k != "version"}, "tags": None, "attempts": {}, "duplicates": {}, "conflicts": [],
            "invalid": {},
        }
    for key in collided:
        excluded.append({"system": key[0], "source": campaigns[key]["source"], "reason": f"campaign {key[1]} is selected more than once; a campaign is selected once"})
        del campaigns[key]
    for path in attempt_files(board):
        source = path.relative_to(board).as_posix()
        try:
            problems = validate(path, board)
        except Exception as e:  # ponytail: the validator rejects every shape it knows; an unknown one must still not stop the build
            problems = [f"validator error: {type(e).__name__}: {e}"]
        if problems:
            reason = "invalid: " + "; ".join(problems)
            excluded.append({"system": path.parts[-4], "source": source, "reason": reason})
            for campaign in campaigns.values():
                if campaign["folder"] == path.parts[-4] and path.stem in campaign["expected"].values():
                    campaign["invalid"][path.stem] = (reason, source)
            continue
        data = json.loads(path.read_text())
        c = data["campaign"]
        campaign = campaigns.get((path.parts[-4], c["id"]))
        if campaign is None:
            excluded.append({"system": path.parts[-4], "source": source, "reason": f"no campaign record for campaign {c['id']}"})
            continue
        key = data["scenario"]
        entry = {
            "attempt_id": data["attempt_id"], "date": data["date"], "source": source, "outcome": data["outcome"],
            "cleanup": data["cleanup"], "child": data["child"], "evidence": data["evidence"],
            "config_sha256": data["config"]["config_sha256"], "base_config_sha256": data["config"]["base_config_sha256"],
            "effective": data["config"]["effective"],
        }
        if key in campaign["duplicates"]:
            campaign["duplicates"][key].append(entry)
        elif key in campaign["attempts"]:
            campaign["duplicates"][key] = [campaign["attempts"].pop(key), entry]
        else:
            campaign["attempts"][key] = entry
        if campaign["expected"].get(key) != data["attempt_id"]:
            campaign["conflicts"].append(f"{SCENARIOS[key]}: attempt {data['attempt_id']} is not the one the campaign record names")
        recorded = {"id": campaign["campaign_id"], **{k: campaign[k] for k in ("preset", "scenarios", "machine", "started")}}
        if {k: c.get(k) for k in recorded} != recorded or bool(c.get("native_only", False)) != campaign["native_only"]:
            campaign["conflicts"].append(f"{SCENARIOS[key]}: campaign metadata differs from the campaign record")
        for field in ("commit", "source_sha256"):
            if data["source"][field] != campaign[field]:
                campaign["conflicts"].append(f"{SCENARIOS[key]}: source {field} differs from the campaign record")
        for field in PROVENANCE:
            if data[field] != campaign[field]:
                campaign["conflicts"].append(f"{SCENARIOS[key]}: {field} differs from the campaign record")
        if data.get("execution_profile", "local_single_host") != campaign.get("execution_profile", "local_single_host"):
            campaign["conflicts"].append(f"{SCENARIOS[key]}: execution_profile differs from the campaign record")
        if campaign["tags"] is None:
            campaign["tags"] = data["tags"]
        campaign["dirty"] = campaign["dirty"] or data["source"]["dirty"]
        campaign["changed_during_run"] = campaign["changed_during_run"] or data["source"]["changed_during_run"]
    results = []
    for campaign in campaigns.values():
        for key, attempt_id in campaign["expected"].items():
            if key in campaign["attempts"] or key in campaign["duplicates"]:
                continue
            reason, source = campaign["invalid"].get(attempt_id, (f"attempt file {attempt_id} is missing", None))
            campaign["attempts"][key] = missing_attempt(attempt_id, reason, source)
        for key, entries in campaign["duplicates"].items():
            campaign["conflicts"].append(f"{SCENARIOS[key]}: {len(entries)} attempts in one campaign ({', '.join(e['attempt_id'] for e in entries)})")
        campaign["conflicts"] = sorted(set(campaign["conflicts"]))
        campaign["tags"] = campaign["tags"] or []
        campaign["version"] = campaign["version"] or "unknown"
        if reasons := failed_checks(campaign):
            excluded.append({"system": campaign["folder"], "source": campaign["source"], "reason": "failed validation: " + "; ".join(reasons)})
            continue
        campaign["group"] = group_key(campaign)
        for key in ("expected", "invalid", "duplicates", "conflicts", "dirty", "changed_during_run", "aborted", "server"):
            del campaign[key]
        results.append(campaign)
    return sorted(results, key=lambda c: c["started"], reverse=True), excluded


def main() -> int:
    """The historical entry point builds the development site through the assembler."""
    from leaderboard import site  # the assembler imports this module
    print("leaderboard.build_macro: assembling the development site (python -m leaderboard.site --phase local)", file=sys.stderr)
    return site.main(["--phase", "local"])


if __name__ == "__main__":
    sys.exit(main())

