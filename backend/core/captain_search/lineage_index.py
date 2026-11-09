"""The lineage index: one write-once edge record per lineage edge, written when a version is published.

Before this, lineage was rebuilt on every request by loading every manifest in the bucket (ops.lineage). Now
Registry.publish calls `record` right after a version's MANIFEST is claimed, and readers list one prefix.

  lineage/edges/<ulid>.json   {"from": node id or null, "to": node id, "relation": ..., "at": ISO time,
                               "labels": {node id: label}}   write-once
  lineage/index.yaml          {"complete": true, ...}         says the index covers every version in the bucket

A version with no sources still gets one record (from null, relation "published"), so the index also answers
"which versions exist" without listing the registry. That is what project stages read.

A workspace made before the index has no `lineage/index.yaml`. Readers then fall back to the full scan, and
`cst lineage reindex` backfills the records and writes the marker. A new workspace writes the marker at init.

Edges are derived from the manifest alone (`derive`), the same way at publish time, in the scan and in reindex,
so the three agree. A source given without a version resolves to the newest version published before the
manifest (by ULID order), never to one published later. A source that can't be found (deleted, or from another
workspace) becomes an "unknown source" node instead of an error.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading

import yaml

from .storage.backend import AlreadyExists, NotFound

EDGES = "lineage/edges/"
MARKER = "lineage/index.yaml"
GRAPH_KINDS = ("dataset", "pipeline", "backtest-run")  # kinds the lineage graph draws even without edges
_NODE_KIND = {"dataset": "dataset", "pipeline": "pipeline", "backtest-run": "run"}

_cache: dict[tuple[str, str], dict] = {}  # (store uri, key) -> record. Records are write-once, so never stale.
_lock = threading.Lock()


def node_id(kind: str, ref: str) -> str:
    return f"{_NODE_KIND.get(kind, kind)}:{ref}"


def kind_of(nid: str) -> str:
    return nid.split(":", 1)[0]


def _ref(m) -> str:
    return f"{m.name}@v{m.version}"


# ---- deriving edges from one manifest ---------------------------------------------------------


def _resolve(reg, kind: str, raw: str, before_id: str | None) -> tuple[str, str | None]:
    """A source reference as a node id. Returns (id, label or None). Unknown sources get an `unknown:` node."""
    base = raw.split(":")[0] if kind != "registry" else raw
    m = re.match(r"^(?P<name>[^@]+)(@v?(?P<ver>\d+))?$", base)
    if not m:
        return f"unknown:{raw}", f"unknown source: {raw}"
    name, ver = m.group("name"), m.group("ver")
    try:
        if ver is not None:
            reg.load(kind, name, int(ver))  # a direct get, no listing
            return node_id(kind, f"{name}@v{int(ver)}"), None
        vs = reg.versions(kind, name)
    except (NotFound, ValueError, KeyError):
        vs = []
    except Exception:  # noqa: BLE001  a malformed manifest is still "not something we can point at"
        vs = []
    if ver is None and vs:
        if before_id:
            for v in reversed(vs):
                try:
                    if reg.load(kind, name, v).id < before_id:
                        return node_id(kind, f"{name}@v{v}"), None
                except Exception:  # noqa: BLE001
                    continue
        return node_id(kind, f"{name}@v{vs[-1]}"), None
    return f"unknown:{kind}:{raw}", f"unknown source: {raw}"


def derive(reg, m) -> tuple[list[dict], dict[str, str]]:
    """The lineage edges a manifest implies, and labels for nodes that need one. Edges are {src, dst, relation}."""
    kind = m.kind
    me = node_id(kind, _ref(m))
    edges: list[dict] = []
    labels: dict[str, str] = {}

    def src(k, raw, relation, before=m.id):
        nid, lab = _resolve(reg, k, raw, before)
        if lab:
            labels[nid] = lab
        edges.append({"src": nid, "dst": me, "relation": relation})

    if kind == "dataset":
        p = m.provenance
        if p.connection:
            edges.append({"src": f"connection:{p.connection}", "dst": me, "relation": "produced_by"})
        params = p.params or {}
        for key in ("catalog", "queries", "logs", "judgments", "refresh_of"):
            if isinstance(params.get(key), str) and params[key]:
                src("dataset", params[key], "derived_from")
        if isinstance(params.get("label"), str) and params["label"]:
            src("label", params["label"], "derived_from")
        for pl in params.get("pipelines") or []:
            if isinstance(pl, str) and pl:
                src("pipeline", pl, "candidates_from")
        if isinstance(params.get("grading_queue"), str) and params["grading_queue"]:
            edges.append({"src": f"grading:{params['grading_queue']}", "dst": me, "relation": "graded_in"})
        if m.parent and m.holdout_seal:
            edges.append({"src": node_id("dataset", f"{m.name}@v{m.parent}"), "dst": me, "relation": "derived_from"})
    elif kind == "pipeline":
        if m.catalog:
            src("dataset", m.catalog, "derived_from")
        tags = m.tags or {}
        for par in filter(None, tags.get("parents", "").split(",")):
            nid, _ = _resolve(reg, "pipeline", par, m.id)
            if nid.startswith("unknown:"):
                nid = f"model-unregistered:{par}"
            edges.append({"src": nid, "dst": me, "relation": "parent"})
        source = getattr(m, "source", None) or {}
        kind_src = source.get("kind") if isinstance(source, dict) else None
        retrained = tags.get("retrained_from") or ""
        if retrained:
            src("pipeline", retrained, "retrained_from")
        for par in getattr(m, "parents", None) or []:  # full refs or outside ids, from the trainer contract
            if not isinstance(par, str) or not par or par == retrained:
                continue
            if ":" in par:  # an outside id: never looked up in the workspace
                nid = _outside(par)
                if nid.startswith("unknown:"):
                    labels[nid] = f"unknown source: {par}"
                edges.append({"src": nid, "dst": me,
                              "relation": "imported_from" if kind_src in ("mlflow", "registry") else "parent"})
            else:
                src("pipeline", par, "retrained_from" if kind_src == "retrain" else "parent")
        if m.registry_ref:
            edges.append({"src": f"registry:{m.registry_ref}", "dst": me, "relation": "produced_by"})
        for r in _training_refs(m):
            src("dataset", r, "trained_on")
    elif kind == "backtest-run":
        try:
            bname, _, bver = m.backtest.partition("@v")
            bt = reg.load("backtest", bname, int(bver) if bver else None)
        except Exception:  # noqa: BLE001
            bt = None
        for a in m.arms:
            if "@v" in a.arm:
                src("pipeline", a.arm, "evaluated_on")
            else:  # a baseline named in an imported readout: one node per run, not shared across brands
                nid = f"baseline:{m.name}:{a.arm}"
                labels[nid] = f"{a.arm} ({((bt.tags if bt else None) or {}).get('brand', m.name)})"
                edges.append({"src": nid, "dst": me, "relation": "evaluated_on"})
        if bt is not None:
            if bt.judgments:
                src("dataset", bt.judgments, "evaluated_on")
            if bt.mode != "imported" and bt.queries:
                src("dataset", bt.queries, "evaluated_on")
            if bt.logs:
                src("dataset", bt.logs, "evaluated_on")
    return edges, labels


# Outside ids in `parents` that other lineage views already draw keep a matching node id, so the graphs join:
# MLflow's view uses mlflow-model:<name>@<ver> and mlflow-run:<id>, the models view uses model:<brand>@<ver>.
_OUTSIDE = {"mlflow-model": "mlflow-model", "mlflow-run": "mlflow-run", "registry": "model"}


def _outside(par: str) -> str:
    prefix, _, rest = par.partition(":")
    return f"{_OUTSIDE[prefix]}:{rest}" if prefix in _OUTSIDE and rest else f"unknown:{par}"


def _training_refs(m) -> list[str]:
    """Dataset refs a trained pipeline version says it trained on: `train_inputs`, a list of {ref, kind, role, uri,
    label_source, as_of} written by the trainer contract. Older and imported versions have none."""
    out = []
    for it in getattr(m, "train_inputs", None) or []:
        r = it.get("ref") if isinstance(it, dict) else getattr(it, "ref", None)
        if isinstance(r, str) and "@v" in r:
            out.append(r)
    return out


# ---- writing ----------------------------------------------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _write(store, rec: dict) -> None:
    from .model.entities import new_id

    store.put_new(f"{EDGES}{new_id()}.json", json.dumps(rec, sort_keys=True).encode())


def records_for(reg, m) -> list[dict]:
    edges, labels = derive(reg, m)
    me = node_id(m.kind, _ref(m))
    at = m.created_at.isoformat() if getattr(m, "created_at", None) else _now()
    recs = [{"from": e["src"], "to": e["dst"], "relation": e["relation"], "at": at,
             **({"labels": {k: v for k, v in labels.items() if k == e["src"]}} if e["src"] in labels else {})}
            for e in edges]
    return recs or [{"from": None, "to": me, "relation": "published", "at": at}]


def record(reg, m) -> int:
    """Append the edge records for a just-published version. Called by Registry.publish. Never raises: a publish
    stands whether or not its lineage was recorded, and `cst lineage reindex` fills any gap."""
    try:
        recs = records_for(reg, m)
        for r in recs:
            _write(reg.store, r)
        return len(recs)
    except Exception:  # noqa: BLE001
        return 0


def mark_complete(store, how: str) -> None:
    body = yaml.safe_dump({"complete": True, "since": _now(), "how": how}).encode()
    try:
        store.put_new(MARKER, body)
    except AlreadyExists:
        pass


def is_complete(store) -> bool:
    try:
        return bool((yaml.safe_load(store.get(MARKER)) or {}).get("complete"))
    except NotFound:
        return False


# ---- reading ----------------------------------------------------------------------------------


def read(store) -> list[dict]:
    """Every edge record, oldest first. One listing of the edges prefix; records already read are cached."""
    keys = store.list(EDGES)
    out = []
    for k in keys:
        if not k.endswith(".json"):
            continue
        ck = (store.uri, k)
        with _lock:
            rec = _cache.get(ck)
        if rec is None:
            try:
                rec = json.loads(store.get(k))
            except (NotFound, json.JSONDecodeError):
                continue
            with _lock:
                _cache[ck] = rec
        out.append(rec)
    return out


def graph_from(records: list[dict]) -> dict:
    """Nodes and edges for the lineage graph, from edge records."""
    nodes: dict[str, dict] = {}
    edges, seen = [], set()

    def node(nid, label=None):
        if nid not in nodes:
            k = kind_of(nid)
            nodes[nid] = {"id": nid, "kind": k, "label": label or (nid.split(":", 1)[1] if ":" in nid else nid)}
            if k == "unknown":
                nodes[nid]["label"] = label or f"unknown source: {nid.split(':', 1)[1]}"
        elif label and nodes[nid]["label"] != label and kind_of(nid) != "unknown":
            nodes[nid]["label"] = label

    for r in records:
        labels = r.get("labels") or {}
        if kind_of(r["to"]) in ("dataset", "pipeline", "run"):
            node(r["to"])
        if r.get("from") is None:
            continue
        if kind_of(r["to"]) not in ("dataset", "pipeline", "run"):
            node(r["to"])
        node(r["from"], labels.get(r["from"]))
        k = (r["from"], r["to"], r["relation"])
        if k not in seen:
            seen.add(k)
            edges.append({"src": r["from"], "dst": r["to"], "relation": r["relation"]})
    return {"nodes": list(nodes.values()), "edges": edges}


def scan(reg, kinds=GRAPH_KINDS) -> list[dict]:
    """Edge records for every version in the bucket, without writing anything: the fallback and reindex source."""
    out = []
    for kind in kinds:
        for n in reg.names(kind):
            for v in reg.versions(kind, n):
                try:
                    m = reg.load(kind, n, v)
                except Exception:  # noqa: BLE001
                    continue
                out += records_for(reg, m)
    return out


def reindex(ctx, dry_run: bool = False) -> dict:
    """Backfill edge records for every version the index doesn't have yet, then mark the index complete."""
    from .model.entities import KIND_DIRS

    have = {(r.get("from"), r["to"], r["relation"]) for r in read(ctx.store)}
    have_to = {r["to"] for r in read(ctx.store)}
    todo = []
    for r in scan(ctx.registry, kinds=tuple(KIND_DIRS)):
        if r.get("from") is None and r["to"] in have_to:
            continue
        k = (r.get("from"), r["to"], r["relation"])
        if k in have:
            continue
        have.add(k)
        have_to.add(r["to"])
        todo.append(r)
    if not dry_run:
        for r in todo:
            _write(ctx.store, r)
        mark_complete(ctx.store, "reindex")
    return {"added": len(todo), "existing": len(have) - len(todo), "dry_run": dry_run,
            "complete": True if not dry_run else is_complete(ctx.store)}


# ---- questions the index answers without listing the registry -----------------------------------


class Index:
    """One read of the edge records, with the lookups project stages need."""

    def __init__(self, store):
        self.records = read(store)
        self.complete = is_complete(store)
        self.latest: dict[tuple[str, str], int] = {}  # (node kind, name) -> newest version seen
        self.sources: dict[str, list[dict]] = {}
        for r in self.records:
            for nid in (r["to"], r.get("from")):
                if not nid:
                    continue
                m = re.match(r"^([a-z-]+):(.+)@v(\d+)$", nid)
                if m:
                    key = (m.group(1), m.group(2))
                    self.latest[key] = max(self.latest.get(key, 0), int(m.group(3)))
            if r.get("from"):
                self.sources.setdefault(r["to"], []).append(r)

    def newest(self, kind: str, name: str) -> int | None:
        return self.latest.get((_NODE_KIND.get(kind, kind), name))

    def upstream(self, nid: str) -> list[dict]:
        return self.sources.get(nid, [])

    def runs_of(self, backtest: str) -> list[int]:
        pfx = f"run:{backtest}@v"
        return sorted({int(r["to"][len(pfx):]) for r in self.records if r["to"].startswith(pfx)})
