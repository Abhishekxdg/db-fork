# BranchBench Leaderboard Context

BranchBench compares database systems that support branch-oriented workloads. This vocabulary applies to benchmark execution, evidence, comparison, and publication.

## Language

**Database system**:
A database product or service measured by BranchBench.

_Avoid_: database provider, plugin

**System key**:
The stable lowercase identifier used by commands, imports, and artifact paths for one database system.

_Avoid_: display name, provider name

**System integration**:
The submitted unit that supplies database metadata, connectivity, and any required self-hosted lifecycle behavior.

_Avoid_: provider folder, database implementation

**Connector**:
The boundary that provisions, loads, identifies, and deletes benchmark databases for one database system.

_Avoid_: driver, client

**Workload**:
A deterministic set of database actions and scale parameters that BranchBench executes and measures.

_Avoid_: result, campaign

**Microbenchmark run**:
One execution of the operations suite for one database system, machine, dataset, and source revision.

_Avoid_: micro campaign

**Macro campaign**:
One indivisible execution of all six agent workflow scenarios for one database system, preset, machine, and source revision.

_Avoid_: macro run, workflow result

**Attempt**:
One scenario execution within a macro campaign.

_Avoid_: campaign, retry

**Branch operation**:
A version-control-style database action such as branch, commit, diff, log, merge, rebase, revert, reset, or delete.

_Avoid_: workflow, SQL statement

**Time in branch operations**:
The share of successful measured operation time attributed to branch operations within one completed workflow. The denominator also includes successful general execution operations.

_Avoid_: workflow completion time, branch overhead

**Artifact**:
An immutable JSON record that contains one benchmark result, attempt, campaign, or branch-verb scaling result and its provenance.

_Avoid_: report, log

**Outcome**:
The evidence-based classification of an attempt or branch-verb scaling result.

_Avoid_: process exit code, cleanup result

**Cleanup verdict**:
The independent finding that the temporary benchmark database is absent after execution.

_Avoid_: outcome, server stop

**Execution profile**:
The declared topology that describes where the benchmark client and database server ran.

_Avoid_: instance type, machine label

**Comparison cohort**:
A set of artifacts with compatible workload, source, runtime, environment, and execution profile identities.

_Avoid_: all results, release

**Publication manifest**:
The explicit allowlist of validated artifacts included in the production site.

_Avoid_: result index, deployment list

**Site phase**:
One of the local, alpha, or production content policies used when assembling the static site.

_Avoid_: environment, execution profile

**Self-hosted system**:
A database system whose server lifecycle BranchBench controls on the benchmark machine.

_Avoid_: local system

**Hosted system**:
A managed database service that BranchBench reaches over the network without controlling its server lifecycle.

_Avoid_: cloud run
