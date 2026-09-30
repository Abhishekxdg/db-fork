const suite = {
 "suite": "local-2",
 "seed": 20260929,
 "tries": 3,
 "table": "item",
 "range_size": 100,
 "chain_length": 4,
 "dataset": {
  "path": "db_setup/ch_benchmark_seed.sql",
  "sha256": "3a1b5d4f01a98a27cc2241b4267e51a17f31a4469e0e3ed38c7d8df4abe1826e",
  "row_counts": {
   "region": 5,
   "nation": 5,
   "supplier": 1,
   "warehouse": 10,
   "district": 100,
   "customer": 10000,
   "history": 0,
   "item": 1000,
   "stock": 10000,
   "orders": 0,
   "new_order": 0,
   "order_line": 0
  }
 },
 "rows": [
  {
   "op": "time_to_first_query",
   "batch": 1
  },
  {
   "op": "branch_create",
   "batch": 1
  },
  {
   "op": "branch_connect",
   "batch": 100
  },
  {
   "op": "read",
   "batch": 1000
  },
  {
   "op": "range_read",
   "batch": 200
  },
  {
   "op": "insert",
   "batch": 1000
  },
  {
   "op": "update",
   "batch": 1000
  },
  {
   "op": "range_update",
   "batch": 200
  }
 ]
};
const data = [
 {
  "system": "Neon",
  "proprietary": "yes",
  "hosted": "yes",
  "tuned": "no",
  "tags": [
   "Postgres protocol",
   "Copy-on-write branches"
  ],
  "basis": "Mock, not a measurement. Branch create and connect follow earlier Neon measurements in run_stats_final (0.171 s per create, 0.78 s first connect, 117 ms repeated connect), adjusted for a client in US East. Data operations assume about 20 ms per statement.",
  "date": "2026-09-30",
  "machine": "apple-m5-32gb",
  "region": "aws-us-east-1",
  "rtt_ms": 18.4,
  "client_location": "US East",
  "provision_time": 4.8,
  "load_time": 9.6,
  "result": [
   [
    1.079,
    1.024,
    0.995
   ],
   [
    0.199,
    0.189,
    0.195
   ],
   [
    16.029,
    15.263,
    14.452
   ],
   [
    21.985,
    21.115,
    20.35
   ],
   [
    4.934,
    4.564,
    4.386
   ],
   [
    22.093,
    21.842,
    20.81
   ],
   [
    22.939,
    20.852,
    21.304
   ],
   [
    5.268,
    5.115,
    5.094
   ]
  ],
  "mock": true,
  "source": "mock/neon.json"
 },
 {
  "system": "Tiger Cloud",
  "proprietary": "yes",
  "hosted": "yes",
  "tuned": "no",
  "tags": [
   "Postgres protocol",
   "Service forks"
  ],
  "basis": "Mock, not a measurement. Branch create and connect follow earlier Tiger measurements in run_stats_final (55.8 s mean create, 0.52 s connect). Data operations assume about 22 ms per statement from a client in US East.",
  "date": "2026-09-30",
  "machine": "apple-m5-32gb",
  "region": "us-east-1",
  "rtt_ms": 21.7,
  "client_location": "US East",
  "provision_time": 96.5,
  "load_time": 11.2,
  "result": [
   [
    58.433,
    55.028,
    52.563
   ],
   [
    54.509,
    51.815,
    54.406
   ],
   [
    55.157,
    51.903,
    50.658
   ],
   [
    23.788,
    22.039,
    21.47
   ],
   [
    5.001,
    4.706,
    4.775
   ],
   [
    23.994,
    21.927,
    21.981
   ],
   [
    23.634,
    22.616,
    22.775
   ],
   [
    5.621,
    5.246,
    5.147
   ]
  ],
  "mock": true,
  "source": "mock/tiger.json"
 },
 {
  "system": "Xata",
  "proprietary": "yes",
  "hosted": "yes",
  "tuned": "no",
  "tags": [
   "Postgres protocol",
   "Copy-on-write branches"
  ],
  "basis": "Mock, not a measurement. Branch create and connect follow earlier Xata measurements in run_stats_final (54.6 s mean and 43.8 s median create, 0.12 s connect). Data operations assume about 20 ms per statement from a client in US East.",
  "date": "2026-09-30",
  "machine": "apple-m5-32gb",
  "region": "us-east-1",
  "rtt_ms": 19.2,
  "client_location": "US East",
  "provision_time": 63.4,
  "load_time": 10.3,
  "result": [
   [
    46.487,
    44.329,
    45.614
   ],
   [
    46.436,
    43.25,
    45.371
   ],
   [
    14.476,
    13.796,
    14.045
   ],
   [
    20.798,
    20.272,
    20.243
   ],
   [
    4.7,
    4.5,
    4.597
   ],
   [
    22.325,
    20.597,
    21.174
   ],
   [
    23.063,
    20.583,
    20.507
   ],
   [
    5.138,
    4.837,
    4.878
   ]
  ],
  "mock": true,
  "source": "mock/xata.json"
 }
];
const excluded = [
 {
  "system": "dolt",
  "machine": "apple-m5-32gb",
  "source": "dolt/results/20260930/apple-m5-32gb.json",
  "reason": "invalid: suite differs from the current suite.json; suite_sha256 differs from the current suite.json"
 },
 {
  "system": "dolt_mysql",
  "machine": "apple-m5-32gb",
  "source": "dolt_mysql/results/20260930/apple-m5-32gb.json",
  "reason": "invalid: suite differs from the current suite.json; suite_sha256 differs from the current suite.json"
 }
];
