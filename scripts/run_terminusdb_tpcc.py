#!/usr/bin/env python3
"""
Run the macrobenchmark's TPC-C transactions and CH queries on TerminusDB.

TerminusDB has no SQL, so it cannot run the macrobenchmark. This script
runs the same data workload written for its own interfaces
(macrobench/tpcc_woql.py: one document-API or WOQL request per SQL
statement of macrobench/tpcc.py) on the same seeded CH dataset, and
records every request under the SQL statement it stands for. The result
parquet has the layout of the other result files, so the statement rows
can be put next to the ones the SQL backends record for the same text.

    python scripts/run_terminusdb_tpcc.py --output-dir run_stats/terminusdb_tpcc
    python scripts/run_terminusdb_tpcc.py --items 1000 --customers-per-district 30 \\
        --orders-per-district 30 --txns 100          # a small dataset

The database is seeded once and kept (pass --reseed to rebuild it). The
workload runs on a fresh branch of it, so every run starts from the seed.
"""

import argparse
import json
import os
import random
import statistics
import time
from datetime import timedelta

from dblib import result_collector as rc
from dblib import result_pb2 as rslt
from dblib import terminusdb
from dblib.db_api import OpStatus
from macrobench import tpcc, tpcc_woql
from macrobench.datagen.ch import CHGenerator, CHScale

SCHEMA = "db_setup/ch_benchmark_schema.sql"
SEEDED = "seeded"   # branch that pins the freshly seeded state
WORK = "work"


def seed(db_name: str, scale: CHScale, seed_value: int) -> None:
    terminusdb.setup_database(db_name, SCHEMA)
    start = time.time()
    for table, columns, rows in CHGenerator(scale, seed_value).tables():
        n = terminusdb.insert_rows(db_name, table, columns, rows)
        print(f"  seeded {table:>12}: {n:>9,} rows  ({time.time() - start:6.0f}s)", flush=True)
    suite = terminusdb.TerminusDBToolSuite.init_for_bench(rc.ResultCollector(), db_name, "main")
    suite.branch(SEEDED, "main", timed=False, raise_on_error=True)
    suite.close_connection()


def is_seeded(db_name: str) -> bool:
    suite = terminusdb.TerminusDBToolSuite.init_for_bench(rc.ResultCollector(), db_name, "main")
    try:
        return SEEDED in suite.list_branches()
    except terminusdb.TerminusDBError:
        return False
    finally:
        suite.close_connection()


def run(args) -> dict:
    scale = CHScale(warehouses=args.warehouses, items=args.items,
                    customers_per_district=args.customers_per_district,
                    orders_per_district=args.orders_per_district)
    if args.reseed or not is_seeded(args.db_name):
        print(f"Seeding {args.db_name} ({scale})")
        seed(args.db_name, scale, args.seed)
    else:
        print(f"Reusing the seeded database {args.db_name}")

    collector = rc.ResultCollector(run_id=args.run_id, output_dir=args.output_dir)
    suite = terminusdb.TerminusDBToolSuite.init_for_bench(collector, args.db_name, "main")
    if WORK in suite.list_branches():
        suite.delete(WORK, timed=False, raise_on_error=True)
    suite.branch(WORK, SEEDED, label="work_branch", raise_on_error=True)

    rng = random.Random(args.seed)
    names = [name for name, _ in tpcc.TXN_MIX]
    weights = [weight for _, weight in tpcc.TXN_MIX]
    transactions, failures = {}, []

    def measured(kind: str, name: str, call):
        """Run one transaction or query; record its total as an EXEC row."""
        db = tpcc_woql.NativeSession(suite, WORK, label=name)
        start_wall, start = time.time(), time.perf_counter()
        status, error = OpStatus.OK, ""
        try:
            call(db)
        except Exception as e:
            status, error = OpStatus.FAILED, f"{type(e).__name__}: {e}"
            failures.append({"kind": kind, "name": name, "error": error[:300]})
        latency = time.perf_counter() - start
        collector.emit(rslt.OpType.EXEC, status=status, latency=latency, start_time=start_wall,
                       end_time=time.time(), ref=WORK, exec_id=db.exec_id, label=name,
                       sql_query=f"{kind} {name}", error_message=error, num_keys_touched=0)
        if status == OpStatus.OK:
            transactions.setdefault(name, []).append(latency)
        return status == OpStatus.OK

    print(f"Running {args.txns} transactions (TPC-C mix)")
    started = time.time()
    for i in range(args.txns):
        name = rng.choices(names, weights)[0]
        measured("transaction", name, lambda db: tpcc_woql.TRANSACTIONS[name](db, rng, scale))
    txn_seconds = time.time() - started

    since = tpcc._now() - timedelta(days=args.days)
    for name, query in tpcc_woql.QUERIES.items():
        for i in range(args.query_runs):
            print(f"Running CH {name} ({i + 1}/{args.query_runs})", flush=True)
            arguments = (since, tpcc._now()) if name == "q6" else (since,)
            if not measured("query", name, lambda db: query(db, *arguments)):
                # The server keeps working on a request the client gave up
                # on; do not time further runs of it on top of that.
                break

    summary = summarise(collector, transactions, failures)
    collector.write_to_parquet(append=False)   # hands the rows over
    summary.update({
        "run_id": args.run_id, "backend": "TERMINUSDB", "scale": scale.__dict__,
        "transactions_requested": args.txns, "transactions_seconds": round(txn_seconds, 2),
        "implementation": suite.implementation(), "observed": dict(suite.OBSERVED),
    })
    suite.close_connection()
    path = os.path.join(args.output_dir, f"{args.run_id}_summary.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
    print_summary(summary)
    print(f"\nWrote {os.path.join(args.output_dir, args.run_id + '.parquet')} and {path}")
    return summary


def _stats(latencies: list) -> dict:
    ordered = sorted(latencies)
    return {"count": len(ordered),
            "median_ms": round(statistics.median(ordered) * 1000, 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 2)}


def summarise(collector, transactions: dict, failures: list) -> dict:
    statements: dict = {}
    for row in collector.results:
        if row.op_type == rslt.OpType.EXEC or row.status != OpStatus.OK or not row.sql_query:
            continue
        statements.setdefault((row.label, row.sql_query), []).append(row.latency)
    return {
        "transactions": {name: _stats(v) for name, v in transactions.items()},
        "statements": [{"in": label, "sql": sql, **_stats(v)}
                       for (label, sql), v in statements.items()],
        "failures": failures,
    }


def print_summary(summary: dict) -> None:
    print("\nPer transaction / query (whole call):")
    for name, s in summary["transactions"].items():
        print(f"  {name:<14} n={s['count']:<5} median {s['median_ms']:>10.1f} ms   p95 {s['p95_ms']:>10.1f} ms")
    print("\nPer statement:")
    for s in summary["statements"]:
        print(f"  {s['in']:<13} n={s['count']:<5} median {s['median_ms']:>10.1f} ms   {s['sql'][:70]}")
    for failure in summary["failures"]:
        print(f"  FAILED {failure['kind']} {failure['name']}: {failure['error']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-name", default="bb_tpcc_woql")
    parser.add_argument("--run-id", default="terminusdb_tpcc_woql")
    parser.add_argument("--output-dir", default="/tmp/run_stats")
    parser.add_argument("--txns", type=int, default=200, help="transactions to run (TPC-C mix)")
    parser.add_argument("--query-runs", type=int, default=3, help="runs of each CH query")
    parser.add_argument("--days", type=int, default=7, help="CH queries look back this many days")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reseed", action="store_true", help="rebuild the database")
    parser.add_argument("--warehouses", type=int, default=1)
    parser.add_argument("--items", type=int, default=100_000)
    parser.add_argument("--customers-per-district", type=int, default=3_000)
    parser.add_argument("--orders-per-district", type=int, default=3_000)
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    run(args)


if __name__ == "__main__":
    main()
