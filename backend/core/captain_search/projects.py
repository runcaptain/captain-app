"""Projects: one per company (or any line of work) inside a workspace.

A search engineer who trains models for several companies needs everything for one company in one place: its
connections, datasets, pipelines and models, backtests, investigations, and the lineage of its models. A project is
that place.

Membership is mutable organization, kept in the workspace's storage (`projects.yaml`, compare-and-swap) and never
written into the write-once records. An item belongs to a project when:
1. it was put there (`assign`), for example by importing into the project; or
2. a model registry's brand maps to the project (a registry folder `<architecture>/<brand>/<version>`), which also
   claims the pipelines linked to those models; or
3. a dataset sits in a folder named like the project, or a pipeline, backtest or dataset name starts with
   `<project>/` or `<project>-`.

Anything unclaimed is "not in a project". Nothing is ever moved or copied to join a project.
"""
from __future__ import annotations

import datetime as dt
import re

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "projects.yaml"
KINDS = ("connection", "dataset", "pipeline", "backtest", "investigation", "registry", "model", "signal", "grading")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]
    if not s:
        raise ValueError("name the project")
    return s


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(KEY)
        doc = yaml.safe_load(body) or {}
    except NotFound:
        doc, tag = {}, None
    doc.setdefault("projects", [])
    doc.setdefault("members", {})  # "<kind>:<name>" -> project id
    doc.setdefault("brands", {})  # registry brand -> project id
    return doc, tag


def _update(ctx: Context, change) -> dict:
    for _ in range(10):
        doc, tag = _read(ctx)
        out = change(doc)
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=True).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            continue
    raise Conflict("projects kept changing while saving. Try again.")


def _get(doc: dict, pid: str) -> dict:
    p = next((p for p in doc["projects"] if p["id"] == pid), None)
    if p is None:
        raise NotFound(f"no project {pid!r}")
    return p


def create(ctx: Context, name: str, description: str = "", brands: list[str] | None = None) -> dict:
    pid = slug(name)

    def ch(doc):
        if any(p["id"] == pid for p in doc["projects"]):
            raise ValueError(f"{name} already exists. Pick another name.")
        p = {"id": pid, "name": name.strip(), "description": description, "created_by": whoami(),
             "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "archived": False}
        doc["projects"].append(p)
        for b in brands or [pid]:
            doc["brands"].setdefault(b.lower(), pid)
        return p

    return _update(ctx, ch)


def update(ctx: Context, pid: str, name: str | None = None, description: str | None = None,
           archived: bool | None = None, brands: list[str] | None = None) -> dict:
    def ch(doc):
        p = _get(doc, pid)
        if name is not None:
            p["name"] = name.strip()
        if description is not None:
            p["description"] = description
        if archived is not None:
            p["archived"] = bool(archived)
        if brands is not None:
            doc["brands"] = {b: q for b, q in doc["brands"].items() if q != pid}
            for b in brands:
                doc["brands"][b.lower()] = pid
        return p

    return _update(ctx, ch)


def assign(ctx: Context, pid: str | None, items: list[dict]) -> dict:
    """Put items ({kind, name}) in a project, or take them out with pid None."""
    for it in items:
        if it.get("kind") not in KINDS:
            raise ValueError(f"a project holds {', '.join(KINDS)}, not {it.get('kind')!r}")

    def ch(doc):
        if pid is not None:
            _get(doc, pid)
        for it in items:
            key = f"{it['kind']}:{it['name']}"
            if pid is None:
                doc["members"][key] = None  # explicit "not in a project", beats the name rules
            else:
                doc["members"][key] = pid
        return {"project": pid, "items": items}

    return _update(ctx, ch)


# ---- who belongs where ----------------------------------------------------------------------


class Resolver:
    """Answers "which project is this in?" for one read of projects.yaml."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.doc, _ = _read(ctx)
        self.ids = [p["id"] for p in self.doc["projects"] if not p.get("archived")]
        self._models = None

    def _by_name(self, name: str) -> str | None:
        low = name.lower()
        for pid in sorted(self.ids, key=len, reverse=True):
            if low == pid or low.startswith((pid + "/", pid + "-", pid + "_")) or f"/{pid}/" in f"/{low}/":
                return pid
        return None

    def _model_brands(self) -> dict[str, str]:
        """Pipeline name -> registry brand, for pipelines linked to a registry model."""
        if self._models is None:
            self._models = {}
            for n in self.ctx.registry.names("pipeline"):
                try:
                    pm = self.ctx.registry.load("pipeline", n)
                except Exception:  # noqa: BLE001
                    continue
                ref = getattr(pm, "registry_ref", None) or ""
                m = re.search(r"/([^/]+)/([^/]+)/?$", ref.rstrip("/"))
                if m:
                    self._models[n] = m.group(1).lower()
        return self._models

    def of(self, kind: str, name: str, brand: str | None = None) -> str | None:
        key = f"{kind}:{name}"
        if key in self.doc["members"]:
            return self.doc["members"][key]
        if brand and brand.lower() in self.doc["brands"]:
            return self.doc["brands"][brand.lower()]
        if kind == "pipeline":
            b = self._model_brands().get(name)
            if b and b in self.doc["brands"]:
                return self.doc["brands"][b]
        if kind == "dataset":
            from . import library as LIB

            ldoc, _ = LIB._read(self.ctx)
            folder = LIB.folder_of(ldoc, name)
            hit = self._by_name(folder) if folder else None
            if hit:
                return hit
        return self._by_name(name)


def listing(ctx: Context) -> list[dict]:
    r = Resolver(ctx)
    counts = {p["id"]: {k: 0 for k in KINDS} | {"models": 0} for p in r.doc["projects"]}
    for kind, names in _all(ctx).items():
        for n in names:
            pid = r.of(kind, n)
            if pid in counts:
                counts[pid][kind] += 1
    for m in _models(ctx):
        pid = r.of("model", m["id"], brand=m.get("brand"))
        if pid in counts:
            counts[pid]["models"] += 1
    brands = {}
    for b, pid in r.doc["brands"].items():
        brands.setdefault(pid, []).append(b)
    return [{**p, "brands": sorted(brands.get(p["id"], [])), "counts": counts[p["id"]]} for p in r.doc["projects"]]


def _all(ctx: Context) -> dict[str, list[str]]:
    from . import investigations as INV

    out = {"connection": ctx.connections(), "dataset": ctx.registry.names("dataset"),
           "pipeline": ctx.registry.names("pipeline"), "backtest": ctx.registry.names("backtest")}
    try:
        out["investigation"] = [i["id"] for i in INV.listing(ctx)]
    except Exception:  # noqa: BLE001
        out["investigation"] = []
    return out


def _models(ctx: Context) -> list[dict]:
    from . import models as MD

    try:
        return MD.listing(ctx)
    except Exception:  # noqa: BLE001
        return []


def contents(ctx: Context, pid: str) -> dict:
    """Everything in one project, by kind."""
    r = Resolver(ctx)
    p = _get(r.doc, pid)
    out = {k: [] for k in KINDS}
    for kind, names in _all(ctx).items():
        out[kind] = [n for n in names if r.of(kind, n) == pid]
    out["models"] = [m for m in _models(ctx) if r.of("model", m["id"], brand=m.get("brand")) == pid]
    return {"project": p, **out}


def member_filter(ctx: Context, pid: str | None, kind: str):
    """A predicate name -> bool for list routes' ?project= filter. pid "none" keeps unclaimed items."""
    if not pid:
        return lambda name, **kw: True
    r = Resolver(ctx)
    want = None if pid == "none" else pid
    return lambda name, **kw: r.of(kind, name, **kw) == want


def lineage(ctx: Context, pid: str) -> dict:
    """The project's model lineage: its registry models, plus the pipelines linked to them and the backtests that
    compared those pipelines."""
    from . import models as MD

    full = MD.lineage(ctx)
    r = Resolver(ctx)
    keep = set()
    for n in full["nodes"]:
        if n["kind"] in ("model", "model-unregistered", "demo") and r.of("model", n["id"], brand=n.get("brand")) == pid:
            keep.add(n["id"])
    for e in full["edges"]:
        if e["relation"] == "built_on" and e["src"] in keep:
            keep.add(e["dst"])
    nodes = [n for n in full["nodes"] if n["id"] in keep]
    edges = [e for e in full["edges"] if e["src"] in keep and e["dst"] in keep]
    for name in ctx.registry.names("pipeline"):
        if r.of("pipeline", name) != pid:
            continue
        nid = f"pipeline:{name}"
        nodes.append({"id": nid, "kind": "pipeline", "label": name})
        b = r._model_brands().get(name)
        try:
            ref = ctx.registry.load("pipeline", name).registry_ref or ""
        except Exception:  # noqa: BLE001
            ref = ""
        m = re.search(r"/([^/]+)/([^/]+)/?$", ref.rstrip("/"))
        if b and m and f"model:{b}@{m.group(2)}" in keep:
            edges.append({"src": f"model:{b}@{m.group(2)}", "dst": nid, "relation": "serves"})
    for name in ctx.registry.names("backtest"):
        try:
            bt = ctx.registry.load("backtest", name)
        except Exception:  # noqa: BLE001
            continue
        arms = [a.split("@")[0] for a in bt.arms]
        if r.of("backtest", name) == pid or any(r.of("pipeline", a) == pid for a in arms):
            bid = f"backtest:{name}"
            nodes.append({"id": bid, "kind": "backtest", "label": name})
            edges += [{"src": f"pipeline:{a}", "dst": bid, "relation": "compared_in"} for a in arms
                      if any(n["id"] == f"pipeline:{a}" for n in nodes)]
    return {"project": pid, "nodes": nodes, "edges": edges}
