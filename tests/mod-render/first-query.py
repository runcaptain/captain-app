#!/usr/bin/env python3
"""Props for captain-runs in the shot check: the seeded replay's first query, so the shot shows it side by side.
Reads the scratch workspace's own serve.json and sends the token in a header (never on a command line).
  first-query.py <CST_HOME>"""
import glob
import json
import sys
import time
import urllib.request

home = sys.argv[1]
BACKTEST, VERSION = "demo-historic-replay", 1
serve = None
for _ in range(40):
    found = glob.glob(f"{home}/**/serve.json", recursive=True)
    if found:
        serve = json.load(open(found[0]))
        break
    time.sleep(0.5)
out = {"backtest": BACKTEST, "version": VERSION, "section": "queries"}
if serve:
    req = urllib.request.Request(
        f"http://127.0.0.1:{serve['port']}/api/runs/{BACKTEST}@v{VERSION}/queries?offset=0&limit=1",
        headers={"Authorization": f"Bearer {serve['token']}"})
    try:
        rows = json.load(urllib.request.urlopen(req, timeout=10))
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        if rows:
            out["query"] = rows[0]["query_id"]
    except Exception as e:
        print(f"first-query: {e}", file=sys.stderr)
print(json.dumps(out))
