"""The scope model, engines and the Explorer tree (spec: "Scope model, terminology, and navigation defaults").

- **Scope types** are configurable. The defaults are Brand and Site: a Brand can contain Sites, and both can sit at the
  workspace root. Each type has a stable key and editable terminology (singular, plural, description), which drive
  labels such as "Add brand". Hierarchy (allowed parents) is validated for cycles and for existing instances.
- **Scopes** are instances (Acme Outdoors, US storefront), each with one type and an optional parent.
- **Engines** are persistent identities for a search or recommendation system. An engine survives model, pipeline
  and configuration changes. It links to any number of scopes and owns the pipelines (and their versions) that
  implement it, so its history is its lineage.
- **Explorer** projects all of this as a tree: All engines, All experiments, the scope tree with engines under their
  scopes and Projects. An engine linked to two scopes shows in both places with the same id. The tree is
  navigation only: it is not a copy and not an authorization mechanism.

The model and its instances live in the workspace's storage (`scope-model.yaml`, `scopes.yaml`, compare-and-swap).
The model carries a revision number, so a run can record which configuration it was resolved under, and renaming a
label never changes an id.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

MODEL_KEY = "scope-model.yaml"
DATA_KEY = "scopes.yaml"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

DEFAULT_MODEL = {
    "revision": 1,
    # Empty until the team defines its own levels (Edgar, 2026-11-07: "the scope model should be empty by default").
    # SUGGESTED is offered in Settings as a one-click start.
    "types": [],
    "explorer": {"order": [], "hidden": []},
    "engine_types": ["search", "recommendation"],
}


SUGGESTED = [
    {"key": "brand", "singular": "Brand", "plural": "Brands", "description": "A business or company you build search for.",
     "parents": [], "fields": []},
    {"key": "site", "singular": "Site", "plural": "Sites",
     "description": "A website or app. An identity, not just a hostname.", "parents": ["brand"], "fields": []},
]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _get(ctx: Context, key: str, default: dict) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(key)
        return (yaml.safe_load(body) or {}), tag
    except NotFound:
        return yaml.safe_load(yaml.safe_dump(default)), None


def _cas(ctx: Context, key: str, default: dict, change):
    for _ in range(10):
        doc, tag = _get(ctx, key, default)
        out = change(doc)
        try:
            ctx.store.put_if(key, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            continue
    raise Conflict(f"{key} kept changing while saving. Try again.")


# ---- the model --------------------------------------------------------------------------------


def model(ctx: Context) -> dict:
    return _get(ctx, MODEL_KEY, DEFAULT_MODEL)[0]


def _data(ctx: Context) -> dict:
    d = _get(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}})[0]
    d.setdefault("scopes", [])
    d.setdefault("engines", [])
    d.setdefault("links", {})  # "<kind>:<name>" -> {"scope": id} or {"engine": id}
    return d


def validate_model(new: dict, scopes: list[dict]) -> list[str]:
    """Problems with a proposed model, in words. Empty means valid."""
    errs = []
    types = new.get("types") or []
    keys = [t.get("key") for t in types]
    for k in keys:
        if not isinstance(k, str) or not KEY_RE.match(k):
            errs.append(f"{k!r} can't be a type key: lowercase letters, digits and underscores, starting with a letter")
    if len(set(keys)) != len(keys):
        errs.append("two scope types share a key")
    for t in types:
        for f in ("singular", "plural"):
            if not str(t.get(f) or "").strip():
                errs.append(f"{t.get('key')} needs a {f} name")
        for p in t.get("parents") or []:
            if p not in keys:
                errs.append(f"{t['singular']} can't sit inside {p!r}: no such type")
    # cycles in allowed parents
    graph = {t["key"]: set(t.get("parents") or []) for t in types if isinstance(t.get("key"), str)}

    def cyc(k, seen):
        if k in seen:
            return True
        return any(cyc(p, seen | {k}) for p in graph.get(k, ()))

    if any(cyc(k, set()) for k in graph):
        errs.append("the hierarchy has a loop: a type can't end up inside itself")
    # existing instances must still fit
    by_id = {s["id"]: s for s in scopes}
    for s in scopes:
        if s.get("archived"):
            continue
        if s["type"] not in graph:
            errs.append(f"{s['name']} is a {s['type']}, which this change removes. Archive or move it first.")
        elif s.get("parent") and by_id.get(s["parent"], {}).get("type") not in graph.get(s["type"], set()):
            errs.append(f"{s['name']} would no longer be allowed inside {by_id[s['parent']]['name']}")
    order = (new.get("explorer") or {}).get("order") or []
    for k in order:
        if k not in graph:
            errs.append(f"the Explorer order names {k!r}, which isn't a type")
    return errs


def set_model(ctx: Context, new: dict, expect_revision: int | None = None) -> dict:
    """Replace the scope model. Refused, with the reasons, if invalid. The last valid model stays."""
    scopes = _data(ctx)["scopes"]
    errs = validate_model(new, scopes)
    if errs:
        raise ValueError(" ".join(errs))

    def ch(doc):
        if expect_revision is not None and doc.get("revision") != expect_revision:
            raise Conflict(f"the scope model changed (now revision {doc.get('revision')}). Reload and try again.")
        rev = int(doc.get("revision") or 1) + 1
        doc.clear()
        doc.update({**new, "revision": rev, "updated_by": whoami(), "updated_at": _now()})
        return doc

    return _cas(ctx, MODEL_KEY, DEFAULT_MODEL, ch)


# ---- scopes and engines ---------------------------------------------------------------------


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "x"


def add_scope(ctx: Context, type_key: str, name: str, parent: str | None = None, fields: dict | None = None) -> dict:
    m = model(ctx)
    t = next((t for t in m["types"] if t["key"] == type_key), None)
    if t is None:
        if not m["types"]:
            raise ValueError("There are no scope levels yet. Add one in Settings, Project Structure (for example Brand), "
                             "then add scopes to it.")
        raise ValueError(f"no scope type {type_key!r}. Types: {', '.join(x['key'] for x in m['types'])}")
    if not name.strip():
        raise ValueError(f"name the {t['singular'].lower()}")

    def ch(doc):
        if parent:
            p = next((s for s in doc["scopes"] if s["id"] == parent), None)
            if p is None:
                raise NotFound(f"no scope {parent!r}")
            if p["type"] not in (t.get("parents") or []):
                raise ValueError(f"a {t['singular'].lower()} can't sit inside a {p['type']}")
        elif t.get("parents") and type_key not in (m.get("root_types") or [type_key]):
            pass  # every type may sit at the root (spec: Brand and Site can both exist at the workspace root)
        s = {"id": f"{type_key}-{_slug(name)}-{uuid.uuid4().hex[:4]}", "type": type_key, "name": name.strip(),
             "parent": parent, "fields": fields or {}, "archived": False, "created_by": whoami(), "created_at": _now()}
        doc.setdefault("scopes", []).append(s)
        return s

    return _cas(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}}, ch)


def update_scope(ctx: Context, sid: str, name: str | None = None, parent: str | None | bool = False,
                 archived: bool | None = None, fields: dict | None = None) -> dict:
    """parent=False leaves it; None moves to the root; an id moves under that scope (validated)."""
    m = model(ctx)

    def ch(doc):
        s = next((x for x in doc["scopes"] if x["id"] == sid), None)
        if s is None:
            raise NotFound(f"no scope {sid!r}")
        if name is not None:
            s["name"] = name.strip()
        if parent is not False:
            if parent is not None:
                p = next((x for x in doc["scopes"] if x["id"] == parent), None)
                t = next(t for t in m["types"] if t["key"] == s["type"])
                if p is None or p["type"] not in (t.get("parents") or []):
                    raise ValueError(f"{s['name']} can't move there")
                anc, seen = p, set()
                while anc:
                    if anc["id"] == sid or anc["id"] in seen:
                        raise ValueError(f"{s['name']} can't move inside itself")
                    seen.add(anc["id"])
                    anc = next((x for x in doc["scopes"] if x["id"] == anc.get("parent")), None)
            s["parent"] = parent
        if archived is not None:
            s["archived"] = bool(archived)
        if fields is not None:
            s["fields"] = fields
        return s

    return _cas(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}}, ch)


def add_engine(ctx: Context, name: str, type_: str = "search", scopes: list[str] | None = None,
               pipelines: list[str] | None = None, description: str = "") -> dict:
    m = model(ctx)
    if type_ not in m.get("engine_types", DEFAULT_MODEL["engine_types"]):
        raise ValueError(f"an engine is one of {', '.join(m['engine_types'])}")

    def ch(doc):
        known = {s["id"] for s in doc["scopes"]}
        bad = [s for s in scopes or [] if s not in known]
        if bad:
            raise NotFound(f"no scope {', '.join(bad)}")
        e = {"id": f"engine-{_slug(name)}-{uuid.uuid4().hex[:4]}", "name": name.strip(), "type": type_,
             "scopes": list(scopes or []), "pipelines": list(pipelines or []), "description": description,
             "archived": False, "created_by": whoami(), "created_at": _now()}
        doc.setdefault("engines", []).append(e)
        return e

    return _cas(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}}, ch)


def update_engine(ctx: Context, eid: str, **changes) -> dict:
    def ch(doc):
        e = next((x for x in doc["engines"] if x["id"] == eid), None)
        if e is None:
            raise NotFound(f"no engine {eid!r}")
        known = {s["id"] for s in doc["scopes"]}
        for k, v in changes.items():
            if v is None or k not in ("name", "type", "scopes", "pipelines", "description", "archived"):
                continue
            if k == "scopes" and any(s not in known for s in v):
                raise NotFound("one of those scopes doesn't exist")
            e[k] = v
        return e

    return _cas(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}}, ch)


def link(ctx: Context, items: list[dict], scope: str | None = None, engine: str | None = None) -> dict:
    """Attach records ({kind, name}: dataset, connection, investigation, backtest, model, registry) to a scope or an
    engine. Pipelines belong to engines through the engine's own list. An unknown scope or engine is refused."""
    kinds = ("dataset", "connection", "investigation", "backtest", "model", "registry", "pipeline", "signal", "grading")
    for it in items:
        if it.get("kind") not in kinds or not it.get("name"):
            raise ValueError(f"each item needs a kind ({', '.join(kinds)}) and a name")
    if scope and engine:
        raise ValueError("link to a scope or an engine, not both")

    def ch(doc):
        if scope and not any(s["id"] == scope and not s.get("archived") for s in doc["scopes"]):
            raise ValueError(f"no scope {scope!r}. Scope ids look like brand-acme-1a2b: GET /api/explorer lists them.")
        if engine and not any(e["id"] == engine and not e.get("archived") for e in doc["engines"]):
            raise ValueError(f"no engine {engine!r}. GET /api/engines lists them.")
        for it in items:
            key = f"{it['kind']}:{it['name']}"
            if scope is None and engine is None:
                doc["links"].pop(key, None)
            else:
                doc["links"][key] = {"scope": scope} if scope else {"engine": engine}
        return {"linked": items, "scope": scope, "engine": engine}

    return _cas(ctx, DATA_KEY, {"scopes": [], "engines": [], "links": {}}, ch)


# ---- resolution: which scope and engine is a record under? ---------------------------------


class Resolve:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.m = model(ctx)
        self.d = _data(ctx)
        self.scopes = {s["id"]: s for s in self.d["scopes"]}
        self.engines = {e["id"]: e for e in self.d["engines"]}
        self.by_pipeline = {p: e["id"] for e in self.d["engines"] for p in e["pipelines"]}
        # registry brand -> scope (a scope's fields.registry_brands, or a scope named like the brand)
        self.brand_scope = {}
        for s in self.d["scopes"]:
            for b in (s.get("fields") or {}).get("registry_brands", []) or []:
                self.brand_scope[b.lower()] = s["id"]
            self.brand_scope.setdefault(_slug(s["name"]), s["id"])

    def ancestors(self, sid: str | None) -> list[str]:
        out, seen = [], set()
        while sid and sid in self.scopes and sid not in seen:
            out.append(sid)
            seen.add(sid)
            sid = self.scopes[sid].get("parent")
        return out

    def targets(self, kind: str, name: str, brand: str | None = None) -> dict:
        """{engine, scopes: [scope ids incl. ancestors]} for a record."""
        eng = None
        sc: list[str] = []
        ln = self.d["links"].get(f"{kind}:{name}")
        if kind == "pipeline":
            eng = self.by_pipeline.get(name.split("@")[0])
        if ln:
            eng = ln.get("engine") or eng
            if ln.get("scope"):
                sc = [ln["scope"]]
        if eng and eng in self.engines:
            sc = sc + list(self.engines[eng]["scopes"])
        if not sc and brand and brand.lower() in self.brand_scope:
            sc = [self.brand_scope[brand.lower()]]
        full = []
        for s in sc:
            for a in self.ancestors(s):
                if a not in full:
                    full.append(a)
        return {"engine": eng, "scopes": full}

    def within(self, kind: str, name: str, scope: str | None = None, engine: str | None = None, **kw) -> bool:
        t = self.targets(kind, name, **kw)
        if engine:
            return t["engine"] == engine
        if scope:
            return scope in t["scopes"]
        return True


def scope_filter(ctx: Context, scope: str | None, engine: str | None, kind: str):
    if not scope and not engine:
        return lambda name, **kw: True
    r = Resolve(ctx)
    return lambda name, **kw: r.within(kind, name, scope, engine, **kw)


# ---- the Explorer -----------------------------------------------------------------------------


def explorer(ctx: Context) -> dict:
    from . import projects as PR

    m = model(ctx)
    d = _data(ctx)
    types = {t["key"]: t for t in m["types"]}
    hidden = set((m.get("explorer") or {}).get("hidden") or [])
    live = [s for s in d["scopes"] if not s.get("archived")]
    engines = [e for e in d["engines"] if not e.get("archived")]

    def eng_node(e):
        return {"kind": "engine", "id": e["id"], "name": e["name"], "type": e["type"], "pipelines": len(e["pipelines"])}

    def visible_parent(s):
        p = s.get("parent")
        while p:
            ps = next((x for x in live if x["id"] == p), None)
            if ps is None:
                return None
            if ps["type"] not in hidden:
                return p
            p = ps.get("parent")
        return None

    def scope_node(s):
        kids = [x for x in live if x["type"] not in hidden and visible_parent(x) == s["id"]]
        groups = {}
        for k in kids:
            groups.setdefault(k["type"], []).append(scope_node(k))
        children = [{"kind": "group", "type": k, "label": types[k]["plural"], "children": v} for k, v in groups.items()]
        # engines under the deepest visible scope they're linked to
        mine = [eng_node(e) for e in engines if s["id"] in e["scopes"]]
        if mine:
            children.append({"kind": "group", "type": "engine", "label": "Engines", "children": mine})
        return {"kind": "scope", "id": s["id"], "type": s["type"], "name": s["name"],
                "label": types.get(s["type"], {}).get("singular", s["type"]), "children": children}

    roots = [s for s in live if s["type"] not in hidden and visible_parent(s) is None]
    order = (m.get("explorer") or {}).get("order") or list(types)
    groups = []
    for k in order:
        if k in hidden:
            continue
        items = [scope_node(s) for s in roots if s["type"] == k]
        if items:
            groups.append({"kind": "group", "type": k, "label": types[k]["plural"], "children": items})
    unscoped = [eng_node(e) for e in engines if not e["scopes"]]
    tree = [{"kind": "view", "id": "all-engines", "label": "All engines"},
            {"kind": "view", "id": "all-experiments", "label": "All experiments"},
            *groups]
    if unscoped:
        tree.append({"kind": "group", "type": "engine", "label": "Engines", "children": unscoped})
    tree.append({"kind": "group", "type": "project", "label": "Projects",
                 "children": [{"kind": "project", "id": p["id"], "name": p["name"], "icon": p.get("icon")} for p in PR.listing(ctx)
                              if not p.get("archived")]})
    return {"workspace": ctx.workspace.name, "model_revision": m.get("revision", 1), "tree": tree,
            "actions": [{"type": t["key"], "label": f"Add {t['singular'].lower()}"} for t in m["types"]]
                       + [{"type": "engine", "label": "Add engine"}]}
