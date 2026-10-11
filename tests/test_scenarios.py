"""Every scenario runs end to end on the fake backend (branch, commit,
log, diff, reset and delete only). Merge, rebase and revert come back
UNSUPPORTED, so these runs check the "never stop the workload" contract:
the lifecycle completes, unsupported verbs are recorded, and the
invariants that depend on them are marked not applicable."""

import pytest
from google.protobuf import text_format

from dblib import result_collector as rc
from macrobench import schema, task_pb2 as tp
from macrobench.datagen.ch import CHScale, seed_ch
from macrobench.scenarios import InvariantRecorder, ScenarioContext, create_scenario
from tests.fake_backend import FakeSuite

MINI_SCHEMA = ("schema { base: CH_BENCH scale_factor: 1 items: 60 customers_per_district: 10 "
               "orders_per_district: 10 suppliers: 5 skip_foreign_keys: true }")

CONFIGS = {
    "rl_env": MINI_SCHEMA + """
        workload { branch_ops { commit_interval: 1 }
                   data_ops { statements_per_step: 3 rows_per_write: 5 write_fraction: 0.5 ddl_fraction: 0.5 }
                   rl_env { tasks: 2 group_size: 2 step_budget: 2 historical_fork_prob: 1.0 } }""",
    "context_mgmt": """schema { base: NO_BASE }
        workload { branch_ops { commit_interval: 1 retention_steps: 2 }
                   data_ops { statements_per_step: 3 rows_per_write: 4 write_fraction: 0.5 }
                   context_mgmt { compaction_interval: 2 fanout: 3 compaction_cycles: 2 rejected_cycle: 1 } }""",
    "multi_agent": """schema { base: NO_BASE }
        workload { branch_ops { commit_interval: 1 }
                   data_ops { statements_per_step: 3 rows_per_write: 2 write_fraction: 0.5 }
                   multi_agent { agents: 2 rounds: 1 key_overlap: 0.5 spine_updates_per_round: 2 } }""",
    "dev_agent": MINI_SCHEMA + """
        workload { branch_ops { commit_interval: 1 }
                   data_ops { statements_per_step: 2 rows_per_write: 5 write_fraction: 0.8 }
                   dev_agent { dev_branches: 1 concurrent_branches: 1 dev_phase_steps: 2 review_phase_steps: 1
                               review_modification_prob: 1.0 rebases_per_branch: 1 spine_migration_interval: 1 key_overlap: 0.5 } }""",
    "ops_agent": MINI_SCHEMA + """
        workload { branch_ops { commit_interval: 1 }
                   data_ops { statements_per_step: 2 rows_per_write: 2 }
                   ops_agent { spine_commits: 5 fault_commit_offset: 2 investigation_branches: 2 history_offset: 4 } }""",
    "data_agent": MINI_SCHEMA + """
        workload { branch_ops { commit_interval: 1 }
                   data_ops { statements_per_step: 2 rows_per_write: 2 write_fraction: 0.8 }
                   data_agent { batches: 2 concurrent_batches: 1 batch_rows: 4 days_back: 2 steps_per_batch: 2 reset_prob: 1.0 } }""",
}

NEEDS_MERGE = {"context_mgmt", "multi_agent", "dev_agent", "data_agent"}


def _config(key: str) -> tp.MacroBenchConfig:
    cfg = tp.MacroBenchConfig()
    text_format.Parse('run_id: "t" database_setup { db_name: "t" } seed: 7 ' + CONFIGS[key], cfg)
    return cfg


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_scenario_runs_to_completion_on_fake_backend(key):
    cfg = _config(key)
    collector = rc.ResultCollector()
    suite = FakeSuite(collector)
    inv = InvariantRecorder()
    ctx = ScenarioContext(config=cfg, suite_factory=lambda: suite, collector=collector,
                          spine="main", invariants=inv, log=lambda m: None)
    scenario = create_scenario(ctx)
    scenario.validate()
    stats = schema.apply_schema(suite, "main", cfg.schema, key, log=lambda m: None)
    assert stats["failed"] == []
    if cfg.schema.base == tp.BaseSchema.CH_BENCH:
        seed_ch(suite, "main", CHScale.from_config(cfg.schema), log=lambda m: None)
    scenario.seed(suite)
    suite.commit("main", "seed", timed=False)

    scenario.run()  # must not raise

    support = collector.support_summary()
    summary = inv.summary()
    if key in NEEDS_MERGE:
        assert support["workflow_supported"] is False
        assert any(op.startswith(("MERGE", "REBASE")) for op in support["unsupported_ops"])
        assert summary["not_applicable"] >= 1
    else:
        assert support["workflow_supported"] is True, support
        assert summary["failed"] == 0, summary["results"]
        assert summary["passed"] >= 1
    assert summary["failed"] == 0, [r for r in summary["results"] if r["passed"] is False]
    # Every branch the scenario made was deleted again.
    assert suite.list_branches() == ["main"], suite.list_branches()
    # Every planned unit reached exactly one terminal outcome, none escaped as an exception.
    work = ctx.work
    assert work["succeeded"] + work["failed"] + work["skipped"] == scenario.total_units(), work
    assert work["failed"] == 0 and ctx.worker_errors == [], (work, ctx.worker_errors)
    assert (work["skipped"] > 0) == (key in NEEDS_MERGE), (key, work)
    assert collector.failed_verbs() == {}, collector.failed_verbs()


def _run(key, **overrides):
    cfg = _config(key)
    for name, value in overrides.items():
        setattr(getattr(cfg.workload, key), name, value)
    collector = rc.ResultCollector()
    suite = FakeSuite(collector)
    ctx = ScenarioContext(config=cfg, suite_factory=lambda: suite, collector=collector,
                          spine="main", invariants=InvariantRecorder(), log=lambda m: None)
    scenario = create_scenario(ctx)
    schema.apply_schema(suite, "main", cfg.schema, key, log=lambda m: None)
    if cfg.schema.base == tp.BaseSchema.CH_BENCH:
        seed_ch(suite, "main", CHScale.from_config(cfg.schema), log=lambda m: None)
    scenario.seed(suite)
    suite.commit("main", "seed", timed=False)
    return ctx, scenario, suite


def test_worker_exception_is_recorded_not_swallowed():
    ctx, scenario, suite = _run("rl_env")
    calls = {"n": 0}
    original = scenario._run_task

    def flaky(w, task_id):
        calls["n"] += 1
        if task_id == 2:
            raise RuntimeError("agent died")
        return original(w, task_id)

    scenario._run_task = flaky
    scenario.run()
    assert calls["n"] == 2
    assert [e["error"] for e in ctx.worker_errors] == ["RuntimeError: agent died"]
    assert ctx.work["succeeded"] < scenario.total_units(), ctx.work
    assert ctx.metrics["tasks_completed"] == 1


class _BranchlessSuite(FakeSuite):
    """branch() always fails: S1 can never grow a rollout leaf."""

    def _branch_impl(self, name, from_ref):
        raise RuntimeError("no branches today")


def test_s1_rollouts_are_bounded_and_report_their_shortfall():
    cfg = _config("rl_env")
    collector = rc.ResultCollector()
    suite = _BranchlessSuite(collector)
    ctx = ScenarioContext(config=cfg, suite_factory=lambda: suite, collector=collector,
                          spine="main", invariants=InvariantRecorder(), log=lambda m: None)
    scenario = create_scenario(ctx)
    schema.apply_schema(suite, "main", cfg.schema, "rl_env", log=lambda m: None)
    seed_ch(suite, "main", CHScale.from_config(cfg.schema), log=lambda m: None)
    suite.commit("main", "seed", timed=False)
    scenario.run()  # terminates without a runtime cap
    p = cfg.workload.rl_env
    assert ctx.metrics["leaf_shortfall"] == p.tasks * p.group_size
    assert ctx.metrics["leaves"] == 0
    assert ctx.work["failed"] == scenario.total_units(), ctx.work
    # Each leaf got at most two attempts, so the attempt count is fixed by config.
    branches = collector.support_summary()["op_status_counts"]["BRANCH"]
    assert branches["failed"] == p.tasks * (1 + 2 * p.group_size)
    failed = collector.failed_verbs()
    assert (failed["task_branch"], failed["rollout_branch"]) == (p.tasks, 2 * p.tasks * p.group_size)
    assert ctx.invariants.summary()["not_applicable"] == p.tasks


def test_history_ref_fallback_is_counted():
    ctx, scenario, suite = _run("ops_agent")
    original = suite._commit_impl
    suite._commit_impl = lambda ref, message: None  # commits succeed but return no hash
    try:
        scenario.run()
    finally:
        suite._commit_impl = original
    assert ctx.metrics["history_ref_fallbacks"] >= 1


def test_s6_integrity_mismatch_is_a_failed_unit_and_a_failed_check():
    """A load step whose derived revenue disagrees with the inserted order lines
    is corrupt output: never a succeeded unit, never a passed S6.2."""
    ctx, scenario, suite = _run("data_agent", reset_prob=0.0)
    original = scenario._load_step

    def tampered(*args, **kwargs):
        script = original(*args, **kwargs)

        def wrapped(db):
            result = script(db)  # the real SQL runs; only the integrity verdict is forged
            return {**result, "mismatches": 1}
        return wrapped

    scenario._load_step = tampered
    scenario.run()
    assert ctx.work["failed"] >= 1, ctx.work
    summary = ctx.invariants.summary()
    failed = [r["name"] for r in summary["results"] if r["passed"] is False]
    assert failed and all(n.startswith("S6.2") for n in failed), summary["results"]
    assert ctx.metrics["integrity_mismatches"] >= 1


def test_s5_spine_traffic_that_always_fails_leaves_evidence(monkeypatch):
    """Expected spine failures are only "recovered" while other spine transactions
    succeed; the metrics must show that all of production failed."""
    from macrobench import tpcc

    def broken(*args, **kwargs):
        raise RuntimeError("production is down")
    monkeypatch.setattr(tpcc, "run_transaction", broken)
    ctx, scenario, suite = _run("ops_agent")
    scenario.run()
    assert ctx.metrics["spine_attempted"] > 0
    assert ctx.metrics["spine_failed"] == ctx.metrics["spine_attempted"]
    assert ctx.collector.failed_verbs().get("spine") == ctx.metrics["spine_failed"]


class _ProbeLostSuite(FakeSuite):
    """Every untimed probe of the spine fails with a connection error."""

    def exec(self, script, refs=None, **kwargs):
        from dblib.db_api import ExecResult, OpStatus
        text = script if isinstance(script, str) else str(script)
        if kwargs.get("timed") is False and ("information_schema" in text or "loyalty_tier" in text):
            return [ExecResult(op="exec", status=OpStatus.FAILED, ref="main", refs=["main"], error="connection lost")]
        return super().exec(script, refs, **kwargs)


def test_s4_isolation_check_fails_when_the_spine_cannot_be_probed():
    """Only a missing-relation error proves the table is absent. A probe that
    fails for any other reason cannot pass S4.1."""
    from macrobench.scenarios.s4_dev_agent import _missing_relation
    assert _missing_relation('relation "loyalty_tier" does not exist')
    assert _missing_relation("Table 'x.loyalty_tier' doesn't exist") and _missing_relation("no such table: loyalty_tier")
    assert _missing_relation("table not found: loyalty_tier") and not _missing_relation("connection lost")
    cfg = _config("dev_agent")
    collector = rc.ResultCollector()
    suite = _ProbeLostSuite(collector)
    inv = InvariantRecorder()
    ctx = ScenarioContext(config=cfg, suite_factory=lambda: suite, collector=collector,
                          spine="main", invariants=inv, log=lambda m: None)
    scenario = create_scenario(ctx)
    schema.apply_schema(suite, "main", cfg.schema, "dev_agent", log=lambda m: None)
    seed_ch(suite, "main", CHScale.from_config(cfg.schema), log=lambda m: None)
    scenario.seed(suite)
    suite.commit("main", "seed", timed=False)
    scenario.run()
    s4_1 = [r for r in inv.summary()["results"] if r["name"].startswith("S4.1")]
    assert s4_1 and all(r["passed"] is False and "connection lost" in r["detail"] for r in s4_1), s4_1
