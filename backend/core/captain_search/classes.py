"""Dataset classes: a taxonomy the search team defines, instead of a fixed list of kinds.

A class has a name, a description, a colour, optional required columns, and an optional `role`. The
role is how the tools know what a dataset can be used for: a class with role `catalog` can feed a
pipeline, `queries` a backtest, `judgments` scoring, `events` historic replay. A class with no role is
just a label for people (for example `seasonal`, `pii-reviewed`, `vendor-feed`).

Classifying a dataset is mutable metadata, not part of the write-once version: who changed it and
when is kept in the classification's own history. Both files update by compare-and-swap.
  classes/classes.yaml              the class list
  classifications/<dataset>.yaml    {classes, history}
"""
from __future__ import annotations

import datetime as dt
import re

import yaml

from .model.entities import DatasetKind
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

ROLES = [k.value for k in DatasetKind if k.value != "generic"]
CLASSES_KEY = "classes/classes.yaml"
DEFAULTS = [
    {"name": "catalog", "description": "Products a pipeline searches over", "color": "#0043CA", "role": "catalog",
     "required_columns": ["id", "title"]},
    {"name": "queries", "description": "Query sets for backtests", "color": "#7C3AED", "role": "queries",
     "required_columns": ["query_id", "query"]},
    {"name": "judgments", "description": "Relevance grades", "color": "#059669", "role": "judgments",
     "required_columns": ["query_id", "product_id", "grade"]},
    {"name": "events", "description": "Search logs: impressions, clicks, purchases", "color": "#D97706", "role": "events",
     "required_columns": ["query", "product_id", "position", "clicked"]},
    {"name": "logs", "description": "Training or simulator logs", "color": "#6B7280", "role": "logs", "required_columns": []},
]
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read(ctx: Context, key: str, default):
    try:
        data, tag = ctx.store.get_with_tag(key)
        return yaml.safe_load(data) or default, tag
    except NotFound:
        return default, None


def _write(ctx: Context, key: str, body, tag):
    try:
        return ctx.store.put_if(key, dump_yaml(body), tag)
    except Conflict:
        raise Conflict("someone else changed this at the same moment. Reload and try again.") from None


def listing(ctx: Context, include=None) -> list[dict]:
    """Classes with how many datasets carry each. include(name) -> bool limits the count (the Explorer's pick)."""
    doc, tag = _read(ctx, CLASSES_KEY, None)
    if doc is None:  # first use: seed the defaults, which the team can edit or delete
        doc = {"classes": [{**c, "created_by": "default", "created_at": _now()} for c in DEFAULTS]}
        try:
            _write(ctx, CLASSES_KEY, doc, None)
        except Conflict:
            doc, _ = _read(ctx, CLASSES_KEY, {"classes": []})
    counts: dict[str, int] = {}
    for n in ctx.registry.names("dataset"):
        if include is not None and not include(n):
            continue
        for c in classes_of(ctx, n):
            counts[c] = counts.get(c, 0) + 1
    return [{**c, "datasets": counts.get(c["name"], 0)} for c in doc["classes"]]


def _validate(c: dict) -> dict:
    if not NAME.match(c.get("name", "")):
        raise ValueError("a class name is lowercase letters, digits, . _ - (up to 64)")
    if c.get("color") and not COLOR.match(c["color"]):
        raise ValueError("colour is a hex code like #0043CA")
    if c.get("role") not in (None, "", *ROLES):
        raise ValueError(f"role is one of {', '.join(ROLES)}, or none")
    cols = c.get("required_columns") or []
    if isinstance(cols, str):
        cols = [x.strip() for x in cols.split(",") if x.strip()]
    return {"name": c["name"], "description": c.get("description", ""), "color": c.get("color") or "#6B7280",
            "role": c.get("role") or None, "required_columns": cols}


def upsert(ctx: Context, c: dict, create: bool) -> dict:
    listing(ctx)  # seeds
    doc, tag = _read(ctx, CLASSES_KEY, {"classes": []})
    c = _validate(c)
    existing = next((x for x in doc["classes"] if x["name"] == c["name"]), None)
    if create and existing:
        raise ValueError(f"class {c['name']} exists")
    if not create and not existing:
        raise NotFound(f"no class {c['name']}")
    if existing:
        existing.update(c, updated_by=whoami(), updated_at=_now())
    else:
        doc["classes"].append({**c, "created_by": whoami(), "created_at": _now()})
    _write(ctx, CLASSES_KEY, doc, tag)
    return next(x for x in doc["classes"] if x["name"] == c["name"])


def delete(ctx: Context, name: str) -> None:
    used = [n for n in ctx.registry.names("dataset") if name in classes_of(ctx, n)]
    if used:
        raise ValueError(f"class {name} is on {len(used)} dataset(s), for example {used[0]}. Reclassify them first.")
    doc, tag = _read(ctx, CLASSES_KEY, {"classes": []})
    doc["classes"] = [x for x in doc["classes"] if x["name"] != name]
    _write(ctx, CLASSES_KEY, doc, tag)


def _ckey(dataset: str) -> str:
    return f"classifications/{dataset}.yaml"


def classes_of(ctx: Context, dataset: str) -> list[str]:
    doc, _ = _read(ctx, _ckey(dataset), None)
    if doc is not None:
        return list(doc.get("classes", []))
    try:  # never classified: fall back to the dataset's own role
        m = ctx.registry.load("dataset", dataset)
        return [] if m.dataset_kind == DatasetKind.generic else [m.dataset_kind.value]
    except NotFound:
        return []


def classify(ctx: Context, dataset: str, names: list[str]) -> dict:
    m = ctx.registry.load("dataset", dataset)  # must exist
    known = {c["name"]: c for c in listing(ctx)}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown class {unknown[0]}. Create it first.")
    roles = {known[n]["role"] for n in names if known[n]["role"]}
    if len(roles) > 1:
        raise ValueError(f"a dataset can carry one role at most; these classes have roles {sorted(roles)}")
    cols = {f["name"] for f in m.schema_fields}
    warnings = []
    for n in names:
        missing = [c for c in known[n]["required_columns"] if cols and c not in cols]
        if missing:
            warnings.append(f"{n} expects columns {missing}, which {dataset} does not have")
    doc, tag = _read(ctx, _ckey(dataset), {"classes": [], "history": []})
    doc.setdefault("history", []).append({"at": _now(), "by": whoami(), "from": doc.get("classes", []), "to": names})
    doc["classes"] = names
    _write(ctx, _ckey(dataset), doc, tag)
    return {"dataset": dataset, "classes": names, "role": next(iter(roles), None), "warnings": warnings}


def role_of(ctx: Context, dataset: str) -> str | None:
    known = {c["name"]: c for c in listing(ctx)}
    for n in classes_of(ctx, dataset):
        if known.get(n, {}).get("role"):
            return known[n]["role"]
    return None
