#!/usr/bin/env python3
"""Shot check: a dataset with several table files (like a catalog registered by reference: items, meta, coverage), so
the Sample data tab draws its file picker above the rows. The files sit next to the workspace, read by absolute path.
Run with cst's own Python and CST_HOME."""
import json
import os

from captain_search import external
from captain_search.model.entities import DatasetKind, ExternalFile
from captain_search.workspace import open_context

ctx = open_context()
d = os.path.join(os.environ["CST_HOME"], "multifile")
os.makedirs(d, exist_ok=True)
files = {"items.jsonl": [{"pid": f"p{i:04d}", "title": f"Product {i}", "price": round(3 + i * 0.7, 2)} for i in range(300)],
         "meta.json": [{"key": "source", "value": "shot check"}],
         "coverage.json": [{"field": "title", "share": 1.0}]}
for name, rows in files.items():
    with open(os.path.join(d, name), "w") as f:
        f.write("\n".join(json.dumps(r) for r in rows) if name.endswith(".jsonl") else json.dumps(rows))
ext = [ExternalFile(uri=os.path.join(d, n), bytes=os.path.getsize(os.path.join(d, n))) for n in files]
external.register(ctx, "shot-multifile", [], DatasetKind.catalog, description="Three files, for the file picker.",
                  row_count=300, extra_files=ext)
print("shot-multifile: 3 files")
