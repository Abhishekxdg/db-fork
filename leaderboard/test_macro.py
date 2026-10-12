"""Agent-workflow lane: outcome classification, result contract, build and the
parent runner's lifecycle with a fake child. No database or upstream runner needed."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from leaderboard import build_macro, run_macro
from leaderboard import run as micro
from leaderboard.validate import BOARD, sha256
from leaderboard.validate_macro import (
    SCENARIOS,
    classify,
    config_path,
    evidence_summary,
    validate,
    validate_campaign,
    workload_digest,
)

CHILD = {"exit_code": 0, "ended": "exited", "error": None, "deadline_sec": 60, "log_tail": ""}
SOURCE = {"commit": "0" * 40, "dirty": False, "changed_during_run": False, "source_sha256": "1" * 64}
ENV = {"cpu": "Test CPU", "cpus": 8, "memory_bytes": 16 * 2**30}
PROVENANCE = {"source": SOURCE, "lock_sha256": "4" * 64, "python": "3.13.14", "platform": "Darwin 27.0.0 arm64", "environment": ENV}
CAMPAIGN = {"id": "89abcdef", "preset": "mini", "scenarios": list(SCENARIOS), "machine": "box", "started": "2026-10-10T10:00:00+00:00"}


def good_evidence(**changes) -> dict:
    return {
        "run_id": "bb_0123abcd", "backend": "DOLT_MYSQL", "scenario": "rl_env", "status": "completed",
        "elapsed_sec": 12.5, "max_runtime_sec": 60, "timed_out": False, "seed": 42,
        "setup": {"schema_statements": 26, "schema_failed": [], "seed_commit": "OK", "setup_sec": 1.5},
        "capabilities": {"merge": True}, "implementation": {"verbs": {"merge": "native"}, "notes": {}},
        "invariants": {"enabled": True, "checked": 3, "passed": 3, "failed": 0, "not_applicable": 0, "all_passed": True,
                       "results": [{"name": f"S1.{i}", "passed": True, "detail": ""} for i in (1, 2, 3)]},
        "metrics": {"leaves": 6}, "work": {"expected": 8, "succeeded": 8, "failed": 0, "skipped": 0},
        "worker_errors": [], "failed_verbs": {"expected": {}, "unexpected": {}}, "workflow_supported": True,
        "unsupported_ops": [], "op_status_counts": {"BRANCH": {"ok": 8, "unsupported": 0, "failed": 0, "ok_seconds": 0.4}},
        "commit_ref_fallbacks": 0, **changes,
    }


def invariants(**changes) -> dict:
    return {**good_evidence()["invariants"], **changes}


def config_block(scenario: str = "rl_env", preset: str = "mini") -> dict:
    base = config_path(scenario, preset)
    _, effective = run_macro.effective_config(base, 7, "bb_00000000")
    return {"preset": preset, "base_config_sha256": sha256(base), "config_sha256": workload_digest(effective), "effective": effective}


class Classifier(unittest.TestCase):
    def test_verified_needs_every_proof(self):
        self.assertEqual(classify(good_evidence(), CHILD), ("verified", []))

    def test_each_missing_or_bad_proof_changes_the_verdict(self):
        cases = {
            "setup failed": (good_evidence(status="setup_failed: SetupFailed: 2 schema statements failed",
                                           setup={"schema_failed": [["CREATE", "boom"]] * 2, "seed_commit": "OK"}), "failed"),
            "seed commit": (good_evidence(setup={"schema_failed": [], "seed_commit": "FAILED"}), "failed"),
            "crash before setup": (good_evidence(status="crashed: KeyError: x", setup={}), "failed"),
            "worker error": (good_evidence(worker_errors=[{"worker": 1, "item": "2", "error": "RuntimeError: x"}]), "failed"),
            "failed units": (good_evidence(work={"expected": 8, "succeeded": 6, "failed": 2, "skipped": 0}), "failed"),
            "unfinished units": (good_evidence(work={"expected": 8, "succeeded": 6, "failed": 0, "skipped": 0}), "failed"),
            "unexpected failed op": (good_evidence(failed_verbs={"expected": {"spine": 3}, "unexpected": {"ingest": 1}}), "failed"),
            "invariant failed": (good_evidence(invariants=invariants(passed=2, failed=1, all_passed=False,
                                                                     results=[{"name": "S1.1", "passed": True, "detail": ""}, {"name": "S1.2", "passed": True, "detail": ""},
                                                                              {"name": "S1.3", "passed": False, "detail": "d"}])), "failed"),
            "crashed": (good_evidence(status="crashed: KeyError: x"), "failed"),
            "skipped units": (good_evidence(work={"expected": 8, "succeeded": 7, "failed": 0, "skipped": 1}), "incomplete"),
            "unsupported": (good_evidence(unsupported_ops=["MERGE:not implemented"]), "incomplete"),
            "collector fallback": (good_evidence(commit_ref_fallbacks=2), "incomplete"),
            "scenario fallback": (good_evidence(metrics={"history_ref_fallbacks": 1}), "incomplete"),
            "not applicable": (good_evidence(invariants=invariants(checked=2, passed=2, not_applicable=1,
                                                                   results=[{"name": "S1.1", "passed": True, "detail": ""}, {"name": "S1.2", "passed": True, "detail": ""},
                                                                            {"name": "S1.3", "passed": None, "detail": "n/a"}])), "incomplete"),
            "no checks": (good_evidence(invariants=invariants(checked=0, passed=0, results=[])), "incomplete"),
            "checks disabled": (good_evidence(invariants=invariants(enabled=False)), "incomplete"),
            "timed out": (good_evidence(timed_out=True, work={"expected": 8, "succeeded": 3, "failed": 0, "skipped": 0}), "timed_out"),
            "interrupted": (good_evidence(status="interrupted"), "timed_out"),
            "stalled operation": (good_evidence(status="incomplete"), "incomplete"),
            "no status": ({k: v for k, v in good_evidence().items() if k != "status"}, "unverified"),
            "no work": ({k: v for k, v in good_evidence().items() if k != "work"}, "unverified"),
            "too much work": (good_evidence(work={"expected": 8, "succeeded": 9, "failed": 0, "skipped": 0}), "unverified"),
            "no invariants": ({k: v for k, v in good_evidence().items() if k != "invariants"}, "unverified"),
            "no failure accounting": ({k: v for k, v in good_evidence().items() if k != "failed_verbs"}, "unverified"),
            "no fallback count": ({k: v for k, v in good_evidence().items() if k != "commit_ref_fallbacks"}, "unverified"),
            "no operation counts": ({k: v for k, v in good_evidence().items() if k != "op_status_counts"}, "unverified"),
            "unknown status": (good_evidence(status="done"), "unverified"),
        }
        for name, (evidence, expected) in cases.items():
            with self.subTest(name):
                status, reasons = classify(evidence, CHILD)
                self.assertEqual(status, expected)
                self.assertTrue(reasons, "a non-verified verdict names its reasons")
        # Expected spine failures and raw statement failures alone never fail a run.
        self.assertEqual(classify(good_evidence(failed_verbs={"expected": {"spine": 9}, "unexpected": {}}, metrics={"spine_attempted": 40, "spine_failed": 9},
                                                op_status_counts={"READ": {"ok": 5, "unsupported": 0, "failed": 4, "ok_seconds": 1}}), CHILD)[0], "verified")

    def test_malformed_nested_shapes_are_unverified_not_exceptions(self):
        shapes = {
            "status dict": {"status": {"completed": True}},
            "elapsed NaN": {"elapsed_sec": float("nan")}, "elapsed negative": {"elapsed_sec": -1}, "elapsed bool": {"elapsed_sec": True},
            "timed_out string": {"timed_out": "no"},
            "setup list": {"setup": []}, "setup_sec negative": {"setup": {"schema_failed": [], "seed_commit": "OK", "setup_sec": -2}},
            "schema_failed dict": {"setup": {"schema_failed": {}, "seed_commit": "OK"}},
            "work expected zero": {"work": {"expected": 0, "succeeded": 0, "failed": 0, "skipped": 0}},
            "work negative": {"work": {"expected": 8, "succeeded": -1, "failed": 0, "skipped": 0}},
            "work bool": {"work": {"expected": 8, "succeeded": True, "failed": 0, "skipped": 0}},
            "work list": {"work": [8, 8, 0, 0]},
            "worker errors not dicts": {"worker_errors": [1]}, "worker error without text": {"worker_errors": [{"worker": 1}]},
            "failed verbs list": {"failed_verbs": {"expected": {}, "unexpected": []}},
            "failed verbs string counts": {"failed_verbs": {"expected": {"spine": "3"}, "unexpected": {}}},
            "failed verbs nested": {"failed_verbs": {"expected": {}, "unexpected": {"x": {"y": 1}}}},
            "invariants totals disagree": {"invariants": invariants(checked=2)},
            "invariants sample contradicts counts": {"invariants": invariants(results=[{"name": "S1.1", "passed": True, "detail": ""}, {"name": "S1.2", "passed": True, "detail": ""},
                                                                                       {"name": "S1.3", "passed": False, "detail": "d"}])},
            "invariants sample exceeds counts": {"invariants": invariants(results=[{"name": f"S1.{i}", "passed": True, "detail": ""} for i in range(4)])},
            "invariants partial sample with a failure": {"invariants": invariants(results=[{"name": "S1.9", "passed": False, "detail": "d"}])},
            "invariants results empty": {"invariants": invariants(results=[])},
            "invariants results shorter than counts": {"invariants": invariants(results=[{"name": "S1.1", "passed": True, "detail": ""}] * 2)},
            "invariants results below the cap": {"invariants": invariants(checked=500, passed=500, results=[{"name": "C", "passed": True, "detail": ""}] * 399)},
            "invariants results above the cap": {"invariants": invariants(checked=500, passed=500, results=[{"name": "C", "passed": True, "detail": ""}] * 401)},
            "invariants capped sample contradicts counts": {"invariants": invariants(checked=500, passed=500, results=[{"name": "C", "passed": False, "detail": ""}] * 400)},
            "integrity mismatches string": {"metrics": {"integrity_mismatches": "2"}},
            "integrity mismatches negative": {"metrics": {"integrity_mismatches": -1}},
            "integrity mismatches list": {"metrics": {"integrity_mismatches": [2]}},
            "integrity mismatches bool": {"metrics": {"integrity_mismatches": True}},
            "invariants all_passed lies": {"invariants": invariants(all_passed=False)},
            "invariants results not list": {"invariants": invariants(results={})},
            "invariant result passed string": {"invariants": invariants(results=[{"name": "S1.1", "passed": "yes", "detail": ""}] * 3)},
            "invariant result nameless": {"invariants": invariants(results=[{"passed": True, "detail": ""}] * 3)},
            "invariants enabled int": {"invariants": invariants(enabled=1)},
            "unsupported ops dicts": {"unsupported_ops": [{}]}, "unsupported ops string": {"unsupported_ops": "MERGE"},
            "op counts missing ok_seconds": {"op_status_counts": {"BRANCH": {"ok": 1, "unsupported": 0, "failed": 0}}},
            "op counts negative": {"op_status_counts": {"BRANCH": {"ok": -1, "unsupported": 0, "failed": 0, "ok_seconds": 0}}},
            "op counts list": {"op_status_counts": [{"ok": 1}]},
            "fallbacks negative": {"commit_ref_fallbacks": -1}, "fallbacks float": {"commit_ref_fallbacks": 1.0},
            "metrics list": {"metrics": [1]}, "metric fallbacks string": {"metrics": {"history_ref_fallbacks": "2"}},
            "implementation verbs list": {"implementation": {"verbs": ["merge"], "notes": {}}},
            "capabilities list": {"capabilities": ["merge"]},
        }
        for name, changes in shapes.items():
            with self.subTest(name):
                status, reasons = classify(good_evidence(**changes), CHILD)
                self.assertEqual(status, "unverified", reasons)
                self.assertTrue(reasons)
        # scrubbed() keeps at most 400 results; a Full campaign with more checks keeps exactly the cap.
        many = invariants(checked=500, passed=500, results=[{"name": f"C{i}", "passed": True, "detail": ""} for i in range(400)])
        self.assertEqual(classify(good_evidence(invariants=many), CHILD), ("verified", []))
        exact = invariants(checked=400, passed=400, results=[{"name": f"C{i}", "passed": True, "detail": ""} for i in range(400)])
        self.assertEqual(classify(good_evidence(invariants=exact), CHILD), ("verified", []))
        raw = good_evidence(invariants=invariants(checked=450, passed=450, results=[{"name": f"C{i}", "passed": True, "detail": ""} for i in range(450)]))
        self.assertEqual(classify(evidence_summary(raw), CHILD), ("verified", []), "the summary's own truncation is accepted")

    def test_integrity_mismatches_contradict_green_work_and_checks(self):
        contradictory = good_evidence(scenario="data_agent", metrics={"integrity_mismatches": 2})
        status, reasons = classify(contradictory, CHILD)
        self.assertEqual(status, "failed")
        self.assertIn("2 ingest integrity mismatches", reasons[0])
        self.assertEqual(classify(good_evidence(scenario="data_agent", metrics={"integrity_mismatches": 0, "rows_loaded": 8}), CHILD), ("verified", []))

    def test_expected_spine_failures_need_recovered_traffic(self):
        spine = {"expected": {"spine": 3}, "unexpected": {}}
        self.assertEqual(classify(good_evidence(failed_verbs=spine, metrics={"spine_attempted": 12, "spine_failed": 3}), CHILD), ("verified", []))
        status, reasons = classify(good_evidence(failed_verbs=spine, metrics={"spine_attempted": 3, "spine_failed": 3}), CHILD)
        self.assertEqual(status, "failed")
        self.assertIn("every background spine transaction failed", reasons[0])
        self.assertEqual(classify(good_evidence(failed_verbs=spine), CHILD)[0], "failed", "no evidence of any success is not recovery")
        self.assertEqual(classify(good_evidence(metrics={"spine_attempted": 3, "spine_failed": 3}), CHILD)[0], "verified",
                         "without an expected spine failure to explain, the counters are diagnostics")
        self.assertEqual(classify(good_evidence(metrics={"spine_failed": "3"}), CHILD)[0], "unverified")

    def test_how_the_child_ended_bounds_the_verdict(self):
        self.assertEqual(classify(None, {**CHILD, "ended": "killed", "exit_code": None})[0], "timed_out")
        self.assertEqual(classify(None, {**CHILD, "exit_code": 2})[0], "unverified")
        self.assertEqual(classify(None, CHILD)[0], "unverified", "exit 0 without a result proves nothing")
        status, reasons = classify(good_evidence(), {**CHILD, "ended": "killed", "exit_code": None})
        self.assertEqual(status, "timed_out", "a child the parent had to kill is never verified, even with a result")
        self.assertIn("deadline", reasons[0])
        for ended in ("interrupted", "launch_failed"):
            with self.subTest(ended):
                for evidence in (None, good_evidence()):
                    status, reasons = classify(evidence, {**CHILD, "ended": ended, "exit_code": None, "error": "OSError: spawn failed"})
                    self.assertEqual(status, "unverified")
                    self.assertTrue(reasons)

    def test_evidence_summary_is_allowlisted_and_scrubbed(self):
        raw = good_evidence(metrics={"uri": "postgresql://u:secret@h/db", "nan": float("nan"), "deep": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}},
                            e2e_private_path="/tmp/x.parquet")
        summary = evidence_summary(raw)
        self.assertNotIn("e2e_private_path", summary)
        self.assertEqual(summary["metrics"]["uri"], "postgresql://u:***@h/db")
        self.assertIsNone(summary["metrics"]["nan"])
        self.assertIsNone(summary["metrics"]["deep"]["a"]["b"]["c"]["d"]["e"])


def attempt_payload(**changes) -> dict:
    meta = json.loads((BOARD / "dolt_mysql" / "system.json").read_text())
    attempt_id, scenario = changes.get("attempt_id", "bb_0123abcd"), changes.get("scenario", "rl_env")
    campaign = changes.get("campaign", CAMPAIGN)
    return {
        "schema_version": 2, "suite": "macro-1", "attempt_id": attempt_id, "campaign": copy.deepcopy(campaign), **meta,
        "scenario": scenario, "date": "2026-10-10", "machine": "box", "version": "1.81.2",
        "outcome": {"status": "verified", "reasons": [], "elapsed_sec": 12.5, "setup_sec": 1.5, "completion_sec": 12.5},
        "cleanup": {"status": "verified", "detail": f"{attempt_id} deleted and absent"},
        "child": {**CHILD, "deadline_sec": 1800, "log_tail": "ok"},
        "evidence": evidence_summary(good_evidence(run_id=attempt_id, scenario=scenario)),
        **copy.deepcopy(PROVENANCE),
        "config": config_block(scenario if scenario in SCENARIOS else "rl_env", campaign.get("preset", "mini") if isinstance(campaign, dict) else "mini"),
        **changes,
    }


def campaign_record(**changes) -> dict:
    meta = json.loads((BOARD / "dolt_mysql" / "system.json").read_text())
    return {"schema_version": 2, "suite": "macro-1", "record": "campaign", **CAMPAIGN, "system": meta["system"],
            "finished": "2026-10-10T11:00:00+00:00", "aborted": None, "server": {"started_here": True, "stop": "stopped"},
            "attempts": {}, "version": "1.81.2", **copy.deepcopy(PROVENANCE), **changes}


def outcome(status: str, reasons: list[str], elapsed=12.5, setup=1.5) -> dict:
    return {"status": status, "reasons": reasons, "elapsed_sec": elapsed, "setup_sec": setup, "completion_sec": elapsed if status == "verified" else None}


class Contract(unittest.TestCase):
    def setUp(self):
        self.board = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.board)
        for system in ("dolt", "dolt_mysql"):
            (self.board / system).mkdir()
            shutil.copy(BOARD / system / "system.json", self.board / system)

    def write(self, payload: dict, system: str = "dolt_mysql", day: str = "20261010", name: str | None = None) -> Path:
        stem = name or (f"campaign_{payload['id']}" if "id" in payload else payload["attempt_id"])
        path = self.board / system / "macro" / day / f"{stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_valid_attempt_and_campaign_pass(self):
        self.assertEqual(validate(self.write(attempt_payload()), self.board), [])
        unverified = attempt_payload(evidence=None, outcome=outcome("unverified", ["no result"], None, None), attempt_id="bb_0123abce")
        self.assertEqual(validate(self.write(unverified), self.board), [])
        killed = attempt_payload(child={**CHILD, "ended": "killed", "exit_code": None}, attempt_id="bb_0123abcf",
                                 outcome=outcome("timed_out", ["the parent ended the child at its deadline"]))
        self.assertEqual(validate(self.write(killed), self.board), [])
        self.assertEqual(validate_campaign(self.write(campaign_record(attempts={"rl_env": "bb_0123abcd"})), self.board), [])
        self.assertEqual(validate_campaign(self.write(campaign_record(finished=None, version=None, server={"started_here": False, "stop": "pending"})), self.board), [])

    def test_bad_attempts_are_rejected(self):
        full = {**CAMPAIGN, "preset": "full"}
        bad = {
            "renamed": (attempt_payload(), {"name": "bb_0123abce"}),
            "wrong date dir": (attempt_payload(), {"day": "20261011"}),
            "other system": (attempt_payload(), {"system": "dolt"}),
            "unknown scenario": (attempt_payload(scenario="s7"), {}),
            "scenario outside campaign": (attempt_payload(scenario="ops_agent", campaign={**CAMPAIGN, "scenarios": ["rl_env"]}), {}),
            "completion time without verification": (attempt_payload(outcome={**outcome("failed", ["x"]), "completion_sec": 12.5}), {}),
            "verdict without reasons": (attempt_payload(outcome=outcome("failed", [])), {}),
            "verdict contradicts evidence": (attempt_payload(evidence=evidence_summary(good_evidence(unsupported_ops=["MERGE"]))), {}),
            "verdict without evidence": (attempt_payload(evidence=None), {}),
            "verdict from a killed child": (attempt_payload(child={**CHILD, "ended": "killed", "exit_code": None}), {}),
            "timing differs from evidence": (attempt_payload(outcome=outcome("verified", [], 11.0)), {}),
            "setup timing differs": (attempt_payload(outcome=outcome("verified", [], 12.5, 2.0)), {}),
            "foreign evidence": (attempt_payload(evidence=evidence_summary(good_evidence(run_id="bb_ffffffff"))), {}),
            "evidence outside allowlist": (attempt_payload(evidence={**evidence_summary(good_evidence()), "parquet": "/tmp/x"}), {}),
            "non-finite": (attempt_payload(outcome=outcome("verified", [], float("inf"))), {}),
            "credential": (attempt_payload(child={**CHILD, "log_tail": "uri postgresql://u:hunter2@h/db"}), {}),
            "bad cleanup": (attempt_payload(cleanup={"status": "done", "detail": ""}), {}),
            "bad ending": (attempt_payload(child={**CHILD, "ended": "done"}), {}),
            "bad commit": (attempt_payload(source={**SOURCE, "commit": "abc"}), {}),
            "preset mismatch": (attempt_payload(config=config_block("rl_env", "full")), {}),
            "preset mismatch in campaign": (attempt_payload(campaign=full, config=config_block("rl_env", "mini")), {}),
            "digest not of the effective config": (attempt_payload(config={**config_block(), "config_sha256": "3" * 64}), {}),
            "base digest not the frozen preset": (attempt_payload(config={**config_block(), "base_config_sha256": "2" * 64}), {}),
            "effective config of another scenario": (attempt_payload(config=config_block("ops_agent")), {}),
            "effective config keeps the run id": (attempt_payload(config=(lambda c: {**c, "effective": {**c["effective"], "run_id": "x"},
                                                                                     "config_sha256": workload_digest({**c["effective"], "run_id": "x"})})(config_block())), {}),
            "extra key": (attempt_payload(score=1.0), {}),
            "bool as number": (attempt_payload(outcome=outcome("verified", [], True)), {}),
            "environment unknown shape": (attempt_payload(environment={"cpu": "x"}), {}),
            "environment bool count": (attempt_payload(environment={**ENV, "cpus": True}), {}),
            # Shapes that used to raise inside the validator must be plain problems.
            "campaign scenarios of dicts": (attempt_payload(campaign={**CAMPAIGN, "scenarios": [{}]}), {}),
            "campaign scenarios string": (attempt_payload(campaign={**CAMPAIGN, "scenarios": "rl_env"}), {}),
            "campaign list": (attempt_payload(campaign=[]), {}),
            "campaign id list": (attempt_payload(campaign={**CAMPAIGN, "id": ["89abcdef"]}), {}),
            "campaign preset dict": (attempt_payload(campaign={**CAMPAIGN, "preset": {}}), {}),
            "campaign preset list": (attempt_payload(campaign={**CAMPAIGN, "preset": [{}]}), {}),
            "campaign and config preset dict": (attempt_payload(campaign={**CAMPAIGN, "preset": {}}, config={**config_block(), "preset": {}}), {}),
            "config preset list": (attempt_payload(config={**config_block(), "preset": []}), {}),
            "reasons not strings": (attempt_payload(outcome=outcome("failed", [1])), {}),
            "tags not list": (attempt_payload(tags="x"), {}),
            "evidence list": (attempt_payload(evidence=[]), {}),
            "evidence nested lists": (attempt_payload(evidence=evidence_summary(good_evidence(work=[], invariants=[], failed_verbs=[]))), {}),
            "evidence negative counts": (attempt_payload(evidence=evidence_summary(good_evidence(work={"expected": -8, "succeeded": 0, "failed": 0, "skipped": 0}))), {}),
            "evidence NaN": (attempt_payload(evidence=evidence_summary(good_evidence(elapsed_sec=float("nan")))), {}),
            "evidence booleans": (attempt_payload(evidence=evidence_summary(good_evidence(commit_ref_fallbacks=False))), {}),
            "child dict fields": (attempt_payload(child={**CHILD, "deadline_sec": {"s": 60}}), {}),
            "secret key": (attempt_payload(evidence=evidence_summary(good_evidence(metrics={"api_key": 1}))), {}),
        }
        for name, (payload, where) in bad.items():
            with self.subTest(name):
                self.assertTrue(validate(self.write(payload, **where), self.board), name)
        (self.board / "dolt_mysql" / "macro" / "20261010" / "bb_0123abcd.json").write_text("{")
        self.assertTrue(validate(self.board / "dolt_mysql" / "macro" / "20261010" / "bb_0123abcd.json", self.board))
        (self.board / "dolt_mysql" / "macro" / "20261010" / "bb_0123abcd.json").write_text("[1, 2]")
        self.assertTrue(validate(self.board / "dolt_mysql" / "macro" / "20261010" / "bb_0123abcd.json", self.board))

    def test_bad_campaign_records_are_rejected(self):
        bad = {
            "renamed": (campaign_record(), {"name": "campaign_00000000"}),
            "not a campaign": (campaign_record(record="attempt"), {}),
            "other system": (campaign_record(), {"system": "dolt"}),
            "unknown stop": (campaign_record(server={"started_here": True, "stop": "maybe"}), {}),
            "server extra": (campaign_record(server={"started_here": True, "stop": "stopped", "pid": 1}), {}),
            "attempts of unknown scenario": (campaign_record(attempts={"s7": "bb_00000001"}), {}),
            "attempts outside selection": (campaign_record(scenarios=["rl_env"], attempts={"ops_agent": "bb_00000001"}), {}),
            "attempts same id": (campaign_record(attempts={"rl_env": "bb_00000001", "ops_agent": "bb_00000001"}), {}),
            "attempts list": (campaign_record(attempts=["bb_00000001"]), {}),
            "attempt id dict": (campaign_record(attempts={"rl_env": {}}), {}),
            "scenarios of dicts": (campaign_record(scenarios=[{}]), {}),
            "preset dict": (campaign_record(preset={}), {}),
            "preset list": (campaign_record(preset=[{}]), {}),
            "aborted list": (campaign_record(aborted=["x"]), {}),
            "empty version": (campaign_record(version=" "), {}),
            "bad lock": (campaign_record(lock_sha256="x"), {}),
            "credential in abort": (campaign_record(aborted="postgresql://u:hunter2@h/db refused"), {}),
            "attempt file": (attempt_payload(), {"name": "campaign_89abcdef"}),
        }
        for name, (payload, where) in bad.items():
            with self.subTest(name):
                self.assertTrue(validate_campaign(self.write(payload, **where), self.board), name)

    def write_campaign(self, campaign: dict, scenarios, ids: str = "1", **record_changes) -> dict:
        attempts = {}
        for i, key in enumerate(scenarios):
            attempt_id = f"bb_{ids}000000{i}"
            attempts[key] = attempt_id
            self.write(attempt_payload(attempt_id=attempt_id, scenario=key, campaign=campaign, date=campaign["started"][:10]),
                       day=campaign["started"][:10].replace("-", ""))
        record = campaign_record(**{k: v for k, v in campaign.items()}, attempts=attempts, **record_changes)
        self.write(record, day=campaign["started"][:10].replace("-", ""))
        return record

    @staticmethod
    def reason(excluded: list[dict], campaign_id: str) -> str:
        [x] = [x for x in excluded if x["source"].endswith(f"campaign_{campaign_id}.json")]
        return x["reason"]

    def test_only_campaigns_that_pass_validation_are_results(self):
        older = {**CAMPAIGN, "id": "01234567", "started": "2026-10-09T10:00:00+00:00"}
        self.write_campaign(older, SCENARIOS, "0")
        newer = {**CAMPAIGN, "id": "89abcdef", "started": "2026-10-10T10:00:00+00:00", "scenarios": ["rl_env", "ops_agent"]}
        self.write(attempt_payload(attempt_id="bb_1000000a", campaign=newer))
        self.write(attempt_payload(attempt_id="bb_1000000b", scenario="ops_agent", campaign=newer,
                                   cleanup={"status": "failed", "detail": "still there"},
                                   outcome=outcome("failed", ["2 of 8 work units failed"], 3, 1.5),
                                   evidence=evidence_summary(good_evidence(run_id="bb_1000000b", scenario="ops_agent", elapsed_sec=3,
                                                                           work={"expected": 8, "succeeded": 6, "failed": 2, "skipped": 0}))))
        self.write(campaign_record(**newer, attempts={"rl_env": "bb_1000000a", "ops_agent": "bb_1000000b"}, aborted="stopped after S5 Ops agent: cleanup failed"))
        (self.board / "dolt" / "macro" / "20261010").mkdir(parents=True)
        (self.board / "dolt" / "macro" / "20261010" / "bb_deadbeef.json").write_text("not json")
        (self.board / "dolt" / "macro" / "20261010" / "campaign_deadbeef.json").write_text("[]")
        self.write(attempt_payload(attempt_id="bb_1000000c", campaign={**newer, "id": "aaaaaaaa"}))  # no campaign record
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual([c["campaign_id"] for c in campaigns], ["01234567"], "a campaign that fails validation is never a result")
        [old] = campaigns
        self.assertEqual(old["environment"], ENV)
        self.assertFalse({"dirty", "aborted", "conflicts", "duplicates", "server"} & set(old), "a result carries no validation caveats")
        self.assertEqual(sorted(x["source"].split("/")[-1] for x in excluded), ["bb_1000000c.json", "bb_deadbeef.json", "campaign_89abcdef.json", "campaign_deadbeef.json"])
        self.assertIn("no campaign record", next(x["reason"] for x in excluded if "bb_1000000c" in x["source"]))
        reason = self.reason(excluded, "89abcdef")
        for part in ("failed validation: ", "campaign aborted: stopped after S5 Ops agent", "subset run: 2 of 6 workflows selected", "database cleanup not verified: S5 Ops agent"):
            self.assertIn(part, reason)
        self.assertNotIn("not verified: S1", reason)
        full = {**older, "id": "fedcba98", "preset": "full", "started": "2026-10-11T10:00:00+00:00"}
        self.write_campaign(full, SCENARIOS, "2")
        self.write(attempt_payload(attempt_id="bb_20000004", scenario="ops_agent", campaign=full, date="2026-10-11",
                                   outcome=outcome("failed", ["2 of 8 work units failed"], 3, 1.5),
                                   evidence=evidence_summary(good_evidence(run_id="bb_20000004", scenario="ops_agent", elapsed_sec=3,
                                                                           work={"expected": 8, "succeeded": 6, "failed": 2, "skipped": 0}))), day="20261011")
        campaigns, _ = build_macro.build(self.board)
        self.assertEqual(campaigns[0]["campaign_id"], "fedcba98", "a failed workflow is a result, not a validation failure")
        self.assertEqual(campaigns[0]["attempts"]["ops_agent"]["outcome"]["status"], "failed")
        self.assertNotEqual(campaigns[0]["group"], old["group"], "another workload size is another comparison")
        dirty = {**full, "id": "fedcba99", "started": "2026-10-12T10:00:00+00:00"}
        self.write(attempt_payload(attempt_id="bb_30000000", campaign=dirty, date="2026-10-12", source={**SOURCE, "dirty": True}), day="20261012")
        self.write(campaign_record(**dirty, attempts={"rl_env": "bb_30000000"}), day="20261012")
        campaigns, excluded = build_macro.build(self.board)
        self.assertNotIn("fedcba99", [c["campaign_id"] for c in campaigns])
        self.assertIn("run from uncommitted sources", self.reason(excluded, "fedcba99"), "a dirty attempt dirties the campaign, whatever its record says")

    def test_group_key_binds_source_runtime_and_environment(self):
        self.write_campaign({**CAMPAIGN, "preset": "full"}, SCENARIOS)
        [base], _ = build_macro.build(self.board)
        for field, value in (("source_sha256", "f" * 64), ("python", "3.12.0"), ("lock_sha256", "e" * 64), ("platform", "Linux 6.1 x86_64"),
                             ("environment", {**ENV, "cpus": 4}), ("machine", "other")):
            with self.subTest(field):
                changed = copy.deepcopy(base)
                changed[field] = value
                self.assertNotEqual(build_macro.group_key(changed), base["group"])
        same_system_other_version = copy.deepcopy(base)
        same_system_other_version["version"] = "9.9.9"
        self.assertEqual(build_macro.group_key(same_system_other_version), base["group"], "the system under test is per row, not per group")

    def test_inconsistent_campaigns_fail_validation_not_cherry_picked(self):
        record = self.write_campaign(CAMPAIGN, ["rl_env", "ops_agent"])
        self.write(attempt_payload(attempt_id="bb_1000000f", scenario="rl_env", outcome=outcome("failed", ["2 of 8 work units failed"]),
                                   evidence=evidence_summary(good_evidence(run_id="bb_1000000f", work={"expected": 8, "succeeded": 6, "failed": 2, "skipped": 0}))))
        self.write(attempt_payload(attempt_id="bb_10000001", scenario="ops_agent", version="9.9.9", python="3.12.0", campaign=CAMPAIGN))
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual(campaigns, [], "neither duplicate is chosen")
        reason = self.reason(excluded, CAMPAIGN["id"])
        for part in ("2 attempts in one campaign (bb_10000000, bb_1000000f)", "not the one the campaign record names", "version differs", "python differs"):
            self.assertIn(part, reason)
        self.assertEqual(record["attempts"]["ops_agent"], "bb_10000001")

    def test_missing_or_invalid_named_attempts_fail_validation(self):
        self.write_campaign(CAMPAIGN, ["rl_env", "ops_agent", "dev_agent"])
        (self.board / "dolt_mysql" / "macro" / "20261010" / "bb_10000001.json").write_text("{bad")
        (self.board / "dolt_mysql" / "macro" / "20261010" / "bb_10000002.json").unlink()
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual(campaigns, [])
        self.assertEqual(len(excluded), 2, "the invalid attempt file and the campaign it breaks")
        self.assertIn("invalid", next(x["reason"] for x in excluded if x["source"].endswith("bb_10000001.json")))
        reason = self.reason(excluded, CAMPAIGN["id"])
        for part in ("evidence missing or unreadable: ", "S5 Ops agent", "S4 Dev agent", "not run: S2 Context management, S3 Multi-agent, S6 Data agent"):
            self.assertIn(part, reason)
        self.assertNotIn("not run: S4", reason, "named attempts are unreadable, never 'not run'")

    def test_abort_before_the_first_child_and_a_stop_failure_fail_validation(self):
        self.write(campaign_record(attempts={}, aborted="RunError: a server already answers on 127.0.0.1:3306",
                                   server={"started_here": False, "stop": "not_started"}, version=None))
        unfinished = {**CAMPAIGN, "id": "00000001", "started": "2026-10-10T12:00:00+00:00"}
        self.write(campaign_record(**unfinished, finished=None, server={"started_here": True, "stop": "pending"}))
        stop_failed = {**CAMPAIGN, "id": "00000002", "started": "2026-10-10T13:00:00+00:00"}
        self.write_campaign(stop_failed, SCENARIOS, server={"started_here": True, "stop": "failed"}, aborted="server stop failed: did not stop")
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual(campaigns, [])
        self.assertEqual(len(excluded), 3)
        self.assertIn("server stop failed", self.reason(excluded, "00000002"))
        self.assertIn("campaign did not finish", self.reason(excluded, "00000001"))
        self.assertIn("server stop pending", self.reason(excluded, "00000001"))
        aborted = self.reason(excluded, "89abcdef")
        self.assertIn("campaign aborted: RunError", aborted)
        self.assertIn("not run: S1 RL environment, S2 Context management, S3 Multi-agent, S4 Dev agent, S5 Ops agent, S6 Data agent", aborted)


class Runner(unittest.TestCase):
    """run_macro with the server, connector and child replaced; the lifecycle is real."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.board = self.root / "leaderboard"
        (self.board / "dolt_mysql").mkdir(parents=True)
        shutil.copy(BOARD / "dolt_mysql" / "system.json", self.board / "dolt_mysql")
        (self.root / "uv.lock").write_text("fixture\n")
        (self.board / ".gitignore").write_text(".runs/\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.out = self.board  # generated attempts under the board must not count as source changes
        self.server = mock.Mock(needs_psql=False, backend=7)
        self.server.scope.return_value = {"host": "127.0.0.1", "port": 1}
        self.server.exists.return_value = False
        self.server.version.return_value = "1.81.2"
        self.runs = self.board / ".runs"
        self.pid = self.runs / "dolt_mysql.pid"

        def start(sysdir):  # like the start scripts: the pid file is the evidence of what was launched
            self.runs.mkdir(exist_ok=True)
            self.pid.write_text(f"{os.getpid()}\n")

        def lifecycle(sysdir, step):  # like the stop scripts: a successful stop removes the pid file
            if step == "stop" and self.stop_ok:
                self.pid.unlink(missing_ok=True)
            return self.stop_ok
        self.stop_ok = True
        for module, attr, value in ((micro, "ROOT", self.root), (micro, "BOARD", self.board), (micro, "RUNS", self.runs),
                                    (run_macro, "ROOT", self.root), (run_macro, "BOARD", self.board),
                                    (run_macro, "MACRO_RUNS", self.runs / "macro"),
                                    (run_macro, "CODE_PATHS", ["leaderboard", "uv.lock", ":!leaderboard/*/macro/*"]),
                                    (micro, "start_server", mock.Mock(side_effect=start)), (micro, "lifecycle", mock.Mock(side_effect=lifecycle)),
                                    (run_macro, "load_connector", mock.Mock(return_value=self.server)),
                                    (run_macro, "port_answers", mock.Mock(return_value=False))):
            patch = mock.patch.object(module, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.children = []
        self.evidence = lambda run_id, key: good_evidence(run_id=run_id, scenario=key)
        self.child = {**CHILD}

        def fake_child(config, outdir, deadline_sec):
            self.children.append(config.read_text())
            run_id = outdir.name
            key = next((k for k in SCENARIOS if f"{k} {{" in self.children[-1]), "rl_env")
            evidence = self.evidence(run_id, key)
            if evidence is not None:
                (outdir / f"{run_id}_e2e_stats.json").write_text(evidence if isinstance(evidence, str) else json.dumps(evidence))
            self.assertTrue(micro.journal("dolt_mysql").exists(), "the journal exists while the child runs")
            return {**self.child, "deadline_sec": deadline_sec}
        patch = mock.patch.object(run_macro, "launch_child", side_effect=fake_child)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=t@example.invalid", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def campaign(self, keys=("rl_env",), **kwargs) -> int:
        return run_macro.run("dolt_mysql", kwargs.get("preset", "mini"), list(keys), "box", self.out, kwargs.get("deadline_min", 1))

    def attempts(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.out / "dolt_mysql" / "macro").rglob("bb_*.json"))]

    def record(self) -> dict:
        [path] = (self.out / "dolt_mysql" / "macro").rglob("campaign_*.json")
        return json.loads(path.read_text())

    def stopped(self) -> None:
        micro.lifecycle.assert_called_once_with(self.board / "dolt_mysql", "stop")

    def test_verified_attempt_is_published_with_provenance_and_clean_state(self):
        self.assertEqual(self.campaign(), 0)
        [a] = self.attempts()
        self.assertEqual((a["outcome"]["status"], a["cleanup"]["status"]), ("verified", "verified"))
        self.assertEqual(a["outcome"]["completion_sec"], 12.5)
        path = self.out / "dolt_mysql" / "macro" / a["date"].replace("-", "") / f"{a['attempt_id']}.json"
        self.assertEqual(validate(path, self.board), [])
        self.assertFalse(a["source"]["dirty"])
        self.assertEqual(a["source"]["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(a["lock_sha256"], micro.sha256(self.root / "uv.lock"))
        self.assertEqual(set(a["environment"]), {"cpu", "cpus", "memory_bytes"})
        self.assertFalse(micro.journal("dolt_mysql").exists())
        self.assertFalse(micro.journal("dolt_mysql").exists())
        self.server.delete.assert_called_once_with(a["attempt_id"])
        self.stopped()
        config = self.children[0]
        self.assertIn(f'db_name: "{a["attempt_id"]}"', config)
        self.assertNotIn("cleanup: true", config)
        self.assertIn("backend: DOLT_MYSQL", config)
        self.assertNotIn("existing_db", config)
        self.assertEqual(a["config"]["preset"], "mini")
        self.assertEqual(a["config"]["effective"]["workload"]["rl_env"]["tasks"], 2, "the complete normalized workload is stored")
        self.assertNotIn("run_id", a["config"]["effective"])
        record = self.record()
        self.assertEqual(validate_campaign(path.parent / f"campaign_{record['id']}.json", self.board), [])
        self.assertEqual(record["attempts"], {"rl_env": a["attempt_id"]})
        self.assertEqual(record["server"], {"started_here": True, "stop": "stopped"})
        self.assertIsNone(record["aborted"])
        self.assertTrue(record["finished"])
        self.assertEqual(a["campaign"], {k: record[k] for k in ("id", "preset", "scenarios", "machine", "started")})
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual((campaigns, [x["reason"] for x in excluded]), ([], ["failed validation: subset run: 1 of 6 workflows selected"]),
                         "a one-workflow test campaign fails only the all-six check")

    def test_workload_identity_ignores_run_ids_and_backend_only(self):
        base = config_path("rl_env", "mini")
        _, dolt = run_macro.effective_config(base, 1, "bb_00000001")
        _, dolt_mysql = run_macro.effective_config(base, 7, "bb_00000002")
        _, full = run_macro.effective_config(config_path("rl_env", "full"), 1, "bb_00000001")
        self.assertEqual(workload_digest(dolt), workload_digest(dolt_mysql))
        self.assertNotEqual(workload_digest(dolt), workload_digest(full))
        self.assertEqual(dolt["database_setup"], {"generated": {}})

    def test_each_failure_shape_is_published_and_cleaned_up(self):
        shapes = {
            "no result": (lambda run_id, key: None, {}, "unverified"),
            "killed": (lambda run_id, key: None, {"ended": "killed", "exit_code": None}, "timed_out"),
            "killed after writing": (lambda run_id, key: good_evidence(run_id=run_id), {"ended": "killed", "exit_code": None}, "timed_out"),
            "malformed": (lambda run_id, key: "{not json", {}, "unverified"),
            "malformed nested": (lambda run_id, key: good_evidence(run_id=run_id, invariants={"enabled": "yes", "results": 3}), {}, "unverified"),
            "foreign": (lambda run_id, key: good_evidence(run_id="bb_ffffffff"), {}, "unverified"),
            "setup failed": (lambda run_id, key: good_evidence(run_id=run_id, status="setup_failed: SetupFailed: seed commit FAILED",
                                                               setup={"schema_failed": [], "seed_commit": "FAILED"}, work={"expected": 8, "succeeded": 0, "failed": 0, "skipped": 0}), {"exit_code": 2}, "failed"),
            "worker exception": (lambda run_id, key: good_evidence(run_id=run_id, worker_errors=[{"worker": 1, "item": "1", "error": "RuntimeError: boom"}]), {}, "failed"),
            "unsupported": (lambda run_id, key: good_evidence(run_id=run_id, unsupported_ops=["REBASE"], work={"expected": 8, "succeeded": 7, "failed": 0, "skipped": 1}), {}, "incomplete"),
            "fallback": (lambda run_id, key: good_evidence(run_id=run_id, commit_ref_fallbacks=1), {}, "incomplete"),
            "recovered faults": (lambda run_id, key: good_evidence(run_id=run_id, failed_verbs={"expected": {"spine": 4}, "unexpected": {}},
                                                                    metrics={"spine_attempted": 12, "spine_failed": 4}), {}, "verified"),
            "unrecovered faults": (lambda run_id, key: good_evidence(run_id=run_id, failed_verbs={"expected": {"spine": 4}, "unexpected": {}},
                                                                      metrics={"spine_attempted": 4, "spine_failed": 4}), {}, "failed"),
            "timed out": (lambda run_id, key: good_evidence(run_id=run_id, timed_out=True, status="interrupted"), {}, "timed_out"),
        }
        for name, (evidence, child, expected) in shapes.items():
            with self.subTest(name):
                shutil.rmtree(self.out / "dolt_mysql" / "macro", ignore_errors=True)
                self.server.delete.reset_mock()
                self.evidence, self.child = evidence, {**CHILD, **child}
                self.assertEqual(self.campaign(), 0 if expected == "verified" else 1)
                [a] = self.attempts()
                self.assertEqual(a["outcome"]["status"], expected)
                self.assertEqual(a["cleanup"]["status"], "verified")
                self.assertIsNone(a["outcome"]["completion_sec"] if expected != "verified" else None)
                self.assertEqual(validate(self.out / "dolt_mysql" / "macro" / a["date"].replace("-", "") / f"{a['attempt_id']}.json", self.board), [])
                self.server.delete.assert_called_once()
                self.assertFalse(micro.journal("dolt_mysql").exists())

    def test_launch_failure_still_cleans_up_and_publishes(self):
        run_macro.launch_child.side_effect = OSError("spawn failed")
        self.assertEqual(self.campaign(("rl_env", "ops_agent")), 1)
        attempts = self.attempts()
        self.assertEqual(len(attempts), 2, "a launch failure is an attempt's outcome, so the campaign goes on")
        for a in attempts:
            self.assertEqual(a["outcome"]["status"], "unverified")
            self.assertEqual(a["child"]["ended"], "launch_failed")
            self.assertIn("spawn failed", a["outcome"]["reasons"][0])
            self.assertEqual(a["cleanup"]["status"], "verified")
        self.assertEqual(self.server.delete.call_count, 2)
        self.assertFalse(micro.journal("dolt_mysql").exists())
        self.stopped()
        self.assertIsNone(self.record()["aborted"])

    def test_unreadable_evidence_still_cleans_up_and_publishes(self):
        with mock.patch.object(run_macro, "read_evidence", side_effect=TypeError("nested parsing exploded")):
            self.assertEqual(self.campaign(), 1)
        [a] = self.attempts()
        self.assertEqual((a["outcome"]["status"], a["child"]["ended"], a["cleanup"]["status"]), ("unverified", "exited", "verified"))
        self.assertIn("could not be read", a["outcome"]["reasons"][0])
        self.server.delete.assert_called_once()
        self.assertFalse(micro.journal("dolt_mysql").exists())
        self.stopped()

    def test_interrupt_cleans_up_publishes_and_marks_the_campaign(self):
        run_macro.launch_child.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.campaign(("rl_env", "ops_agent"))
        [a] = self.attempts()
        self.assertEqual((a["outcome"]["status"], a["child"]["ended"], a["cleanup"]["status"]), ("unverified", "interrupted", "verified"))
        self.server.delete.assert_called_once()
        self.assertFalse(micro.journal("dolt_mysql").exists())
        self.stopped()
        record = self.record()
        self.assertEqual((record["aborted"], record["server"]["stop"], list(record["attempts"])), ("interrupted", "stopped", ["rl_env"]))
        self.assertTrue(record["finished"])

    def test_cleanup_failure_keeps_journal_and_stops_the_campaign(self):
        self.server.delete.side_effect = PermissionError("drop refused")
        self.assertEqual(self.campaign(("rl_env", "ops_agent")), 1)
        [a] = self.attempts()
        self.assertEqual(a["outcome"]["status"], "verified", "the workflow verdict is not rewritten by cleanup")
        self.assertEqual(a["cleanup"]["status"], "failed")
        self.assertIn("drop refused", a["cleanup"]["detail"])
        self.assertIn("cleanup failed", self.record()["aborted"])
        self.assertTrue(micro.journal("dolt_mysql").exists())
        self.stopped()

    def test_database_still_present_after_delete_keeps_the_journal(self):
        self.server.exists.return_value = True
        self.assertEqual(self.campaign(), 1)
        [a] = self.attempts()
        self.assertEqual(a["cleanup"]["status"], "failed")
        self.assertIn("still exists", a["cleanup"]["detail"])
        self.assertEqual(json.loads(micro.journal("dolt_mysql").read_text())["name"], a["attempt_id"])

    def test_exists_check_error_is_unknown_and_keeps_the_journal(self):
        self.server.exists.side_effect = OSError("connection lost")
        self.assertEqual(self.campaign(("rl_env", "ops_agent")), 1)
        [a] = self.attempts()
        self.assertEqual(a["cleanup"]["status"], "unknown")
        self.assertIn("connection lost", a["cleanup"]["detail"])
        self.assertEqual(json.loads(micro.journal("dolt_mysql").read_text())["name"], a["attempt_id"], "recovery evidence stays until absence is proven")
        self.assertIn("cleanup unknown", self.record()["aborted"])

    def test_prior_journal_is_recovered_only_with_proof_of_absence(self):
        self.runs.mkdir(parents=True)
        micro.journal("dolt_mysql").write_text(json.dumps({"name": "bb_00000000", "scope": self.server.scope()}))
        self.server.exists.return_value = True
        self.assertEqual(self.campaign(), 1)
        self.assertEqual(self.children, [], "no new child while the old database is still there")
        self.server.delete.assert_called_once_with("bb_00000000")
        self.assertTrue(micro.journal("dolt_mysql").exists())
        self.assertIn("earlier run left behind", self.record()["aborted"])
        self.stopped()
        micro.lifecycle.reset_mock(); self.server.delete.reset_mock()
        shutil.rmtree(self.out / "dolt_mysql" / "macro")
        self.server.exists.return_value = False
        self.assertEqual(self.campaign(), 0)
        self.assertEqual(self.server.delete.call_args_list[0].args, ("bb_00000000",))
        self.assertEqual(len(self.children), 1)
        self.assertFalse(micro.journal("dolt_mysql").exists())

    def test_leftover_journal_from_another_scope_stops_before_any_child(self):
        self.runs.mkdir(parents=True)
        micro.journal("dolt_mysql").write_text(json.dumps({"name": "bb_00000000", "scope": {"host": "elsewhere"}}))
        self.assertEqual(self.campaign(), 1)
        self.assertEqual(self.children, [])
        self.assertTrue(micro.journal("dolt_mysql").exists())
        self.server.delete.assert_not_called()
        self.stopped()
        record = self.record()
        self.assertEqual(record["attempts"], {})
        self.assertIn("different or missing target scope", record["aborted"])

    def test_a_dirty_tree_fails_validation_and_nothing_runs(self):
        (self.board / "extra.py").write_text("x = 1\n")
        with self.assertRaisesRegex(micro.RunError, "uncommitted"):
            self.campaign()
        micro.start_server.assert_not_called()
        self.assertEqual(self.children, [])
        self.assertFalse((self.out / "dolt_mysql" / "macro").exists(), "a run that fails validation writes no record")

    def test_sources_changed_during_the_run_are_recorded_not_hidden(self):
        def evidence(run_id, key):
            (self.root / "uv.lock").write_text("changed\n")
            return good_evidence(run_id=run_id)
        self.evidence = evidence
        self.assertEqual(self.campaign(), 0)
        [a] = self.attempts()
        self.assertFalse(a["source"]["dirty"])
        self.assertTrue(a["source"]["changed_during_run"])
        self.assertTrue(self.record()["source"]["changed_during_run"])

    def test_existing_server_on_the_port_is_never_touched(self):
        run_macro.port_answers.return_value = True
        with self.assertRaisesRegex(micro.RunError, "already answers"):
            self.campaign()
        micro.start_server.assert_not_called()
        micro.lifecycle.assert_not_called()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(list((self.out / "dolt_mysql").rglob("campaign_*.json")), [], "a preflight failure needs no campaign")

    def test_live_pid_file_from_elsewhere_is_never_claimed(self):
        self.runs.mkdir(parents=True)
        self.pid.write_text(f"{os.getpid()}\n")
        with self.assertRaisesRegex(micro.RunError, "live process"):
            self.campaign()
        micro.start_server.assert_not_called()
        micro.lifecycle.assert_not_called()

    def test_server_started_but_never_ready_is_still_stopped(self):
        def start(sysdir):
            self.runs.mkdir(exist_ok=True)
            self.pid.write_text(f"{os.getpid()}\n")
            raise micro.RunError("dolt_mysql: server failed its check for 60 s")
        micro.start_server.side_effect = start
        self.assertEqual(self.campaign(), 1)
        self.stopped()
        self.assertEqual(self.attempts(), [])
        record = self.record()
        self.assertEqual(record["server"], {"started_here": True, "stop": "stopped"})
        self.assertIn("failed its check", record["aborted"])
        self.assertIsNone(record["version"])

    def test_server_that_never_started_is_not_stopped(self):
        micro.start_server.side_effect = micro.RunError("dolt_mysql: install or start failed")
        self.assertEqual(self.campaign(), 1)
        micro.lifecycle.assert_not_called()
        record = self.record()
        self.assertEqual(record["server"], {"started_here": False, "stop": "not_started"})
        self.assertIn("install or start failed", record["aborted"])

    def test_version_failure_after_start_aborts_and_stops(self):
        self.server.version.side_effect = ConnectionError("handshake failed")
        self.assertEqual(self.campaign(), 1)
        self.stopped()
        self.assertEqual(self.attempts(), [])
        self.assertIn("ConnectionError: handshake failed", self.record()["aborted"])

    def test_stop_failure_is_recorded_separately(self):
        self.stop_ok = False
        self.assertEqual(self.campaign(), 1)
        [a] = self.attempts()
        self.assertEqual((a["outcome"]["status"], a["cleanup"]["status"]), ("verified", "verified"), "attempt records are not rewritten")
        self.assertNotIn("aborted", a["campaign"])
        record = self.record()
        self.assertEqual(record["server"]["stop"], "failed")
        self.assertIn("did not stop", record["aborted"])
        campaigns, [x] = build_macro.build(self.board)
        self.assertEqual(campaigns, [])
        self.assertIn("server stop failed", x["reason"])


class ChildProcess(unittest.TestCase):
    def test_child_is_killed_at_its_deadline_and_log_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as folder:
            outdir = Path(folder)
            real_run = subprocess.run
            with mock.patch.object(run_macro, "GRACE", 0), mock.patch.object(run_macro.subprocess, "run") as spawn:
                spawn.side_effect = lambda args, **kw: real_run(
                    [sys.executable, "-c", "import sys, time; print('uri mysql://root:@h/'); sys.stdout.flush(); time.sleep(5)"], **kw)
                child = run_macro.launch_child(outdir / "config.textproto", outdir, 1)
            self.assertEqual(child["ended"], "killed")
            self.assertIsNone(child["exit_code"])
            self.assertIn("root:***@", child["log_tail"])
            self.assertNotIn("root:@", child["log_tail"])


if __name__ == "__main__":
    unittest.main()
