# Leaderboard Simplification Plan

Status: Implemented on 2026-10-21

This plan reorganizes database integration source code without changing benchmark execution or artifact paths. Fable reviewed the inventory and recommended this bounded change before production automation.

The batch descriptions below preserve the pre-change evidence and the approved migration sequence.

## Decision

Move every database system integration to `leaderboard/systems/<system-key>/`.

Keep generated and published artifacts at their current paths:

- `leaderboard/<system-key>/results/`
- `leaderboard/<system-key>/macro/`
- `leaderboard/<system-key>/verbs/`

The source tree then separates integration code from generated evidence. Existing tracked and untracked artifacts remain untouched.

The target structure is:

```text
leaderboard/
├── systems/
│   ├── chronos/
│   ├── dolt/
│   ├── dolt_mysql/
│   ├── file_copy/
│   ├── matrixone/
│   ├── neon/
│   ├── seekdb/
│   ├── tiger/
│   └── xata/
├── <system-key>/
│   ├── results/
│   ├── macro/
│   └── verbs/
├── docs/
├── requirements/
├── js/
├── workflow-diagrams/
└── existing runner, validation, publication, AWS, and site modules
```

An artifact-only system directory exists only after that system has produced or published evidence. A new integration pull request adds source under `leaderboard/systems/`, not at the package root.

## Safety net before the move

Add a characterization test for the artifact path rule that current tests do not state directly:

1. Moving integration source does not change micro, macro, or verb artifact paths.

This test must pass against the current behavior before the structural move. Add the nested lifecycle-path regression test within the structural batch because the nested path does not exist before that move.

## Planned batches

### 1. Remove the personal secret-manager assumption

**What it is**

`run.py:13` and `run.py:517` prescribe one personal secret manager even though the runner only requires environment variables.

**Smell**

An environment-specific policy leaks into the benchmark domain and its error contract.

**Evidence**

The runner reads credentials only from `os.environ`. No leaderboard module calls a secret manager.

**Change**

Describe credentials as process environment variables. Add one test that rejects a future personal secret-manager reference.

**Estimated delta**

Zero files and approximately zero lines. No dependency change.

**Inventory features**

F04, F08, and F12.

**Risk**

LOW. The missing-credential error text changes. This correction stays in its own commit.

**Verification**

Run the hosted preflight and source-text tests.

### 2. Move system integrations under one namespace

**What it is**

Forty-one tracked integration files currently share the package root with runners, validators, AWS orchestration, site code, and documentation.

**Smell**

The package root mixes the system-integration bounded context with unrelated application concerns.

**Evidence**

Connector discovery, runners, validators, builders, the site, the worker, and tests all repeat the assumption that metadata lives at `leaderboard/<system-key>/system.json`.

**Change**

- Move the 41 integration files to `leaderboard/systems/<system-key>/`.
- Keep `connector.py` as the small contract and discovery module.
- Add one `system_dir()` path function beside connector discovery.
- Import connectors from `leaderboard.systems.<system-key>.connector`.
- Update every metadata consumer to use `system_dir()`.
- Update lifecycle script relative paths so state remains in `leaderboard/.runs`.
- Add a regression test that executes a lifecycle script from the nested integration path and checks `leaderboard/.runs`.
- Keep every artifact path unchanged.
- Keep all existing CLI module names and flags unchanged.

**Estimated delta**

Forty-one file moves. Approximately 18 implementation, documentation, and test files receive small path edits. File count and dependency count stay unchanged. Net code growth should remain below 20 lines.

**Inventory features**

F01 through F14 because every runner and publisher resolves database metadata. F02 and F03 carry the direct lifecycle risk.

**Risk**

HIGH. The move crosses more than ten files and uses dynamic imports and shell-relative paths. The user must approve this batch explicitly.

**Verification**

- Discover runnable integrations only under `systems/`.
- Keep metadata-only Tiger non-runnable.
- Import every runnable connector.
- Run all lifecycle script contract tests.
- Confirm `leaderboard/.runs` remains the state directory.
- Confirm builders still find existing root artifact directories.
- Confirm the local site still contains 8 microbenchmark entries and 1 macro campaign.

### 3. Make one integration folder sufficient for the local launcher

**What it is**

`serve.py`, `run.html`, connector classes, and `system.json` duplicate system registration, display, requirements, and credential-variable knowledge.

**Smell**

The same integration knowledge has three or more owners. A contributor can add a valid connector that the local wizard cannot select.

**Evidence**

Connector discovery is dynamic, while `serve.py` and `run.html` use separate fixed maps.

**Change**

- Add public credential-variable names and a requirements description to each `system.json`.
- Parse and validate integration metadata at one shared boundary used by `site.py` and `serve.py`.
- Require each credential-variable name to match `^[A-Z_][A-Z0-9_]*$` and reject duplicates.
- Require the requirements description to be a non-empty printable string.
- Make runners and the launcher read credential-variable names from metadata.
- Generate the run-page system list from discovered integration metadata.
- Remove the fixed system maps from `serve.py` and `run.html`.
- Keep metadata-only systems out of executable choices.

**Estimated delta**

Nine metadata files and approximately eight consumers change. The fixed maps are deleted. No dependency is added. Net line count should stay flat.

**Inventory features**

F01, F02, F04, and F12.

**Risk**

MED. This extends the contribution metadata contract and changes launcher data generation. It follows the behavior-preserving move in a separate commit.

**Verification**

- Add a scratch integration and confirm discovery, server validation, and the generated run page include it without another registry edit.
- Confirm hosted credentials remain variable names only.
- Reject duplicate, malformed, and shell-injection credential-variable names before script generation.
- Confirm no secret value enters generated files.
- Run launcher and page regression tests.

### 4. Update contributor and architecture documentation

**What it is**

Contributor instructions, the scaffold generator, and architecture text name the old integration path.

**Smell**

Documentation would direct contributors to a path the runner no longer discovers.

**Evidence**

The old path appears in `README.md`, `run.html`, `architecture.html`, and source docstrings.

**Change**

Update all integration examples to `leaderboard/systems/<system-key>/`. Keep artifact examples at their current root paths. Explain that code and evidence have separate locations.

**Estimated delta**

Approximately five documentation and UI files. No meaningful line or dependency change.

**Inventory features**

F01, F02, F05, F07, F10, F12, F13, and F14.

**Risk**

LOW. Page regression tests protect visible paths and architecture labels.

**Verification**

Search for stale contribution paths. Run the page regression test and assemble the local site.

## Considered and rejected

### Moving artifacts under `leaderboard/artifacts/`

Rejected for this change. The workspace contains 635 artifact files totaling approximately 16 MB. Many are generated or untracked. Moving them changes validation, publication manifests, raw links, and user data locations without improving the integration submission boundary.

### Splitting the large AWS and runner modules

Rejected for this change. `aws_alpha.py`, `aws_worker.py`, `run_macro.py`, and `run.py` are large, but their stateful failure and cleanup behavior is extensively tested. Splitting them does not solve the database integration placement problem and would combine two high-risk changes.

### Adding domain services, repositories, factories, or a dependency injection container

Rejected. There is no persistence abstraction or alternate orchestration implementation that would pay for these layers. The existing `Connector` abstraction already has eight real implementations.

### Moving static site assets

Rejected. The existing `js/` and `workflow-diagrams/` boundaries are clear. Moving them would only churn asset paths and tests.

### Implementing production automation in this refactor

Rejected as part of the simplification commits. Production fleet launch, artifact collection, publication, deployment, and external review are new behavior. They require a separate implementation after the structural report confirms no behavior loss. A pull-request merge trigger also cannot live inside `leaderboard/` because GitHub requires workflow files under `.github/workflows/`.

## Fable review disposition

Fable approved coding with these conditions:

1. Keep artifacts at their current paths.
2. Migrate every metadata consumer in the same structural batch.
3. Correct lifecycle script roots.
4. Remove duplicate launcher registration after the move.
5. Do not claim absolute compliance with SOLID, DRY, or DDD.
6. Keep production automation separate.

All six conditions are incorporated above.

## Final verification

Run these checks after each applicable batch:

```bash
uv run --frozen python -m pytest -q leaderboard
node leaderboard/test_page.cjs
uv run --frozen python -m leaderboard.site --phase local
bash -n leaderboard/systems/*/{install,start,check,stop}
git diff --check
```

Also verify:

- No file outside `leaderboard/` changed.
- No tracked or untracked artifact moved or changed.
- No personal secret-manager reference remains under `leaderboard/`.
- Every current CLI command and flag remains available.
- The before and after feature inventories contain the same behaviors.
