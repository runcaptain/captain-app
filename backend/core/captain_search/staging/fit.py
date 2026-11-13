"""Will a staged endpoint fit on this computer? Asked before "On this computer" is offered.

A local endpoint holds, in one process: the catalog's rows, a keyword index over them for each version, and for a
trained ranker the feature tables too. When the baseline runs beside the candidate that is two indexes. This
estimates that from the catalog's row count, compares it with the memory and disk free right now, and answers
`fits`, `tight` (allowed, with a warning), `wont-fit` (not offered: stage it in your cloud) or `over-limit`.

Fitting is not enough. A big model with a lot of products should not run on a laptop even when the memory is
there, so there is a size limit for deploying on this computer, separate from free memory: by default a model that
needs more than 1 GB to serve (this estimate, with headroom, counting the baseline beside it) is `over-limit` and
not offered here. The limit is a workspace setting (`staging.local_limit_mb`, 0 for none). It is the first test;
free memory is the second.

The per-product figures were measured, not guessed. Measured 2026-11-11 on an Apple silicon laptop with Python
3.14, on catalogs shaped like the demo catalog (short titles and descriptions), by building the same objects the
runtime builds and reading the process's peak memory:

    products   candidate (index + features)   baseline (index)   whole process   time to load   one query
         480                 3.8 MB                  2.2 MB             82 MB         0.02 s        0.6 ms
       3,000                23.8 MB                 13.8 MB            114 MB         0.06 s        1.3 ms
     200,000             1,601.6 MB                886.1 MB          2,648 MB         7.0 s       116.5 ms

That is about 8.0 KB a product for a trained ranker, 4.5 KB a product for a keyword version, 0.45 KB a product for
the rows themselves, and about 120 MB for the process before any catalog. Loading takes about 35 microseconds a
product for both versions together. A catalog with long descriptions needs more: the estimate adds 30% headroom,
and the answer is "tight" well before memory actually runs out.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from ..workspace import Context, home

MEASURED = "2026-11-11"
BASE_MB = 120.0          # the process, its imports and the web server, before any catalog
ROWS_KB = 0.45           # the catalog's rows, held once
KEYWORD_KB = 4.5         # a keyword index, per product, per version
RANKER_KB = 8.0          # a keyword index plus the feature tables of a trained ranker, per product
LOAD_US = {"keyword": 12.5, "ranker": 22.5}  # microseconds a product to build each
QUERY_US = 0.58          # microseconds a product for one keyword query (it scans the matching postings)
HEADROOM = 1.3
TIGHT = 0.6              # needing more than this share of what is free is "tight"
LOG_MB = 200.0           # room for the request log and the bundle's own files


def free() -> dict:
    """Memory and disk free on this computer right now, in MB. Memory is what can be had without swapping: free
    plus what the system would give back (inactive and cached pages)."""
    mem = None
    try:
        if os.path.exists("/proc/meminfo"):
            for ln in open("/proc/meminfo"):
                if ln.startswith("MemAvailable:"):
                    mem = int(ln.split()[1]) / 1024
        else:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(out.split("page size of")[1].split()[0])
            pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in (ln.split(":") for ln in out.splitlines()[1:] if ":" in ln) if v.strip().rstrip(".").isdigit()}
            mem = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)) * page / (1024 * 1024)
    except Exception:  # noqa: BLE001  can't tell: say so, never guess
        mem = None
    try:
        os.makedirs(home(), exist_ok=True)
        disk = shutil.disk_usage(home()).free / (1024 * 1024)
    except OSError:
        disk = None
    return {"memory_mb": mem, "disk_mb": disk}


def _products(ctx: Context, pipeline: str) -> tuple[int, str]:
    """How many products a version ranks, and whether it is a trained ranker or keyword search. From the catalog's
    manifest: nothing is loaded."""
    try:
        pm, _ = ctx.registry.resolve("pipeline", pipeline)
    except Exception:  # noqa: BLE001  an address the team runs: it holds nothing here
        return 0, "wrapped"
    if not pm.catalog or pm.engine not in ("local-bm25", "bm25-ltr"):
        return 0, "wrapped"
    m, _ = ctx.registry.resolve("dataset", pm.catalog)
    kind = "ranker" if pm.engine == "bm25-ltr" and ((pm.spec or {}).get("rerank") or {}).get("model") else "keyword"
    return int(m.row_count or 0), kind


def need(arms: list[tuple[int, str]]) -> dict:
    """What these versions need on one computer. `arms` is [(products, "ranker" | "keyword" | "wrapped")]."""
    n = max([a[0] for a in arms] or [0])
    kb = ROWS_KB * n + sum((RANKER_KB if k == "ranker" else KEYWORD_KB if k == "keyword" else 0.0) * p for p, k in arms)
    mem = (BASE_MB + kb / 1024) * HEADROOM
    load = sum(LOAD_US.get(k, 0.0) * p for p, k in arms) / 1e6 + 1.0
    return {"products": n, "memory_mb": round(mem), "disk_mb": round(LOG_MB + n * ROWS_KB / 1024 * 2),
            "load_s": round(load, 1), "query_ms": round(max(1.0, n * QUERY_US / 1000), 1)}


def _gb(mb: float) -> str:
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024:.0f} GB"
    return f"{mb / 1024:.0f} GB" if mb >= 10 * 1024 else f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def limit_mb(ctx: Context) -> int:
    from .. import settings as ST

    return int((ST.get(ctx).get("staging") or {}).get("local_limit_mb") or 0)


def check(ctx: Context, arms: dict[str, str], have: dict | None = None, allow_large: bool = False) -> dict:
    """{outcome: fits | tight | wont-fit | over-limit | unknown, need, free, limit_mb, text}. `arms` is
    {arm: model version}. `allow_large` skips the size limit, never the free-memory test."""
    nd = need([_products(ctx, r) for r in arms.values()])
    lim = limit_mb(ctx)
    if lim and nd["memory_mb"] > lim and not allow_large:
        return {"need": nd, "free": None, "measured": MEASURED, "limit_mb": lim, "outcome": "over-limit",
                "text": f"This model needs about {_gb(nd['memory_mb'])} to serve, which is more than the {_gb(lim)} limit for deploying on "
                        "this computer. Deploy it to a cloud."}
    hv = have if have is not None else free()
    mem, disk = hv.get("memory_mb"), hv.get("disk_mb")
    out = {"need": nd, "free": {"memory_mb": None if mem is None else round(mem), "disk_mb": None if disk is None else round(disk)}, "measured": MEASURED,
           "limit_mb": lim}
    what = "This catalog" if nd["products"] else "This model"
    if mem is None:
        return {**out, "outcome": "unknown",
                "text": f"{what} needs about {_gb(nd['memory_mb'])} of memory to serve. Captain couldn't read how much this computer has free."}
    if disk is not None and nd["disk_mb"] > disk:
        return {**out, "outcome": "wont-fit",
                "text": f"{what} and its request log need about {_gb(nd['disk_mb'])} of disk, and this computer has {_gb(disk)} free. Deploy it to a cloud."}
    if nd["memory_mb"] > mem:
        return {**out, "outcome": "wont-fit",
                "text": f"{what} needs about {_gb(nd['memory_mb'])} of memory to serve, and this computer has {_gb(mem)} free. Deploy it to a cloud."}
    if nd["memory_mb"] > TIGHT * mem:
        return {**out, "outcome": "tight",
                "text": f"{what} needs about {_gb(nd['memory_mb'])} of memory to serve, and this computer has {_gb(mem)} free. It will run, "
                        "but other work on this computer may slow down. Your cloud has more room."}
    slow = f" With {nd['products']:,} products a query takes about {nd['query_ms']:.0f} ms here." if nd["query_ms"] >= 50 else ""
    return {**out, "outcome": "fits",
            "text": f"Needs about {_gb(nd['memory_mb'])} of memory, and this computer has {_gb(mem)} free. It loads in about {nd['load_s']:.0f} s.{slow}"}
