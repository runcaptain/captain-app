#!/usr/bin/env python3
"""Shot check: wide tables in the demo store's database, for the Rows tab at 13 and 40 columns. `usage` has 40 columns
(like a Databricks system.billing.usage), `spend` has 13. Run with cst's own Python and CST_HOME, then pull the tables."""
import duckdb

from captain_search.workspace import open_context

ctx = open_context()
url = ctx.load_connection("demo-store").url
path = url.split("duckdb:///", 1)[1]
con = duckdb.connect(path)
cols = ", ".join([f"'acct-' || (i % 7) as account_id", "i as record_id", "date '2026-10-01' + (i % 30)::integer as usage_date"]
                 + [f"round((i * {k}) % 997 / 7.0, 2)::decimal(12,2) as metric_{k:02d}" for k in range(1, 34)]
                 + ["['sku-' || (i % 5), 'tier-' || (i % 3)] as tags", "{'region': 'us-west', 'zone': i % 4} as location",
                    "i % 2 = 0 as billable", "timestamp '2026-10-01 00:00:00' + to_seconds(i * 3600) as recorded_at"])
con.execute(f"create or replace table usage as select {cols} from range(12480) t(i)")
spend = ", ".join(["'endpoint-' || (i % 9) as endpoint", "i as request_id", "date '2026-10-01' + (i % 30)::integer as day",
                   "round(i * 0.013, 4)::decimal(14,4) as usd", "i % 1000 as input_tokens", "i % 700 as output_tokens",
                   "'gpt-' || (i % 3) as model", "i % 5 = 0 as cached", "(i % 250) / 10.0 as latency_ms",
                   "'team-' || (i % 4) as team", "['a', 'b'] as labels", "timestamp '2026-10-01' + to_seconds(i * 60) as at",
                   "null::varchar as note"])
con.execute(f"create or replace table spend as select {spend} from range(4200) t(i)")
con.close()
print("usage: 40 columns, spend: 13 columns")
