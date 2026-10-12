# BranchBench leaderboard

BranchBench ranks databases that support branches on branch operations and on
point and range reads and writes, and shows how they behave under six end-to-end
agent workflows. It has one directory per system, one shared runner per lane,
dated result files, validators, a site assembler that generates one static site
per phase, and a dependency-free, responsive benchmark explorer.

## Production and development phases

The site is generated in one of two phases. The phase is website configuration
chosen when the site is assembled; nothing in a URL, hostname, query or hash can
change it, and it is never part of a measurement's provenance.

| Phase | Contents | Generated into |
| --- | --- | --- |
| `prod` (default) | Only the official records named in `leaderboard/publish.json`, their raw JSON, `index.html`, `styles.css` and the page scripts in `js/`. No mock, quick, unvalidated or unselected record, no wizard, no launcher routes, no README. | `leaderboard/.site/prod/` |
| `local` | What the builders find on this machine: the newest microbench result per system and machine (an invalid or failed newest file is listed as excluded with its reason), the mocks, every campaign that passes validation (quick ones included), the run wizard (linked from the header as **Run a benchmark**), the Terminal launcher and this README. The page itself is the production page: it renders the validated records as ordinary rows; only a mock carries a **Mock data** flag. | `leaderboard/.site/local/` |

Three distinct commands, one per job:

```bash
# 1. Local launcher: the development site with the wizard and the Terminal launcher
python -m leaderboard.serve --phase local      # assembles local, serves http://127.0.0.1:8765/, wizard at /run.html

# 2. Production preview: the production site on loopback, no wizard, no launcher routes
python -m leaderboard.serve                    # assembles prod and serves http://127.0.0.1:8765/

# 3. Static production build: the artifact to deploy (deployment itself is out of scope)
python -m leaderboard.site                     # writes leaderboard/.site/prod/
python -m leaderboard.site --phase local       # the development site without serving it
LEADERBOARD_PHASE=local python -m leaderboard.site   # the environment variable is the same switch
```

`--phase` wins over `LEADERBOARD_PHASE`; any value other than `prod` or `local`
fails with a message naming both. `python -m leaderboard.build` and
`python -m leaderboard.build_macro` still work and build the development site
through the assembler. `.site/` is ignored by git and, like `publish.json` and
the legacy generated `data.js` and `macro-data.js`, excluded from the runner's
source snapshot, so assembling or publishing never dirties a run or changes its
source digest or comparison group. Both phases run the same page and the same
ingestion (`build.build()` and `build_macro.build()`); the assembler only
selects and copies. Output goes only to `leaderboard/.site/<phase>`: it is
prepared next to it and swapped in whole, a failed build leaves the previous
output in place, and a symlinked page, record, manifest, suite file or output
directory is refused.

### Publishing official records

`leaderboard/publish.json` names exactly which records production carries. It is
empty by default, so a fresh production site says **No published results yet**.

```json
{
  "micro": ["dolt_mysql/results/20261020/ec2-c6i-4xlarge.json"],
  "macro": ["dolt_mysql/macro/20261020/campaign_0a1b2c3d.json"]
}
```

Entries are relative `<system>/results/<YYYYMMDD>/<machine>.json` and
`<system>/macro/<YYYYMMDD>/campaign_<id>.json` paths; anything else, a duplicate,
a path that leaves the board, a symlink or a missing file fails the build.

**Selection versus validation and provenance.** The manifest decides only
what production shows. It changes nothing about a record: validation,
outcomes, exclusion reasons, source digests and comparison groups are computed
by the same builders in both phases, and the manifest is not part of any run's
source snapshot.

- Every named micro file must validate, or the build fails. Among the selected
  official records of one system and machine, the newest determines what is
  shown for that system and machine, so an unpublished newer laptop run never
  supersedes an official record, while a newer selected error record supersedes
  an older selected success (the error stays visible as a run without a result;
  the older success is omitted, not an error). No mock stands in for an
  unpublished system.
- Macro entries must be Full campaign records that pass the builder's
  validation (see **Agent workflows**); a quick campaign or one that fails
  validation is refused with its reason. Only the selected records are campaign
  heads, so an unselected record of the same campaign at another date cannot
  block or replace them, and a campaign selected twice fails the build. Every
  attempt file is still ingested, so a duplicate attempt still fails validation.
  A published campaign keeps its failed, incomplete and timed-out workflows
  exactly as recorded: those are results, not validation failures.
- Each system folder's `system.json` must be a regular file; a symlinked sidecar
  or record is refused before any validator reads it.

Local laptop runs are not published by relabeling; the manifest names real records.

## Status after the git-like API merge

The leaderboard's `local-4` suite uses the new shared API, but remains the
eight-row **microbenchmark** below. The six macrobench scenarios run in a separate,
unranked **Agent workflows** lane (see the last section). Only **Doltgres (`dolt`)
and Dolt (`dolt_mysql`)** currently have leaderboard connectors. Neon, Xata and Tiger Cloud are mock previews, not
runnable leaderboard systems. Main also has SeekDB and MatrixOne adapters, but
neither has a leaderboard connector yet; main removed its Tiger adapter.

`local-4` records branch creation from `BRANCH`, branch switches from `CONNECT`,
and data latency from individual statement rows. It never adds an enclosing
`EXEC` timing to its statements. A failed or unsupported operation makes the
attempt null, including an INSERT that succeeds only after a failed retry.
The API, timing boundaries and dependency lock changed, so earlier results are
preserved as history and excluded from current rankings. Full reruns are needed.

The leaderboard is one card. Its tabs switch between **Macrobench** (the agent
workflows, first) and **Microbench**; arrow keys, Home and End move between them.
Both boards are plain data tables: a sortable column shows a sort icon in its
header, and the whole header sorts, fastest first; sorting the sorted column
again lists it slowest first, with rows that have no value always last. Header
labels stay within two lines. The last column holds the row's
**Details** button, and a cell has a background only as heatmap shading where at
least two values compare. On the microbench, a toolbar above the table holds
**First try / Best repeat** and one chip per filter (hosting, systems, machine,
capabilities) that opens a checklist; a dimension with a single value has no
chip. The microbench table is one per hosting group, with rank, system and the
three category scores (first query, reads & writes, load), each with its elapsed
time where it has one; a score is shaded and its best marked only where at least
two systems have one. Rank by one score at a time from its column header; there
is no overall score. When the reads & writes score counts fewer than all of its
workloads, its header says so, for example **Reads & writes (1 of 5)**. The view
is part of the URL, so a link reopens the same table:
`#microbench?try=best&sort=data&order=desc&workloads=insert,update&systems=Neon`
holds the try, the sorted score, its order, the counted workloads and every
narrowed filter (`hosting`, `systems`, `machines`, `capabilities`); unknown
values fall back to the defaults. A row from a mock file is ranked like any other and
carries a **Mock data** flag; production never contains one.

Below the card, the workload heatmap shows every batch time: darker mint means
closer to the fastest time within that row, and a check marks the fastest; a row
with only one time has neither. Tick
read/write workloads there to choose what the reads & writes score counts; at
least one stays ticked, so the score never silently counts all of them. A
trade-off plot beside it compares the first-query and reads & writes scores.
**The operations** defines every operation the way the workflows are defined: a
picker, a branch-history diagram in the same drawing language (beads are
commits, open circles are new branch heads, diamonds are reads, squares are writes),
what is timed, and each system's batch time. A workload's name in the heatmap
links to that definition (`#dataset?operation=read`). The dataset table counts,
checksum and seed, and the scoring rules, are collapsed below it; the bare
`#dataset` link from the run page opens them.

A row's **Details** button, in its last column, opens its details dialog;
nothing else in a row is clickable. It starts with **Why this position**: the rank, then every workload's
time against the fastest in its group, so the score's arithmetic is visible.
Every try, the environment and provenance follow; the raw JSON is linked from
the dialog.

In the local phase, **Run a benchmark** opens `run.html`: a single-page setup with suite choice
(Microbench or Agent workflows), database selection, environment fields, and a
live script preview. Select one or more databases;
changing any setting updates the script and invalidates the previous launch
preview. Expand **Tools & credentials** for prerequisites. The shell-quoted
commands run the selected databases sequentially and rebuild the development
site once. The build validates results and excludes historical suites and
errors. The sequence stops if a command fails. A workflow diagram explains setup,
measurement, cleanup, and comparison. Detailed setup instructions remain in the
expandable help.

For **Run in Terminal** on macOS, start `python -m leaderboard.serve --phase local`
from the repository root and open `http://127.0.0.1:8765/run.html`. Review the
full script, then click **Run in
Terminal** to execute that exact script in a new Terminal session. It uses the
project's `.venv/bin/python` when present, otherwise the launcher's Python.
Missing hosted-service credentials are prompted for in Terminal with hidden
input; the browser never receives them. A failed command stops the sequence.
Source changes must be committed before benchmarking, as with the CLI.

The launcher binds only to loopback, accepts same-origin JSON requests, and
reconstructs scripts from validated database choices rather than accepting
arbitrary shell commands. Static `file://` previews of `.site/local/index.html`
still offer copyable commands and show how to start the launcher. The production
server has no launcher routes at all. Nothing is published by the wizard.

The two runnable connectors start local servers on one machine. The result
format also supports hosted services once their connectors are implemented.

## Official deployment direction (not implemented)

Official rankings should come from controlled benchmark workers, not arbitrary
contributor machines. Contributors would request a run or submit a database
configuration. Workers would pin the machine specification, OS image, resource
limits, benchmark revision, dataset, and seed. Managed services also need fixed
client regions and provider-specific service configurations. Keep workers separate
from the website/API host so web traffic does not affect measurements.

The intended pipeline is: run request, standardized worker, result JSON,
server-side validation, an entry in `publish.json`, `python -m leaderboard.site`,
deployment of `leaderboard/.site/prod/` to a static host, browser display. The
validators check file consistency and the manifest selects records; neither
attests that a run came from an approved environment. Submission, job
scheduling, trusted-run provenance and deployment are future work. The local
launcher is a development tool; its results reach production only through an
explicit manifest entry, and a quick or unvalidated campaign never can.

## Quick start

Prerequisites: `uv`, Python 3.13, `protoc`, `dolt` and `doltgres` on `PATH`, and
`psql` from libpq. Use main's committed dependency lock, not a separate
leaderboard environment.

```bash
# Once, from the repository root
uv sync --frozen
uv run --frozen python build_protos.py
source .venv/bin/activate
export PATH="$(brew --prefix libpq)/bin:$PATH"   # libpq is keg-only

# Each run: commit first, because the runner refuses uncommitted changes
python -m leaderboard.run dolt --machine apple-m5-32gb
python -m leaderboard.run dolt_mysql --machine apple-m5-32gb
python -m leaderboard.site --phase local
open leaderboard/.site/local/index.html
```

A local run starts the server, creates a database named `bb_<run id>`, loads
`db_setup/ch_benchmark_seed.sql`, measures, deletes the database, and stops the
server. A full run takes about 1 to 2 minutes.

For a short trial, add `--max-batch 5 --allow-dirty --out /tmp/lb`. The validator
rejects such results, so they never reach the page.

## Hosted connectors (not implemented)

The current hosted entries are mock previews only. Adding a hosted system
requires a connector with credential preflight, provisioning, loading and scoped
cleanup; a shared adapter alone does not make it runnable here. Tiger also needs
an upstream adapter port to the new API before a leaderboard connector can use it.

The runner already checks a connector's required environment variables and
preflight before creating resources, requires `--client-location`, and records
region, requested service settings and a measured round-trip baseline. Do not
put credentials in the browser or result files.

If a run is killed, `leaderboard/.runs/<system>.journal.json` keeps the name and
target scope privately, without API keys. The next run deletes those resources
before starting only if its target matches. A changed target or an older journal
without a scope stops the run and preserves the journal for manual recovery.
Restore the original account/project or local data directory before retrying;
do not discard the journal until its resources have been removed.

`--deadline-min` bounds startup, recovery, provisioning, loading, and measurement
(default 120 minutes). Cleanup then gets its own 20-minute limit, and server stop
gets 60 seconds. A cleanup failure retains the journal for a later retry.

## Page code

The page is static: `index.html`, `styles.css`, the two data files the build
writes (`data.js`, `macro-data.js`) and six plain scripts in `js/`. Every script
loads at the end of the body, so none blocks the first paint, in this order after
the data files:

1. `dom.js`: DOM building, number formats, design tokens, and the table and control helpers every board shares.
2. `details.js`: the details dialog.
3. `scenes.js`: the Canvas branch-history scenes behind the workflow and operation definitions.
4. `micro.js`: the microbench board, its breakdowns and the operation definitions.
5. `macro.js`: the agent-workflow board, its details and the workflow definitions.
6. `app.js`: page state, URL routing and start-up.

They are classic scripts that share one global scope, not ES modules, so a
`file://` preview still runs. `site.SCRIPTS` lists them, the site build copies
exactly that list, and a test checks that it matches the page's script tags.

Every color is a token, defined once in `:root` in `styles.css`. Rules, markup
and scripts refer to tokens with `var(--name)`; the Canvas scenes read token
values through `getComputedStyle`. `node leaderboard/test_page.cjs` fails on a
color literal anywhere else and names the file, line, selector and nearest
token. The favicon and `<meta name="theme-color">` are the only literals,
because browser chrome cannot read CSS, and `theme-color` must equal `--canvas`.

## Mock data

Until Neon, Tiger Cloud, and Xata have real runs, the page shows mock entries for
them, from `leaderboard/mock/<system>.json`. These are illustrative numbers, not
measurements. Branch create and connect follow earlier hosted measurements in
`run_stats_final`, and data operations assume about 20 ms per statement from a
client in US East. Each file's `basis` says so.

- The build adds a mock only for a system with no result file. So a system's first
  real result, or even its first error record, replaces its mock.
- The local page ranks and charts a mock like any other row and flags it **Mock data**;
  production never carries one, so it never meets a published result there.
- Mock files sit outside `results/`, and the validator rejects a result file that
  carries a `mock` key, so a mock cannot be published as a measurement.
- To remove them all, delete `leaderboard/mock/` and run the build again.

## What is measured

The fixed `db_setup/ch_benchmark_seed.sql` loads **21,121 initial rows across 12
tables** using a CH-benCHmark-style retail schema. Only `item`, with **1,000
initial rows**, is targeted by timed reads and writes; all tables are loaded and
included in branches. This is a small branching and CRUD microbenchmark, **not
the full CH-benCHmark, TPC-C, or TPC-H workload suite**. The dataset SHA-256 pins
the SQL file. The workload seed fixes which keys each try targets, not generated values.

The suite in `suite.json` runs 8 rows, 3 tries each, on the `item` table:

| row | one operation | batch |
|---|---|---|
| Time to first query | create a child of the chain's tip, connect, read one row, timed as one span | 1 |
| BRANCH_CREATE | create a child of the tip | 1 |
| BRANCH_CONNECT | connect to the next branch of a chain of 4 | 100 |
| READ | read one row by primary key | 1000 |
| RANGE_READ | read 100 consecutive keys | 200 |
| INSERT | insert one generated row | 1000 |
| UPDATE | update one row by key | 1000 |
| RANGE_UPDATE | update 100 consecutive keys | 200 |

- **Cell:** the sum of the relevant `BRANCH`, `CONNECT`, or SQL-statement rows in
  one batch, in seconds. Enclosing `EXEC` rows are not added again. It is null
  unless every call in the batch succeeded and no operation was unsupported.
- **First try:** try 1. **Best repeat:** the lower of tries 2 and 3, and it needs both.
- **Branches:** every row starts from the tip of a chain of 4 branches. Each data
  row runs on its own new branch. After every run, the runner checks that the tip
  still has the dump's row counts.
- **Determinism:** each row and try has its own seeded random stream. It fixes the
  keys that reads, updates, and ranges target, provided the server lists keys in
  the same order every time, and a failed try cannot change later tries' keys.
  Generated values come from main's unseeded row generator, so they differ between
  runs. INSERT keys are random too: a collision is retried, up to 5 attempts in
  all. A failed attempt is recorded and makes that try null, even if a retry
  succeeds; the leaderboard does not rank a successful retry as a clean batch.
- **Load:** the time to load the dump. Provisioning is recorded but not ranked.
- **Hosted results** include the network round trip from the client. Each one
  records its region, where the client ran, and a round-trip baseline: the median
  of 20 `SELECT 1` calls.

## Ranking

- The page ranks self-hosted and managed-service systems in separate tables.
- For each ranked row: (value + 10 ms) / (best value in the same table + 10 ms).
  The score is the geometric mean of these ratios, as on ClickBench.
- The branch view ranks time to first query only. Create and connect are its
  parts, so they are shown but not ranked. The data view ranks READ through
  RANGE_UPDATE, and viewers can untick rows.
- A row that no entry in the table has a value for is left out of the score, and
  the page says so. An entry missing a ranked row is listed below the ranking,
  unranked, with the rows it lacks.
- Only the newest file per system and machine counts. If it is an error file,
  that system and machine are excluded, and no older result stands in.

## Result files

A result lives at `<system>/results/<YYYYMMDD>/<machine>.json`, dated in UTC. It
holds the system's identity from `system.json`, the server version, the suite
name and digest, the seed, the git commit, the dataset and lock digests, the
provisioning and load times, and `result`: 8 rows of 3 cells. `lock_sha256`
identifies the repository-root `uv.lock`. Hosted results add
`region`, `rtt_ms`, `client_location`, and `service`, an allowlist of the settings
the adapter requested.

A failed run writes `{"error": "..."}` in the same place. The runner masks the
password of any connection URI in an error record or message.

`python -m leaderboard.validate` rejects a file when:

- its path, date directory, or machine name is malformed;
- a key is missing, unexpected, or of the wrong type, including booleans used as numbers;
- it came from a dirty tree or a capped (`--max-batch`) run;
- its suite, suite digest, seed, or dataset digest differs from `suite.json`;
- its identity differs from `system.json`;
- any key or string looks like a credential.

`results/` is tracked: db-fork is public, so pushing a result file publishes it;
the generated `data.js` lives only under the ignored `.site/`. Existing `local-1` and `local-2` measurements are retained as historical
files, but the build excludes them from `local-4`. New measurements require a
committed tree and a full rerun. To validate a new result directly, pass its path
to `python -m leaderboard.validate`; calling it without paths deliberately checks
all files, so retained historical suites fail the current-suite check.

## Rules

- Systems run with their default configuration. A tuned system says `"tuned": "yes"`
  in `system.json`, and its tuning is described in this file.
- No result caching between tries or runs.
- The runner queries the server version and records it.
- Results come only from a committed tree. Changing `suite.json` changes its
  digest, so results from different suites are never compared. Bump the suite
  name whenever workload semantics change. The runner snapshots source files,
  dataset, and Git HEAD before starting and rejects results if they change during
  the run, even when `--allow-dirty` is used for testing.

## Adding a system

1. **Adapter.** The leaderboard uses main's shared code as it is. The backend must
   already exist in main's `microbench/task2.proto`, and main's
   `create_db_tools()` in `microbench/runner2.py` must build its adapter.
   An adapter main lacks, or a fix one needs, goes to main first.
2. **Identity.** Create `leaderboard/<system>/system.json` with `system`,
   `proprietary`, `hosted`, `tuned`, and `tags`.
3. **Local server scripts.** Add executable `start`, `check`, and `stop` scripts, and
   optionally `install`, as in `leaderboard/dolt/`. `start` must send the server's
   output to a log file, and `stop` must stop only the process `start` launched.
4. **Connector.** Add `leaderboard/<system>/connector.py` defining `CONNECTOR`, a
   `Connector` from `leaderboard/connector.py` with `backend`, `needs_psql`,
   `version_sql`, `scope`, `provision`, `load`, and `delete`. The runner finds it
   by its folder. Also expose the runnable system in `run.html` and the
   launcher's credential mapping in `serve.py` when adding wizard support.
   Everything it creates must be named after the run's database, so `delete` can
   find it by name. `scope` must identify the deletion target without secrets;
   cleanup refuses a journal from a different scope. A hosted connector also sets `keys`, `region`, `service`, and
   `preflight`, and every `service` key must be in the validator's allowlist.
5. **Trial.** Run it with `--max-batch 5 --allow-dirty --out /tmp/lb`, and check that
   every cell is filled and nothing is left behind.
6. **Result.** Commit, run it in full, then validate and build.

Tests: `python -m unittest discover -s leaderboard -p 'test_*.py'` and
`node leaderboard/test_page.cjs`.

## Agent workflows

A second, separate lane runs main's six macrobench scenarios on a local Doltgres
or Dolt server and shows the outcome of every attempt on the page under **Agent
workflows**. It shares nothing with `local-4`: its own runner, result contract,
validator, build step and `macro-data.js`. Production opens on this lane; the
microbench stays one switch away. The micro validator/build never read these files.

The page names each workflow by the job it simulates and keeps the upstream
scenario name and key as secondary information:

| Page name | Upstream scenario | Key |
| --- | --- | --- |
| Evaluate agent rollouts | S1 RL environment | `rl_env` |
| Manage context history | S2 Context management | `context_mgmt` |
| Coordinate multiple agents | S3 Multi-agent | `multi_agent` |
| Develop against live data | S4 Dev agent | `dev_agent` |
| Recover from a bad deployment | S5 Ops agent | `ops_agent` |
| Ingest and merge data | S6 Data agent | `data_agent` |

The S6 description on the page says what the scenario does: with a configured
probability a step is treated as failed and the batch resets to an earlier
commit, or continues on a new branch from that commit when the backend has no
reset, then redoes the work.

The workflow board has a row per system and a column per workflow, one table
per comparison group, headed on one line by its machine and how many systems it
compares. A completed workflow shows its completion time; any other outcome
shows a drawn status icon and its word. Within the group, systems are ranked by
one workflow's completion time at a time, chosen from the column headers (also
part of the URL: `#agent-workflows?workflow=ops_agent`, with `&order=desc` for
slowest first). Tied times share a rank.
A column with at least two completion times marks its fastest and is shaded
relative to it; a column with one time has no mark and no shading, because
nothing compares. A workflow that did not complete is unranked in that column
only. The last column holds how many workflows completed and the row's
**Details** button. There is no overall score or weighted average. Rows carry no
badges: every campaign on the board passed validation (below), and a campaign
that did not is listed under **Runs not on the board** with its reasons. Narrow
screens scroll the workflow columns past a pinned system column.

**The workflows**, directly below the board, defines each workflow
(`#workflows?workflow=ops_agent`): what it simulates, an animated branch history
drawn on a Canvas (played once when seen, then held; illustrative, not a trace;
each label is placed clear of every line, commit, mark and ref chip, and a label
with no clear spot is left out, and the caption and **Read the history as text**
carry every step; a ref chip that would cross a line slides along its lane or
moves to its other side),
the branch and history operations it exercises, and the completion times of the
comparable systems for that workflow.

**Completion time** is the upstream runner's `elapsed_sec`: its clock starts
after the schema and seed data are in place (reported separately as the setup
time) and stops once the last workflow step has finished and the background
production traffic some workflows run (the spine workers) has been stopped. It
excludes schema and seed setup and the parent's later database deletion and
server stop. It is shown only for a verified attempt.

Each row has one control, **Details**, which opens one dialog with everything
about that run: its rank in the chosen workflow, then each workflow with its
time against the fastest in the group (or its status and first reason). Each
workflow expands in place to its evidence: status, completion and setup time,
work units and correctness checks as proportion bars, then the checks, the
operation tables and the attempt record behind disclosures. Older runs of the
same system and machine can be chosen there (`#agent-workflows?campaign=<id>`),
and provenance closes the dialog. Malformed evidence is named and shown as
recorded, never drawn as a proportion.

```bash
# Validation comes first: a run starts only from a committed tree and always covers all six workflows.
export PATH="$(brew --prefix libpq)/bin:$PATH"
# Quick: the upstream *_mini workloads, minutes per database, shown on this machine only.
python -m leaderboard.run_macro dolt       --preset mini --machine apple-m5-32gb
python -m leaderboard.run_macro dolt_mysql --preset mini --machine apple-m5-32gb
python -m leaderboard.site --phase local
open "leaderboard/.site/local/index.html#agent-workflows"

# Full: the paper-size workloads; hours per database. Only Full campaigns can be published.
python -m leaderboard.run_macro dolt_mysql --preset full --machine apple-m5-32gb
```

`run.html` offers the same choices (suite, local databases, Quick or Full,
machine) and the launcher reconstructs the script from those validated enums. The
generated script rebuilds the development site even when a run fails and exits
with the first failing run's status. **Run in Terminal** means the launch was
accepted, nothing more.

### How a run works

One `run_macro` invocation is a campaign: one system, one preset, the selected
scenario set, one attempt per scenario, sequentially. The campaign:

1. refuses to start if a server already answers on the connector's host and port
   or if `leaderboard/.runs/<system>.pid` names a live process (it only benchmarks
   servers it starts itself), and takes the per-system lock;
2. writes its campaign record `<system>/macro/<YYYYMMDD>/campaign_<id>.json`
   before the server starts, then starts the server. Ownership is the pid file the
   start script writes: a server that started but never passed its readiness check
   is still stopped at the end; one that never started is left alone. The record
   is rewritten after every attempt and at the end with the server-stop verdict
   (`stopped`, `failed`, `not_started`), the finish time and any abort reason, so
   a campaign that failed before its first child, lost its server or was
   interrupted stays visible on the page instead of letting an older success stand.

For every attempt the parent:

1. deletes what an earlier run's journal names and proves that database is
   absent, keeping the journal otherwise, then writes a private journal
   (`leaderboard/.runs/<system>.journal.json`, mode 0600) with the attempt's
   `bb_<8 hex>` database name before anything is created;
3. writes an effective config from the frozen upstream preset with that unique
   run id and database name, `cleanup: false` and generated data, and runs
   `python -m macrobench.runner` as a child with `--max-runtime-sec` equal to the
   deadline (`--deadline-min`, default 30 for mini and 360 for full, setup
   included); the parent kills the child 120 s past that cap;
4. reads the child's e2e stats, validates their shape and classifies the outcome
   from that evidence and from how the child ended (`exited`, `killed` at the
   deadline, `interrupted`, `launch_failed`), never from the exit code; a child
   the parent had to kill is never verified, even if it wrote a result first;
5. deletes the database through the connector, checks that it is gone, and only
   then deletes the journal; the cleanup verdict is recorded separately. A delete
   that fails, a database that is still there, or an absence check that could not
   run keeps the journal and stops the campaign. Cleanup and publication run on
   every path once the journal exists: a child that could not be started, an
   unreadable result and Ctrl-C all publish a non-verified attempt first;
6. publishes `<system>/macro/<YYYYMMDD>/<attempt id>.json` atomically and never
   rewrites it: the campaign id, selected scenarios, outcome, cleanup, how the
   child ended, the scrubbed allowlisted evidence, the Git commit and dirty state,
   a digest of every source file, the frozen preset's digest, the complete
   normalized workload (only run id, backend selector and database name cleared)
   and its digest, the lock digest, the backend version, the Python and platform
   versions, and the machine's CPU, core count and memory (null when unknown).

The server is stopped at the end of the campaign. Raw logs, Parquet, the effective
config and the child's full e2e JSON stay private under `leaderboard/.runs/macro/`.

The upstream runner now refuses to start timed work when a schema statement or the
seed commit failed (`status: setup_failed`), counts every logical work unit with an
explicit outcome (succeeded, failed, skipped), records every exception that escaped
a worker, splits FAILED verb/exec rows into expected (background spine traffic)
and unexpected, counts commit-ref and branch-head fallbacks, and exits non-zero
unless the scenario completed. A failed setup starts neither the workflow nor the
background spine load. S1 gives each rollout leaf at most two branch attempts and
records the shortfall instead of retrying until the runtime cap.

### Outcome states

| State | Meaning |
| --- | --- |
| Verified completed | Setup complete, every expected unit succeeded, no worker exception, no unexpected failed operation, every applicable correctness check passed, nothing unsupported, no history-ref fallback. Only this state shows a completion time. |
| Incomplete | Completed, but units were skipped, a verb was unsupported, a history reference fell back to a branch head, a check was not applicable or checks were disabled. |
| Failed | Setup failed, a worker raised, units failed, an operation failed unexpectedly, a correctness check failed, or the child crashed. |
| Timed out | The runtime cap ended the workflow, or the parent killed the child at the deadline, with or without a result. Partial elapsed time is shown only in the details. |

These are the only states on the board. An attempt whose evidence is
missing or unreadable, a workflow the campaign never reached and a scenario
with two attempts make the campaign fail validation instead.

Database cleanup is `verified`, `failed` or `unknown` per attempt and the server
stop is `stopped`, `failed` or `not_started` per campaign; neither changes a
workflow outcome, but anything other than a verified cleanup and a stopped
server fails validation.

The validator recomputes every outcome and its timings from the stored evidence
and child record and rejects any file that disagrees, that carries a malformed
nested shape (lists where objects belong, booleans or negative numbers as
counters, totals that do not match their results, non-finite numbers), whose
workload digest is not the digest of its effective config, or whose base digest
is not the frozen preset's. The builder rejects files, never crashes on them. A
campaign whose attempts disagree with its record (version, source, lock, Python,
platform, hardware, preset, scenario set) or that holds two attempts of one
scenario is listed as inconsistent rather than resolved by picking one; dirty and
changed-during-run flags are true if any attempt says so.

**Validation.** Before a run starts, the runner checks that the sources are
committed, the system is local and runnable, the tools are on PATH and nothing
else answers on its port; if any check fails it writes nothing and runs
nothing. There is no flag to bypass this, and a campaign always runs all six
workflows. After the run, the builder validates the recorded campaign: sources
unchanged during the run, a finished run that was not aborted, a stopped
server, every database cleaned up, every named attempt present and readable,
and no conflicting attempt or provenance. A campaign that passes is a result; a
campaign that fails is excluded with every reason, never shown with a caveat.

Systems compare only within one comparison group. The group binds the preset,
the per-scenario workload digests, the source digest, the lock digest, the
Python and platform versions, the machine label and the recorded CPU, core count
and memory; the system under test and its version stay per row so systems
compare across rows. The page shows one table per comparison group and never
presents mixed groups as one comparison. The builder lists campaigns newest
first; older campaigns of a system can be chosen from its details.


### Current local status

Two quick campaigns on 2026-10-11 from commit `77ffb8f` on an Apple M5 (10 CPUs,
32 GiB, machine label `fable-mini`) passed validation and share one comparison
group: Dolt 1.81.2 (campaign `3d6a3943`) verified all six workflows, and Doltgres
0.54.10 (campaign `e7966bf3`) verified S3 and S5. Doltgres failed S1, S2, S4 and
S6: S1 because Doltgres rejects TPC-C consistency queries (`GMSCast expected type
float64, got decimal.Decimal`), and S2, S4 and S6 because `dolt_rebase` aborted
with `rebase aborted due to changes in branch ...` even though the adapter
commits first. These are backend or adapter limitations surfaced by the evidence
and are shown as **Failed** with their reasons. Every database was cleaned up and
both servers stopped. Two earlier quick campaigns from 2026-10-10 ran from
uncommitted sources and are listed under **Runs not on the board**. No Full
campaign has been run yet, and `publish.json` is empty, so the production site
shows no agent-workflow results.

Tests: `python -m unittest discover -s leaderboard -p 'test_*.py'` (including
`test_site.py` for the phases, the manifest and the generated directories),
`python -m pytest tests/test_db_api.py tests/test_macrobench.py tests/test_scenarios.py`
and `node leaderboard/test_page.cjs`. Fake-backend tests cover setup failure (and
the spine-load gate), worker exceptions, bounded S1, history fallbacks, the
classifier including malformed nested shapes and killed children, the result and
campaign-record contracts, grouping, inconsistent and conflicted campaigns,
missing or invalid named attempts, and the parent lifecycle with a fake child (no
result, killed, malformed, foreign, launch failure, unreadable result, interrupt,
delete failure, surviving database, absence check failure, prior-journal
recovery, server that never became ready, server that never started, version
failure, stop failure, dirty and changed sources, occupied port, live pid file).
A passing test is not a live run; the status above is.
