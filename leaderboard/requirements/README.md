# BranchBench requirements

## Official AWS compute

These requirements apply to official `prod` runs for self-hosted database systems.

1. The first automated production runner shall launch one On-Demand `m7a.2xlarge` worker for each database system.
2. Each worker shall use a 40 GiB `gp3` root volume and a dedicated 200 GiB `gp3` data volume at the 3,000 IOPS and 125 MiB/s baseline.
3. The data volume shall use XFS with `crc=1,reflink=1` and shall mount at `/mnt/branchbench`.
4. Every ranked database in one hardware cohort shall use the same EC2 instance and volume configuration.
5. The production launcher shall select the EC2 instance type. The benchmark worker shall receive one database system and shall not select its own instance type.
6. The launcher design shall allow a later per-database instance map without changing the benchmark worker interface.
7. Before the launcher uses different instance types, result provenance and ranking groups shall separate runs by instance type and volume configuration.
8. Before the first ranked release, a full calibration campaign shall confirm that this profile avoids memory exhaustion, disk exhaustion, and sustained EBS saturation.

The fixed compute and storage profile makes the first production cohort comparable. If calibration fails, change the profile before publishing the cohort. The launcher owns placement because placement is an infrastructure decision, not a workload decision.
