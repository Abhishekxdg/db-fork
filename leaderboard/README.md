# BranchBench leaderboard

BranchBench ranks databases that support branches on branch operations and on
point and range reads and writes. It has one directory per system, one shared
runner, dated result files, a validator, a build step that writes `data.js`, and
a dependency-free, responsive benchmark explorer.

The page shows first-query, read/write, and load rankings in tables with explicit
rank, system, score, and time columns. The workload heatmap shows measured batch
timings: darker mint means closer to the fastest time within that row, and a check
marks the fastest. **First try / Best repeat** updates all workloads; load and
provisioning remain one-time measurements. Expand **Filters** or **Scoring** for
details. Mock previews stay separate and never affect rankings or heatmap scales.

The **Dataset & workloads** summary identifies the shared CH-benCHmark seed.
Expand it for initial table counts, the timed table, every workload's batch size
and explanation, and the dataset path, checksum, suite, and workload seed. The run
page shows the same dataset scope and links to this guide.

Two charts complement each measured ranking: a scatter plot compares first-query
and selected read/write scores, and an attempt chart shows all three timings for
a chosen workload. Chart points open the full result. Charts follow the current
filters, keep hosting groups separate, and never include mocks. Missing attempts
leave gaps rather than being plotted as zero. The attempt chart always shows all
tries, independently of the First try / Best repeat scoring toggle.

Hover a workload label or focus/tap its **?** for an animated explanation. These
diagrams illustrate the operation, not measured speed; reduced-motion preferences
disable animation. Select a ranked row or system name for a result dialog with
every attempt, setup timings, environment, and provenance. JSON remains available
from the dialog. Machine details live here rather than in the ranking heading.

**Run a benchmark** opens `run.html`: a single-page setup with database selection,
environment fields, and a live script preview. Select one or more databases;
changing any setting updates the script and invalidates the previous launch
preview. Expand **Tools & credentials** for prerequisites. The shell-quoted
commands run the selected databases sequentially, validate once, and build
once. The sequence stops if a command fails. A workflow diagram explains setup,
measurement, cleanup, and comparison. Detailed setup instructions remain in the
expandable help.

For **Run in Terminal** on macOS, start `python -m leaderboard.serve` from the
repository root. The local launcher opens the wizard at
`http://127.0.0.1:8765/run.html`. Review the full script, then click **Run in
Terminal** to execute that exact script in a new Terminal session. It uses the
project's `.venv/bin/python` when present, otherwise the launcher's Python.
Missing hosted-service credentials are prompted for in Terminal with hidden
input; the browser never receives them. A failed command stops the sequence.
Source changes must be committed before benchmarking, as with the CLI.

The launcher binds only to loopback, accepts same-origin JSON requests, and
reconstructs scripts from validated database choices rather than accepting
arbitrary shell commands. Static `file://` previews still offer copyable commands
and show how to start the launcher. Nothing is published remotely by the wizard.

Everything runs on one machine. The target databases are either local servers
(Doltgres, Dolt) or hosted services (Neon, Tiger Cloud, Xata).

## Official deployment direction (not implemented)

Official rankings should come from controlled benchmark workers, not arbitrary
contributor machines. Contributors would request a run or submit a database
configuration. Workers would pin the machine specification, OS image, resource
limits, benchmark revision, dataset, and seed. Managed services also need fixed
client regions and provider-specific service configurations. Keep workers separate
from the website/API host so web traffic does not affect measurements.

The intended pipeline is: run request → standardized worker → result JSON →
server-side validation → build `data.js` → deploy static files → browser display.
The current validator checks file consistency; it does not attest that a run came
from an approved environment. Submission, job scheduling, trusted-run provenance,
and deployment are future work. The local launcher remains a development tool;
local results must not be mixed into future official rankings.

## Quick start

Prerequisites: Python 3.13, `dolt` and `doltgres` on `PATH`, and `psql` from libpq.

```bash
# Once, from the repository root
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock && pip install -e . --no-deps
python build_protos.py
export PATH="$(brew --prefix libpq)/bin:$PATH"   # libpq is keg-only

# Each run: commit first, because the runner refuses uncommitted changes
python -m leaderboard.run dolt --machine apple-m5-32gb
python -m leaderboard.run dolt_mysql --machine apple-m5-32gb
python -m leaderboard.validate
python -m leaderboard.build
open leaderboard/index.html
```

A local run starts the server, creates a database named `bb_<run id>`, loads
`db_setup/ch_benchmark_seed.sql`, measures, deletes the database, and stops the
server. A full run takes about 1 to 2 minutes.

For a short trial, add `--max-batch 5 --allow-dirty --out /tmp/lb`. The validator
rejects such results, so they never reach the page.

## Hosted systems

Store each key in Bitwarden as an item named after its variable, and load it
when launching the run. No key is ever written to a file.

| system | variables |
|---|---|
| `neon` | `NEON_API_KEY_ORG` |
| `tiger` | `TIGER_ACCESS_KEY`, `TIGER_SECRET_KEY`, `TIGER_PROJECT_ID` |
| `xata` | `XATA_API_KEY`, `XATA_ORGANIZATION_ID` |

```bash
eval "$(bw-load NEON_API_KEY_ORG='NEON_API_KEY_ORG')"
python -m leaderboard.run neon --machine apple-m5-32gb --client-location "US East"
```

Before creating anything, the runner checks that every variable is set and that
the keys can reach the account. A failed check publishes nothing. Every hosted
statement times out after 120 s.

What a run creates, all named after `bb_<run id>` and all deleted at the end:

- Neon and Xata: one project per run.
- Tiger: one root service, plus one forked service per branch, named
  `bb_<run id>_<branch>`, inside the project in `TIGER_PROJECT_ID`.

At most 5 branches or forks are alive at once, besides the root. A run creates
16 branches. Hosted deletions may continue asynchronously after the API accepts
them, so this is not a vendor quota guarantee. Tiger and Xata averaged about 55 s per branch in earlier measurements,
so their runs should take 15 to 25 minutes.

If a run is killed, `leaderboard/.runs/<system>.journal.json` keeps the name and
target scope privately, without API keys. The next run deletes those resources
before starting only if its target matches. A changed target or an older journal
without a scope stops the run and preserves the journal for manual recovery.
Restore the original account/project or local data directory before retrying;
do not discard the journal until its resources have been removed.

`--deadline-min` bounds startup, recovery, provisioning, loading, and measurement
(default 120 minutes). Cleanup then gets its own 20-minute limit, and server stop
gets 60 seconds. A cleanup failure retains the journal for a later retry.

## Mock data

Until Neon, Tiger Cloud, and Xata have real runs, the page shows mock entries for
them, from `leaderboard/mock/<system>.json`. These are illustrative numbers, not
measurements. Branch create and connect follow earlier hosted measurements in
`run_stats_final`, and data operations assume about 20 ms per statement from a
client in US East. Each file's `basis` says so.

- The build adds a mock only for a system with no result file. So a system's first
  real result, or even its first error record, replaces its mock.
- The page shows mocks in a separate, unranked preview. They never affect measured
  scores or ratio baselines. The Data filter hides them.
- Mock files sit outside `results/`, and the validator rejects a result file that
  carries a `mock` key, so a mock cannot be published as a measurement.
- To remove them all, delete `leaderboard/mock/` and run the build again.

## What is measured

The fixed `db_setup/ch_benchmark_seed.sql` loads **21,121 initial rows across 12
tables** using a CH-benCHmark-style retail schema. Only `item`, with **1,000
initial rows**, is targeted by timed reads and writes; all tables are loaded and
included in branches. This is a small branching and CRUD microbenchmark, **not
the full CH-benCHmark, TPC-C, or TPC-H workload suite**. The dataset SHA-256 pins
the SQL file; the workload seed fixes generated operation inputs separately.

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

- **Cell:** the sum of the timed database calls in one batch, in seconds. It is
  null unless every call in the batch succeeded.
- **First try:** try 1. **Best repeat:** the lower of tries 2 and 3, and it needs both.
- **Branches:** every row starts from the tip of a chain of 4 branches. Each data
  row runs on its own new branch. After every run, the runner checks that the tip
  still has the dump's row counts.
- **Determinism:** each row and try has its own seeded random stream, including
  generated values. A failed try cannot change later tries' statements or inputs.
  INSERT reserves distinct integer keys above the loaded maximum for all tries;
  unexpected collisions fail the try instead of being retried outside its timing.
- **Load:** the time to load the dump. Provisioning is recorded but not ranked.
- **Hosted results** include the network round trip from the client. Each one
  records its region, where the client ran, and a round-trip baseline: the median
  of 20 `SELECT 1` calls.

## Ranking

- The page ranks local and hosted systems in separate tables.
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
provisioning and load times, and `result`: 8 rows of 3 cells. Hosted results add
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

`results/` and `data.js` are tracked: db-fork is public, so pushing them publishes
them. Existing `local-1` measurements are retained as historical files, but the
build excludes them from `local-2`, which changes the workload's random streams
and INSERT keys. New measurements require a committed tree and a full rerun.

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

1. **Adapter.** Subclass `DBToolSuite` in `dblib/` with branch create, connect,
   current branch, list, and delete. `dblib/dolt.py` is the smallest example. Add
   the backend to the `Backend` enum in `microbench/task2.proto`, run
   `python build_protos.py`, and construct the adapter in `WorkerContext.__enter__`
   in `microbench/runner2.py`.
2. **Identity.** Create `leaderboard/<system>/system.json` with `system`,
   `proprietary`, `hosted`, `tuned`, and `tags`.
3. **Local server scripts.** Add executable `start`, `check`, and `stop` scripts, and
   optionally `install`, as in `leaderboard/dolt/`. `start` must send the server's
   output to a log file, and `stop` must stop only the process `start` launched.
4. **Runner.** Add a class to `leaderboard/run.py` with `backend`, `needs_psql`,
   `version_sql`, `scope`, `provision`, `load`, and `delete`, and register it in `SYSTEMS`.
   Everything it creates must be named after the run's database, so `delete` can
   find it by name. `scope` must identify the deletion target without secrets;
   cleanup refuses a journal from a different scope. A hosted class also sets `keys`, `region`, `service`, and
   `preflight`, and every `service` key must be in the validator's allowlist.
5. **Trial.** Run it with `--max-batch 5 --allow-dirty --out /tmp/lb`, and check that
   every cell is filled and nothing is left behind.
6. **Result.** Commit, run it in full, then validate and build.

Tests: `python -m unittest discover -s leaderboard -p 'test_*.py'` and
`node leaderboard/test_page.cjs`.
