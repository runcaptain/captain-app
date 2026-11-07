"""Who reads a dataset: a cheap, append-only log of reads, for the dataset's Insights ("Frequent readers").

A read is recorded where a dataset's rows are read: the app's rows and head routes ("app"), `cst.dataset()` in a
notebook ("notebook"), and `cst data head` / `pull` ("cli", or "agent" when CST_AGENT names the agent).

Cheap by design:
- one small file per read, under the dataset's own prefix (`metastore/reads/<dataset>/<day>/<id>.json`), written
  with put_new, so writers never collide and reading one dataset's log never lists another's;
- the same reader of the same version the same way counts once per 10 minutes (in this process), so paging through
  the grid doesn't write a file per page;
- recording never raises and never slows the read it describes beyond one small write.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import uuid
from collections import Counter
from urllib.parse import quote

from ..workspace import Context, whoami

WINDOW_S = 600
VIAS = ("app", "notebook", "agent", "cli")
_last: dict[tuple, float] = {}
_lock = threading.Lock()


def _prefix(name: str) -> str:
    # The name, encoded, so "catalogs/products" never lists "catalogs/products-v2" or a deeper name.
    return f"metastore/reads/{quote(name, safe='')}/"


def record(ctx: Context, name: str, version: int | None, via: str, *, user: str | None = None) -> bool:
    """Log one read. Returns whether it was written (False when it repeats one inside the window, or the store
    refused). Never raises."""
    try:
        agent = os.environ.get("CST_AGENT") or None
        if via == "cli" and agent:
            via = "agent"
        who = user or whoami()
        key = (ctx.workspace.name, name, version, who, via, agent)
        now = time.time()
        with _lock:
            if now - _last.get(key, 0.0) < WINDOW_S:
                return False
            _last[key] = now
        at = dt.datetime.now(dt.UTC)
        rec = {"dataset": name, "version": version, "user": who, "agent": agent, "via": via,
               "at": at.isoformat(timespec="seconds")}
        ctx.store.put_new(f"{_prefix(name)}{at.date().isoformat()}/{uuid.uuid4().hex}.json", json.dumps(rec).encode())
        return True
    except Exception:  # noqa: BLE001  a log must never break the read it describes
        return False


def reads(ctx: Context, name: str, days: int = 90) -> list[dict]:
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).date().isoformat()
    pfx = _prefix(name)
    out = []
    for k in ctx.store.list(pfx):
        day = k[len(pfx):].split("/", 1)[0]
        if day < cutoff or not k.endswith(".json"):
            continue
        try:
            out.append(json.loads(ctx.store.get(k)))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out, key=lambda r: r.get("at", ""), reverse=True)


def readers(ctx: Context, name: str, days: int = 90) -> dict:
    """Frequent readers: who read it, how often, how (app, notebook, agent, cli), which versions, and when last."""
    rs = reads(ctx, name, days)
    by: dict[tuple, dict] = {}
    for r in rs:
        k = (r.get("user"), r.get("agent"))
        e = by.setdefault(k, {"user": r.get("user"), "agent": r.get("agent"), "reads": 0, "via": Counter(), "versions": set(), "last": r.get("at")})
        e["reads"] += 1
        e["via"][r.get("via")] += 1
        if r.get("version") is not None:
            e["versions"].add(r["version"])
    out = [{**e, "via": dict(e["via"]), "versions": sorted(e["versions"])} for e in by.values()]
    out.sort(key=lambda e: (-e["reads"], e["last"] or ""))
    return {"days": days, "reads": len(rs), "by_via": dict(Counter(r.get("via") for r in rs)), "readers": out}
