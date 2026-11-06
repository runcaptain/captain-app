"""Scale benchmarks: time real reads of a dataset where it lives, and keep the numbers.

"Anything that doesn't scale is basically just a demo." A benchmark pages one dataset at several depths
(first page, quarter, middle, three quarters, last page), counts it, and sorts one column, with DuckDB
reading from local disk or the bucket. Results are kept under bench/ with the machine, the dataset version
and the time, so a scale claim always points at a measured run.
"""
from __future__ import annotations

import datetime as dt
import platform
import time

import yaml

from .model.entities import new_id
from .storage.registry import dump_yaml
from .workspace import Context, whoami


def rows(ctx: Context, dataset: str, page: int = 200, dry_run: bool = False) -> dict:
    from . import browse as B

    m, _ = ctx.registry.resolve("dataset", dataset)
    t0 = time.time()
    first = B.rows(ctx, m, offset=0, limit=page, count=True)
    total = first.get("total") or 0
    timings = [{"step": "first page and count", "offset": 0, "ms": int((time.time() - t0) * 1000)}]
    for share in (0.25, 0.5, 0.75):
        off = int(total * share)
        r = B.rows(ctx, m, offset=off, limit=page)
        timings.append({"step": f"page at {int(share * 100)}%", "offset": off, "ms": r["ms"]})
    off = max(0, total - page)
    r = B.rows(ctx, m, offset=off, limit=page)
    timings.append({"step": "last page", "offset": off, "ms": r["ms"]})
    col = next((c["name"] for c in first["columns"] if any(k in c["type"].upper() for k in ("INT", "DOUBLE", "FLOAT", "DECIMAL"))),
               first["columns"][0]["name"])
    r = B.rows(ctx, m, offset=0, limit=page, order=col, desc=True)
    timings.append({"step": f"sorted by {col}, first page", "offset": 0, "ms": r["ms"]})
    out = {"id": new_id().lower(), "kind": "rows", "dataset": f"{m.name}@v{m.version}", "file": first["file"], "rows": total,
           "bytes": next((f["bytes"] for f in first["files"] if f["file"] == first["file"]), None), "page": page,
           "timings": timings, "worst_ms": max(x["ms"] for x in timings), "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "by": whoami(), "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
           "where": "bucket" if first["file"].startswith(("s3://", "gs://")) or hasattr(ctx.store, "bucket") else "local disk"}
    if not dry_run:
        ctx.store.put_new(f"bench/{out['at'][:10]}-{out['id']}.yaml", dump_yaml(out))
    return out


def listing(ctx: Context) -> list[dict]:
    out = [yaml.safe_load(ctx.store.get(k)) for k in ctx.store.list("bench/") if k.endswith(".yaml")]
    return sorted(out, key=lambda d: d["at"], reverse=True)
