"""Insights for a dataset, in the shape of Unity Catalog's Insights tab: what uses it, who works with it, and what
happened to it lately. Read from Captain's own records, nothing sampled:
- used_by: the pipelines built on it, the runs scored with it, the environments it reached and the datasets made
  from it (lineage, downstream, up to three steps), each with how far away it is and which versions it used;
- notebooks: saved notebooks that name it, with how many cells do;
- people: who made its versions and who built on it;
- activity: those events newest first.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter

from ..storage.backend import NotFound
from ..workspace import Context

# Lineage node kinds to registry kinds (lineage.py names nodes kind:name@vN).
REGISTRY_KIND = {"dataset": "dataset", "pipeline": "pipeline", "run": "backtest-run"}
# A run opens in Runs, with its comparison in place (the Compare screen left the app, 2026-11-12).
SECTION = {"pipeline": "runs", "run": "runs", "dataset": "data", "environment": "runs"}


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        return t.isoformat()
    return str(t)


def _split(label: str) -> tuple[str, int | None]:
    m = re.match(r"^(.*)@v(\d+)$", label or "")
    return (m.group(1), int(m.group(2))) if m else (label, None)


def dataset_insights(ctx: Context, name: str, max_versions: int = 10, depth: int = 3) -> dict:
    from .. import lineage as LN
    from .. import notebook as NB

    vs = ctx.registry.versions("dataset", name)
    if not vs:
        raise NotFound(f"there's no dataset named {name}")
    recent = vs[-max(1, int(max_versions)):]
    versions, makers, activity = [], Counter(), []
    for v in recent:
        m = ctx.registry.load("dataset", name, v)
        pv = m.provenance
        versions.append({"version": v, "created_by": m.created_by, "created_at": _iso(m.created_at), "rows": m.row_count,
                         "source": getattr(pv, "source", None), "connection": getattr(pv, "connection", None)})
        makers[m.created_by] += 1
        activity.append({"at": _iso(m.created_at), "who": m.created_by, "what": f"made version {v}",
                         "kind": "dataset", "id": f"dataset:{name}@v{v}"})

    used: dict[str, dict] = {}
    for v in recent:
        g = LN.neighborhood(ctx, f"dataset:{name}@v{v}", 0, depth)
        for n in g["nodes"]:
            base, _ = _split(n.get("label", ""))
            if n["level"] == 0 or (n["kind"] == "dataset" and base == name):
                continue  # itself, or another of its own versions
            e = used.setdefault(n["id"], {"id": n["id"], "kind": n["kind"], "label": n["label"], "name": base,
                                          "version": _split(n["label"])[1], "level": n["level"], "via": []})
            e["level"] = min(e["level"], n["level"])
            if v not in e["via"]:
                e["via"].append(v)

    builders = Counter()
    for e in used.values():
        e["section"] = SECTION.get(e["kind"])
        rk = REGISTRY_KIND.get(e["kind"])
        if not rk or e["version"] is None:
            continue
        try:
            m = ctx.registry.load(rk, e["name"], e["version"])
        except Exception:  # noqa: BLE001  a node lineage knows but the registry can't open: listed without who/when
            continue
        e["created_by"], e["created_at"] = m.created_by, _iso(m.created_at)
        if e["level"] == 1:
            builders[m.created_by] += 1
        verb = {"pipeline": "built model version", "run": "scored run", "dataset": "made dataset"}[e["kind"]]
        activity.append({"at": e["created_at"], "who": m.created_by, "what": f"{verb} {e['label']}", "kind": e["kind"], "id": e["id"]})

    by_kind: dict[str, list] = {}
    for e in sorted(used.values(), key=lambda e: (e["level"], e.get("created_at") or "", e["label"])):
        by_kind.setdefault(e["kind"], []).append(e)

    # Saved notebooks that name it (as a word, so "products" doesn't match "products-v2").
    word = re.compile(rf"(?<![\w/-]){re.escape(name)}(?![\w-])")
    notebooks = []
    for nb in NB.listing(ctx):
        try:
            _, doc, _ = NB.load(ctx, nb["name"])
        except Exception:  # noqa: BLE001
            continue
        cells = [c for c in doc.get("cells", []) if word.search("".join(c.get("source", "")) if isinstance(c.get("source"), list) else str(c.get("source", "")))
                 or word.search(json.dumps(c.get("metadata", {}).get("captain", {})))]
        if cells:
            notebooks.append({"name": nb["name"], "cells": len(cells)})

    # Who reads it (data/reads.py): the app, notebooks, agents and the CLI, over the last 90 days.
    from . import reads as RD

    try:
        read = RD.readers(ctx, name)
    except Exception:  # noqa: BLE001  insights without the read log rather than none
        read = {"days": 90, "reads": 0, "by_via": {}, "readers": []}
    people = sorted({u for u in list(makers) + list(builders) if u},
                    key=lambda u: -(makers[u] + builders[u]))
    activity = sorted([a for a in activity if a["at"]], key=lambda a: a["at"], reverse=True)[:20]
    return {
        "dataset": name, "versions": versions, "versions_total": len(vs),
        "used_by": by_kind,
        "counts": {k: len(v) for k, v in by_kind.items()} | {"notebooks": len(notebooks), "reads": read["reads"]},
        "readers": read,
        "notebooks": notebooks,
        "people": [{"user": u, "versions": makers[u], "builds": builders[u]} for u in people],
        "activity": activity,
    }
