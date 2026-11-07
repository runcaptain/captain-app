"""`cst serve`: the local daemon (and the remote runner). Implements docs/api.md."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import threading
import traceback
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import __version__ as VERSION
from . import index as idx
from . import notebook as NB
from . import ops
from . import skills as SK
from .data import datasets as ds
from .model.entities import DatasetKind, Provenance
from .storage.backend import NotFound
from .workspace import home, open_context, whoami

TOKEN = os.environ.get("CST_SERVE_TOKEN") or secrets.token_urlsafe(24)
app = FastAPI(title="Captain Search Tools", version=VERSION)

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

# The desktop web view (tauri://localhost) and the Vite dev server call the daemon cross-origin.
# Every request still needs the bearer token.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"], allow_headers=["*"],
)
JOBS: dict[str, dict] = {}
KERNELS: dict[str, Any] = {}
_ctx_lock = threading.Lock()
_ctx = None


import contextvars as _cv

# Per-request workspace: a Captain space bound to another workspace sends X-CST-Workspace (or ?workspace=), so one
# daemon serves every workspace and switching spaces switches what the screens read.
_req_ws: "_cv.ContextVar[str | None]" = _cv.ContextVar("cst_workspace", default=None)
_ctxs: dict = {}


@app.middleware("http")
async def _workspace_from_request(request: Request, call_next):
    name = request.headers.get("x-cst-workspace") or request.query_params.get("workspace")
    tok = _req_ws.set(name or None)
    try:
        return await call_next(request)
    finally:
        _req_ws.reset(tok)


def ctx():
    global _ctx
    name = _req_ws.get()
    with _ctx_lock:
        if _ctx is None:
            _ctx = open_context()
        if not name or name == _ctx.workspace.name:
            return _ctx
        if name not in _ctxs:
            try:
                _ctxs[name] = open_context(name)
            except RuntimeError as e:  # unknown workspace: a 400 with the known names, not a crash
                raise ValueError(str(e)) from None
        return _ctxs[name]


def auth(authorization: str = Header(default="")):
    if not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "missing or wrong token")


async def _err(request: Request, exc: Exception):
    from .storage.backend import Conflict

    from .storage.backend import AlreadyExists

    code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, (Conflict, AlreadyExists)) else 400
    if isinstance(exc, AlreadyExists):  # the raw key ("compute/x.yaml") is not a sentence; name what exists
        key = str(exc).strip("'\"")
        thing, _, rest = key.partition("/")
        name = rest.rsplit(".", 1)[0] if rest else key
        return JSONResponse({"error": f"{name} already exists in {thing or 'this workspace'}. Pick another name."}, status_code=409)
    return JSONResponse({"error": str(exc).strip("'\"")}, status_code=code)


from .connectors.guard import WriteRefused  # noqa: E402
from .connectors.sql import TooLarge  # noqa: E402
from .llm import LLMUnavailable  # noqa: E402

from .storage.backend import AlreadyExists as _AlreadyExists  # noqa: E402
from .storage.backend import Conflict as _Conflict  # noqa: E402

for _cls in (NotFound, ValueError, KeyError, WriteRefused, TooLarge, LLMUnavailable, NotImplementedError, _Conflict,
             _AlreadyExists):
    app.add_exception_handler(_cls, _err)


@app.exception_handler(HTTPException)
async def _http(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


A = [Depends(auth)]

# ---- workspace --------------------------------------------------------------------------


@app.get("/api/health", dependencies=A)
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/workspace", dependencies=A)
def workspace():
    c = ctx()
    return {"name": c.workspace.name, "org": c.workspace.org, "storage_uri": c.workspace.storage_uri, "user": whoami()}


@app.get("/api/lineage", dependencies=A)
def lineage(view: str = "artifacts"):
    if view == "models":
        from . import models as MD

        return MD.lineage(ctx())
    return ops.lineage(ctx())


# ---- data -------------------------------------------------------------------------------


@app.get("/api/connections", dependencies=A)
def connections():
    import yaml

    c = ctx()
    out = []
    for n in c.connections():
        raw = yaml.safe_load(c.store.get(c.conn_key(n)))
        if not raw.get("deleted"):
            out.append({"name": n, "engine": raw["engine"], "network": raw["network"]["mode"], "writable": raw["writable"]})
    return out


@app.get("/api/extensions", dependencies=A)
def extensions_list():
    """Every engine, simulator, judge and metric this workspace can use, where each comes from, what failed to load,
    and the team's captain/ folder with its git commit."""
    from . import extensions as X

    reg = X.load(ctx().workspace.name, fresh=True)
    return {"extensions": [e.public() for e in reg.items], "errors": reg.errors, "project": reg.project}


@app.get("/api/connections/kinds", dependencies=A)
def connection_kinds():
    """Every database Captain can connect to, with the fields its form needs and whether its driver is installed."""
    from .connectors import kinds as K

    return K.listing()


@app.post("/api/connections", dependencies=A)
def connection_add(body: dict):
    """Add a connection from a kind and its fields. Secret fields are not accepted here: store each one first with
    POST /api/secrets under the names this returns (or that the kind's form computes), so no password travels with
    the connection."""
    from .connectors import kinds as K
    from .model.entities import Connection, Network
    from .storage.backend import AlreadyExists
    from .storage.registry import check_name
    from .workspace import whoami

    c = ctx()
    name = check_name(str(body.get("name", "")).strip())
    kind = K.BY_KIND.get(body.get("kind", ""))
    if kind is None:
        raise ValueError(f"unknown database kind {body.get('kind')!r}")
    vals = dict(body.get("fields") or {})
    for f in kind["fields"]:
        if f["type"] == "secret" and f["name"] in vals:
            raise ValueError(f"{f['label']} is a secret: store it with POST /api/secrets as {K.secret_name(name, f['name'])}, not here")
    built = K.build(kind["kind"], name, vals)
    conn = Connection(org=c.workspace.org, workspace=c.workspace.name, created_by=whoami(), name=name,
                      engine=built["engine"], url=built["url"], writable=bool(body.get("writable")),
                      network=Network(mode="direct"))
    try:
        c.save_connection(conn, replace=bool(body.get("replace")))
    except AlreadyExists:
        raise ValueError(f"{name} is already connected. Pick another name.") from None
    return {"name": name, "kind": kind["kind"], "engine": built["engine"], "secrets": built["secrets"],
            "driver_installed": next(k["installed"] for k in K.listing() if k["kind"] == kind["kind"])}


@app.post("/api/connections/{name}/test", dependencies=A)
def connection_test(name: str):
    """Run select 1 and say plainly whether the connection works."""
    from .connectors.sql import run_sql

    c = ctx()
    conn = c.load_connection(name)
    try:
        r = run_sql(conn, "select 1 as ok", limit=1)
    except ModuleNotFoundError as e:
        return {"name": name, "ok": False, "error": f"this database's driver isn't installed ({e.name})"}
    except KeyError as e:
        return {"name": name, "ok": False, "error": str(e).strip("'\"")}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "ok": False, "error": str(e).splitlines()[0][:300]}
    return {"name": name, "ok": True, "ms": r.elapsed_ms}


@app.get("/api/tables", dependencies=A)
def tables_overview():
    """Each connection, and what's been read of its tables."""
    from . import metastore as MS

    return MS.overview(ctx())


@app.get("/api/tables/search", dependencies=A)
def tables_search(q: str, conn: str | None = None, limit: int = 50):
    from . import metastore as MS

    return MS.search(ctx(), q, conn, limit)


@app.post("/api/tables/{conn}/pull", dependencies=A)
def tables_pull(conn: str, body: dict | None = None):
    """Read the connection's schemas, tables and columns into the workspace's cache. Runs as a job."""
    from . import metastore as MS

    c = ctx()
    c.load_connection(conn)
    sch = (body or {}).get("schemas")
    return {"job": _job(lambda p: MS.pull(c, conn, sch, progress=p), f"read the tables of {conn}")}


@app.get("/api/tables/{conn}", dependencies=A)
def tables_of(conn: str):
    from . import metastore as MS

    return MS.schemas(ctx(), conn)


@app.get("/api/tables/{conn}/{schema}/{table}", dependencies=A)
def table_detail(conn: str, schema: str, table: str):
    """Columns, comment, when it was read, and insights: frequent users, tables queried with it, example queries."""
    from . import metastore as MS

    return MS.table(ctx(), conn, schema, table)


@app.get("/api/tables/{conn}/{schema}/{table}/sample", dependencies=A)
def table_sample(conn: str, schema: str, table: str, n: int = 20, where: str | None = None,
                 order: str | None = None, desc: bool = False):
    from . import metastore as MS

    return MS.sample(ctx(), conn, schema, table, n, where, order, desc)


@app.post("/api/sql", dependencies=A)
def sql(body: dict):
    from .connectors.sql import run_sql

    c = ctx()
    conn = c.load_connection(body["conn"])
    r = run_sql(conn, body["sql"], limit=body.get("limit"))
    from .metastore import record_query

    record_query(c, body["conn"], body["sql"], rows=r.table.num_rows, ms=r.elapsed_ms, source="app")
    out = {"columns": [{"name": f.name, "type": str(f.type)} for f in r.table.schema],
           "rows": [list(map(_jsonable, row.values())) for row in r.table.slice(0, 1000).to_pylist()],
           "row_count": r.table.num_rows, "truncated": r.truncated, "ms": r.elapsed_ms}
    if body.get("save"):
        m = ds.snapshot(c, body["save"], r.table, kind=DatasetKind(body.get("kind", "generic")),
                        provenance=Provenance(source="sql", connection=body["conn"], sql=body["sql"]),
                        description=body.get("description", ""), truncated=r.truncated)
        out["dataset"] = ops.ref(m)
    return out


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


@app.get("/api/datasets", dependencies=A)
def datasets(kind: str | None = None, cls: str | None = Query(None, alias="class")):
    from . import classes as CL

    c = ctx()
    out = []
    for n in c.registry.names("dataset"):
        m = c.registry.load("dataset", n)
        if kind and m.dataset_kind.value != kind:
            continue
        mine = CL.classes_of(c, n)
        if cls and cls not in mine:
            continue
        out.append({"name": n, "latest": m.version, "kind": m.dataset_kind.value, "classes": mine, "rows": m.row_count,
                    "by": m.created_by, "created": m.created_at.isoformat(), "description": m.description})
    return out


@app.get("/api/dataset-classes", dependencies=A)
def dataset_classes():
    from . import classes as CL

    return {"classes": CL.listing(ctx()), "roles": CL.ROLES}


@app.post("/api/dataset-classes", dependencies=A)
def dataset_class_add(body: dict):
    from . import classes as CL

    return CL.upsert(ctx(), body, create=True)


@app.put("/api/dataset-classes/{name}", dependencies=A)
def dataset_class_edit(name: str, body: dict):
    from . import classes as CL

    return CL.upsert(ctx(), {**body, "name": name}, create=False)


@app.delete("/api/dataset-classes/{name}", dependencies=A)
def dataset_class_delete(name: str):
    from . import classes as CL

    CL.delete(ctx(), name)
    return {"ok": True}


@app.get("/api/datasets/tree", dependencies=A)
def dataset_tree(cls: str | None = Query(None, alias="class")):
    """The workspace as folders, the way the person organized it (see library.py)."""
    from . import classes as CL
    from . import library as LIB

    c = ctx()

    def entry(n):
        if cls and cls not in CL.classes_of(c, n):
            return None
        m = c.registry.load("dataset", n)
        return {"name": n, "leaf": n.split("/")[-1], "latest": m.version, "kind": m.dataset_kind.value,
                "classes": CL.classes_of(c, n), "rows": m.row_count, "created": m.created_at.isoformat(),
                "description": m.description}

    return LIB.tree(c, entry)


@app.post("/api/library/folders", dependencies=A)
def library_folder_make(body: dict):
    from . import library as LIB

    return {"folder": LIB.make_folder(ctx(), body.get("path", ""))}


@app.delete("/api/library/folders", dependencies=A)
def library_folder_remove(path: str):
    from . import library as LIB

    return LIB.remove_folder(ctx(), path)


@app.post("/api/library/move", dependencies=A)
def library_move(body: dict):
    """Move datasets into a folder ("" is the top). Copies nothing and changes no versions."""
    from . import library as LIB

    return LIB.move(ctx(), list(body.get("names") or []), body.get("folder", ""))


@app.post("/api/library/rename", dependencies=A)
def library_rename(body: dict):
    from . import library as LIB

    return LIB.rename_folder(ctx(), body.get("from", ""), body.get("to", ""))


@app.post("/api/datasets/import", dependencies=A)
def datasets_import(body: dict):
    """Files or folders dropped into the app: {paths: [...], folder}. Runs as a job, whose result lists what was
    imported, updated, unchanged and skipped (with reasons)."""
    from .data import importer as IM

    c = ctx()
    paths = [str(p) for p in body.get("paths") or []]
    if not paths:
        raise ValueError("drop at least one file or folder")
    return {"job": _job(lambda p: IM.import_paths(c, paths, body.get("folder", ""), progress=p),
                        f"import {len(paths)} item(s)")}


@app.post("/api/storage/check", dependencies=A)
def storage_check(body: dict):
    """Can Captain read and write this bucket or folder with this cloud login? Plain-words answer."""
    from . import attach as AT

    return AT.check_storage(str(body.get("uri", "")).strip(), body.get("profile") or None)


@app.get("/api/registries", dependencies=A)
def registries_list():
    from . import attach as AT

    return AT.registries(ctx())


@app.post("/api/registries", dependencies=A)
def registries_attach(body: dict):
    """Attach a model registry (s3://bucket[/prefix] in the <architecture>/<brand>/<version>/ layout) and read it.
    Runs as a job. Attaching the same address again syncs it."""
    from . import attach as AT

    c = ctx()
    uri = str(body.get("uri", "")).strip()
    if not uri:
        raise ValueError("name the registry's address, like s3://bucket/models")
    return {"job": _job(lambda p: AT.attach(c, uri, body.get("profile") or None, body.get("label"), progress=p),
                        f"attach {uri}")}


@app.delete("/api/registries", dependencies=A)
def registries_detach(uri: str):
    from . import attach as AT

    return AT.detach(ctx(), uri)


@app.put("/api/datasets/{name:path}/classes", dependencies=A)
def dataset_classify(name: str, body: dict):
    from . import classes as CL

    return CL.classify(ctx(), name, body.get("classes") or [])


@app.get("/api/datasets/{name:path}/rows", dependencies=A)
def dataset_rows(name: str, v: int | None = None, file: str | None = None, offset: int = 0, limit: int = 100,
                 order: str | None = None, desc: bool = False, search: str | None = None, where: str | None = None,
                 count: bool = False):
    from . import browse

    c = ctx()
    m = c.registry.load("dataset", name, v)
    from .workspace import load_config

    profile = (load_config().get("workspaces", {}).get(c.workspace.name) or {}).get("profile")
    return browse.rows(c, m, file, offset, limit, order, desc, search, where, count, profile)


@app.get("/api/datasets/{name:path}/head", dependencies=A)
def dataset_head(name: str, v: int | None = None, n: int = 50):
    c = ctx()
    m = c.registry.load("dataset", name, v)
    if m.external and not m.files:
        from . import browse

        r = browse.rows(c, m, limit=n)
        return {"columns": r["columns"], "rows": r["rows"], "external": True, "file": r["file"]}
    t = ds.load_table(c, m).slice(0, n)
    return {"columns": [{"name": f.name, "type": str(f.type)} for f in t.schema],
            "rows": [list(map(_jsonable, r.values())) for r in t.to_pylist()]}


@app.get("/api/datasets/{name:path}", dependencies=A)
def dataset(name: str, v: int | None = None):
    from . import classes as CL

    m = ctx().registry.load("dataset", name, v)
    out = m.model_dump(mode="json")
    out["classes"] = CL.classes_of(ctx(), name)
    out["role"] = CL.role_of(ctx(), name)
    if m.external and not m.schema_fields:
        try:
            from . import browse

            r = browse.rows(ctx(), m, limit=1)
            out["sample_schema"] = r["columns"]
            out["table_files"] = r["files"]
        except Exception as e:  # noqa: BLE001
            out["sample_schema_error"] = str(e)
    return out


# ---- pipelines, backtests, deploys ------------------------------------------------------


@app.get("/api/pipelines", dependencies=A)
def pipelines():
    c = ctx()
    out = []
    for n in c.registry.names("pipeline"):
        m = c.registry.load("pipeline", n)
        out.append({"name": n, "latest": m.version, "engine": m.engine, "repro_status": m.repro_status.value,
                    "by": m.created_by, "created": m.created_at.isoformat(), "description": m.description})
    return out


@app.get("/api/pipelines/{name:path}/v/{version}/cost", dependencies=A)
def pipeline_cost_get(name: str, version: int):
    from . import costs as COST

    c = ctx()
    out = COST.get(c, f"{name}@v{version}")
    row = next((r for r in COST.ledger(c) if r["pipeline"] == out["pipeline"]), None)
    return {**out, "roi": row["roi"] if row else None}


@app.put("/api/pipelines/{name:path}/v/{version}/cost", dependencies=A)
def pipeline_cost_put(name: str, version: int, body: dict):
    from . import costs as COST

    return COST.set_cost(ctx(), f"{name}@v{version}", {k: v for k, v in body.items() if k in COST.FIELDS})


@app.get("/api/costs", dependencies=A)
def cost_ledger():
    from . import costs as COST

    return COST.ledger(ctx())


@app.get("/api/cost-settings", dependencies=A)
def cost_settings_get():
    from . import costs as COST

    return COST.settings(ctx())


@app.put("/api/cost-settings", dependencies=A)
def cost_settings_put(body: dict):
    from . import costs as COST

    return COST.set_settings(ctx(), body.get("queries_per_day"))


@app.get("/api/proposals", dependencies=A)
def proposals_list(status: str | None = None):
    from . import proposals as PR

    return PR.listing(ctx(), status)


@app.get("/api/proposals/{pid}", dependencies=A)
def proposal_get(pid: str):
    from . import proposals as PR

    return PR.get(ctx(), pid)


@app.post("/api/proposals/{pid}/review", dependencies=A)
def proposal_review(pid: str, body: dict):
    from . import proposals as PR

    return PR.review(ctx(), pid, body.get("decision", ""), body.get("note", ""))


@app.post("/api/proposals/{pid}/run", dependencies=A)
def proposal_run(pid: str):
    from . import proposals as PR

    c = ctx()
    PR.get(c, pid)
    return {"job": _job(lambda p: f"ran {PR.run(c, pid, progress=p)['run']}", f"running proposal {pid}")}


@app.get("/api/grading", dependencies=A)
def grading_list():
    from . import grading as GR

    return GR.listing(ctx())


@app.post("/api/grading", dependencies=A)
def grading_create(body: dict):
    """Start a grading queue: the products these pipelines return for these queries, for people to grade."""
    from . import grading as GR

    pipes = body.get("pipelines") or []
    if isinstance(pipes, str):
        pipes = [p.strip() for p in pipes.split(",") if p.strip()]
    if not pipes:
        raise ValueError("pick at least one pipeline whose results people will grade")
    return GR.create(ctx(), body.get("name", ""), body.get("queries", ""), pipes, int(body.get("depth") or 10),
                     int(body["sample"]) if body.get("sample") else None, float(body.get("overlap", 0.1)))


@app.get("/api/grading/{name}", dependencies=A)
def grading_status(name: str):
    from . import grading as GR

    return GR.status(ctx(), name)


@app.get("/api/grading/{name}/next", dependencies=A)
def grading_next(name: str):
    from . import grading as GR

    return GR.next_pair(ctx(), name)


@app.post("/api/grading/{name}/grade", dependencies=A)
def grading_grade(name: str, body: dict):
    from . import grading as GR

    return GR.grade(ctx(), name, body.get("pair", ""), body.get("grade", -1), body.get("note", ""))


@app.post("/api/grading/{name}/publish", dependencies=A)
def grading_publish(name: str, body: dict):
    from . import grading as GR

    m = GR.publish(ctx(), name, body.get("into", ""))
    return m.model_dump(mode="json")


@app.get("/api/labels", dependencies=A)
def labels_list():
    c = ctx()
    return [{**c.registry.load("label", n).model_dump(mode="json"), "versions": c.registry.versions("label", n)}
            for n in c.registry.names("label")]


@app.get("/api/labels/{name:path}/preview", dependencies=A)
def label_preview(name: str, logs: str, v: int | None = None, limit: int = 20):
    from . import labels as LB

    return LB.preview(ctx(), f"{name}@v{v}" if v else name, logs, limit)


@app.get("/api/replays", dependencies=A)
def replays_list():
    c = ctx()
    return [c.registry.load("replay", n).model_dump(mode="json") for n in c.registry.names("replay")]


@app.get("/api/bench", dependencies=A)
def bench_list():
    from . import bench as BE

    return BE.listing(ctx())


@app.get("/api/handoffs", dependencies=A)
def handoffs_list(pipeline: str | None = None):
    from . import handoff as HO

    return HO.listing(ctx(), pipeline)


@app.get("/api/handoffs/{hid}", dependencies=A)
def handoff_get(hid: str):
    from . import handoff as HO

    return HO.get(ctx(), hid)


@app.post("/api/handoffs/{hid}/link", dependencies=A)
def handoff_link(hid: str, body: dict):
    from . import handoff as HO

    return HO.link(ctx(), hid, body.get("system", ""), body.get("deployment_id", ""), body.get("url"), bool(body.get("live", True)))


@app.post("/api/handoffs/{hid}/outcome", dependencies=A)
def handoff_outcome(hid: str, body: dict):
    from . import handoff as HO

    return HO.outcome(ctx(), hid, body.get("metric", ""), body.get("value", 0), body.get("source", ""), body.get("experiment"),
                      body.get("ci_low"), body.get("ci_high"))


@app.get("/api/investigations", dependencies=A)
def investigations_list(state: str | None = None, q: str | None = None):
    from . import investigations as INV

    return INV.listing(ctx(), state, q)


@app.post("/api/investigations", dependencies=A)
def investigation_create(body: dict):
    from . import investigations as INV

    plan = {k: body.get(k) for k in INV.PLAN_FIELDS if k in body}
    return INV.create(ctx(), body.get("title", ""), body.get("hypothesis", ""), body.get("agent"), **plan)


@app.get("/api/investigations/{iid}", dependencies=A)
def investigation_get(iid: str):
    from . import investigations as INV

    return INV.get(ctx(), iid)


@app.post("/api/investigations/{iid}/revise", dependencies=A)
def investigation_revise(iid: str, body: dict):
    from . import investigations as INV

    plan = {k: body.get(k) for k in INV.PLAN_FIELDS if k in body}
    return INV.revise(ctx(), iid, body.get("hypothesis"), body.get("note", ""), body.get("agent"), body.get("expected_revision"), **plan)


@app.post("/api/investigations/{iid}/evidence", dependencies=A)
def investigation_evidence(iid: str, body: dict):
    from . import investigations as INV

    return INV.add_evidence(ctx(), iid, body.get("kind", ""), body.get("ref", ""), body.get("note", ""), body.get("agent"))


@app.post("/api/investigations/{iid}/run", dependencies=A)
def investigation_run(iid: str, body: dict | None = None):
    from . import investigations as INV

    c, b = ctx(), body or {}
    INV.get(c, iid)
    return {"job": _job(lambda p: f"ran {INV.run(c, iid, b.get('agent'), b.get('idempotency_key'), progress=p)['id']}",
                        f"running investigation {iid}")}


@app.post("/api/investigations/{iid}/decide", dependencies=A)
def investigation_decide(iid: str, body: dict):
    from . import investigations as INV

    return INV.decide(ctx(), iid, body.get("outcome", ""), body.get("interpretation", ""), body.get("limitations") or [],
                      body.get("next_step", ""), body.get("runs"), body.get("slices"), body.get("claim", "none"))


@app.get("/api/investigations/{iid}/export", dependencies=A)
def investigation_export(iid: str):
    from . import investigations as INV

    return INV.export(ctx(), iid)


@app.get("/api/investigations/{iid}/prompt", dependencies=A)
def investigation_prompt(iid: str, ask: str = "investigate"):
    from . import investigations as INV

    return {"prompt": INV.prompt(ctx(), iid, ask)}


@app.get("/api/decisions/{did}", dependencies=A)
def decision_get(did: str):
    from . import investigations as INV

    return INV.decision_get(ctx(), did)


@app.get("/api/signals", dependencies=A)
def signals_list():
    from . import investigations as INV

    return INV.signals(ctx())


@app.post("/api/signals", dependencies=A)
def signal_create(body: dict):
    from . import investigations as INV

    return INV.signal_create(ctx(), body.get("name", ""), body.get("source", ""), body.get("definition", ""),
                             body.get("rationale", ""), body.get("available_at", ""), body.get("investigation"), body.get("agent"))


@app.post("/api/signals/{sid}/check", dependencies=A)
def signal_check(sid: str, body: dict):
    from . import investigations as INV

    return INV.signal_check(ctx(), sid, body.get("what", ""), body.get("status", ""), body.get("note", ""), body.get("agent"))


@app.get("/api/execution-policy", dependencies=A)
def execution_policy():
    from . import investigations as INV

    return INV.policy(ctx())


@app.get("/api/onboarding", dependencies=A)
def onboarding_status():
    from . import onboarding as OB

    return OB.status(ctx())


@app.get("/api/monitor/{env}", dependencies=A)
def monitor_show(env: str):
    from . import monitor as MON

    return MON.show(ctx(), env)


@app.get("/api/pipelines/{name:path}", dependencies=A)
def pipeline(name: str, v: int | None = None):
    return ctx().registry.load("pipeline", name, v).model_dump(mode="json")


@app.get("/api/backtests", dependencies=A)
def backtests():
    c = ctx()
    out = []
    for n in c.registry.names("backtest"):
        m = c.registry.load("backtest", n)
        out.append({"name": n, "latest": m.version, "arms": m.arms, "primary_metric": m.preregistration.primary_metric,
                    "runs": len(c.registry.versions("backtest-run", n))})
    return out


@app.get("/api/backtests/{name:path}", dependencies=A)
def backtest(name: str):
    c = ctx()
    m = c.registry.load("backtest", name).model_dump(mode="json")
    runs = []
    for v in c.registry.versions("backtest-run", name):
        r = c.registry.load("backtest-run", name, v)
        runs.append({"version": v, "created": r.created_at.isoformat(), "complete": r.complete, "independent": r.independent})
    m["runs"] = runs
    return m


def _job(fn, label: str) -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "status": "queued", "progress": 0.0, "message": label, "result": None, "log": [label], "compute": "local"}

    def go():
        JOBS[jid]["status"] = "running"
        try:
            res = fn(lambda f: JOBS[jid].__setitem__("progress", round(float(f), 3)))
            JOBS[jid].update(status="done", progress=1.0, result=res, message="done")
            JOBS[jid]["log"].append(f"done: {res}")
        except Exception as e:  # noqa: BLE001
            JOBS[jid].update(status="failed", message=str(e))
            JOBS[jid]["log"].append(f"failed: {e}")

    threading.Thread(target=go, daemon=True).start()
    return jid


@app.post("/api/backtests/{name:path}/run", dependencies=A)
def backtest_run(name: str, body: dict | None = None):
    c = ctx()
    bt = c.registry.load("backtest", name)
    compute = (body or {}).get("compute") or bt.compute or "local"

    def go(p):
        r = ops.backtest_launch(c, name, compute, progress=p)
        return r.get("run") or r

    jid = _job(go, f"running {name} on {compute}")
    JOBS[jid]["compute"] = compute
    return {"job": jid}


@app.get("/api/jobs/{jid}", dependencies=A)
def job(jid: str):
    """A job started by this daemon (progress of a local run), or else a recorded compute job (jobs.py)."""
    if jid in JOBS:
        return JOBS[jid]
    from . import jobs as J

    return J.show(ctx(), jid)


@app.get("/api/runs/{name:path}/queries/{qid}", dependencies=A)
def run_query_side_by_side(name: str, qid: str, a: str | None = None, b: str | None = None, k: int = 20):
    from . import compare as CP

    return CP.query(ctx(), name, qid, a, b, k)


@app.get("/api/runs/{name:path}/queries", dependencies=A)
def run_queries(name: str, a: str | None = None, b: str | None = None, sort: str = "delta", desc: bool = False,
                offset: int = 0, limit: int = 100, cls: str | None = Query(None, alias="class"), q: str | None = None,
                only: str | None = None):
    from . import compare as CP

    return CP.queries(ctx(), name, a, b, sort, desc, offset, limit, cls, q, only)


@app.get("/api/runs/{name:path}/compatibility", dependencies=A)
def run_compatibility(name: str):
    from . import compare as CP

    return CP.compatibility(ctx(), name)


@app.get("/api/runs/{name:path}/histogram", dependencies=A)
def run_histogram(name: str, a: str | None = None, b: str | None = None, bins: int = 21):
    from . import compare as CP

    return CP.histogram(ctx(), name, a, b, bins)


@app.get("/api/compare-runs", dependencies=A)
def compare_runs(a: str, b: str):
    from . import compare as CP

    return CP.compare_runs(ctx(), a, b)


@app.get("/api/runs/{name:path}", dependencies=A)
def run(name: str, v: int | None = None):
    c = ctx()
    r = c.registry.load("backtest-run", name, v).model_dump(mode="json")
    r["report"] = ops.backtest_report(c.registry.load("backtest-run", name, v), c.registry.load("backtest", name))
    return r


@app.get("/api/environments", dependencies=A)
def environments():
    return ops.env_list(ctx())


@app.post("/api/environments", dependencies=A)
def environment_add(body: dict):
    return ops.env_create(ctx(), body["name"], body.get("tier", "demo"), body.get("target") or {"type": "record"},
                          body.get("gates"), body.get("compute") or None).model_dump(mode="json")


@app.get("/api/deployments", dependencies=A)
def deployments(env: str | None = None):
    c = ctx()
    envs = [env] if env else [e["name"] for e in ops.env_list(c)]
    out = [d.model_dump(mode="json") for e in envs for d in ops.history(c, e)]
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


@app.post("/api/deploy/promote", dependencies=A)
def promote(body: dict):
    try:
        d = ops.promote(ctx(), body["pipeline"], body["env"], body.get("override_reason") or None, bool(body.get("confirm")),
                        dry_run=bool(body.get("dry_run")))
    except ops.GateFailed as e:
        return JSONResponse({"error": str(e), "gate_results": e.results}, status_code=409)
    return d.model_dump(mode="json")


@app.post("/api/deploy/rollback", dependencies=A)
def rollback(body: dict):
    try:
        d = ops.rollback(ctx(), body["env"], bool(body.get("confirm")))
    except ops.GateFailed as e:
        return JSONResponse({"error": str(e), "gate_results": e.results}, status_code=409)
    return d.model_dump(mode="json")


# ---- notebooks ----------------------------------------------------------------------------


@app.get("/api/notebooks", dependencies=A)
def notebooks():
    return NB.listing(ctx())


@app.get("/api/notebooks/{name:path}/export", dependencies=A)
def notebook_export(name: str, format: str = "ipynb"):
    from .notebook import formats as F

    _, nb, _ = NB.load(ctx(), name)
    ext, text, mime = F.from_ipynb(nb, format)
    return {"filename": name.split("/")[-1] + ext, "content": text, "mime": mime}


@app.get("/api/notebooks/{name:path}", dependencies=A)
def notebook(name: str):
    try:
        v, nb, draft = NB.load(ctx(), name)
    except NotFound:
        v, nb, draft = None, NB.new_notebook(), True
    return {"name": name, "version": v, "notebook": nb, "draft": draft}


@app.put("/api/notebooks/{name:path}/draft", dependencies=A)
def notebook_draft(name: str, body: dict):
    NB.save_draft(ctx(), name, body["notebook"])
    return {"ok": True}


@app.post("/api/notebooks/{name:path}/save", dependencies=A)
def notebook_save(name: str, body: dict):
    m = NB.save_revision(ctx(), name, body["notebook"], body.get("message", ""))
    return {"version": m.version}


@app.post("/api/notebooks/{name:path}/import", dependencies=A)
def notebook_import(name: str, body: dict):
    """Body: {notebook: <.ipynb JSON>} or {filename, content} in any supported format."""
    warnings, fmt = [], "ipynb"
    if body.get("notebook") is not None:
        nb = body["notebook"]
    else:
        from .notebook import formats as F

        nb, fmt, warnings = F.to_ipynb(body["filename"], body["content"])
    m = NB.save_revision(ctx(), name, nb, body.get("message") or f"imported ({fmt})")
    return {"name": m.name, "version": m.version, "format": fmt, "warnings": warnings}


@app.post("/api/kernels", dependencies=A)
def kernel_start(body: dict):
    sid = uuid.uuid4().hex[:12]
    KERNELS[sid] = NB.Kernel(ctx())
    return {"session": sid}


def _ws_ok(ws: WebSocket) -> bool:
    return secrets.compare_digest(ws.query_params.get("token", ""), TOKEN)


@app.websocket("/api/kernels/{sid}")
async def kernel_ws(ws: WebSocket, sid: str):
    if not _ws_ok(ws) or sid not in KERNELS:
        await ws.close(code=4401)
        return
    await ws.accept()
    k = KERNELS[sid]
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    lock = threading.Lock()

    def run(msg):
        with lock:
            try:
                n = int(msg.get("n") or 0)
                cell = {"cell_type": "code", "source": msg["code"],
                        "metadata": {"captain": {"kind": msg.get("kind", "python"), "conn": msg.get("conn"),
                                                 "var": msg.get("var"), "chart": msg.get("chart"),
                                                 "limit": msg.get("limit")}}}
                code = NB.cell_code(cell, n or 1)
                for ev in k.execute(msg["cell_id"], code):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "cell_id": msg.get("cell_id"), "ename": type(e).__name__, "evalue": str(e), "traceback": []})
                loop.call_soon_threadsafe(q.put_nowait, {"type": "done", "cell_id": msg.get("cell_id"), "ms": 0})

    async def pump():
        while True:
            await ws.send_json(await q.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "execute":
                threading.Thread(target=run, args=(msg,), daemon=True).start()
            elif msg.get("type") == "interrupt":
                k.interrupt()
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()


@app.delete("/api/kernels/{sid}", dependencies=A)
def kernel_stop(sid: str):
    k = KERNELS.pop(sid, None)
    if k:
        k.shutdown()
    return {"ok": True}


# ---- agent ------------------------------------------------------------------------------


@app.get("/api/skills", dependencies=A)
def skills():
    return SK.listing(ctx())


# ---- v2: coding agent, cloud, workspaces, compute, building backtests, registry ---------


@app.get("/api/coding-agent", dependencies=A)
def coding_agent():
    from . import codeagent

    return codeagent.prepare(ctx())


@app.get("/api/cloud/providers", dependencies=A)
def cloud_providers():
    from . import cloud

    return cloud.PROVIDERS


@app.get("/api/secrets", dependencies=A)
def secrets_list():
    """Names only. Values never leave the keychain through this API."""
    from .workspace import load_config

    return {"names": load_config().get("secret_names", [])}


@app.post("/api/secrets", dependencies=A)
def secret_set(body: dict):
    """Store a secret in the OS keychain in-process, so a password typed into the app never reaches a command line or
    a process list. The value is never echoed, logged or returned."""
    import re as _re

    from .workspace import set_secret

    name, value = str(body.get("name", "")).strip(), body.get("value")
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
        raise ValueError("a secret name is letters, digits and underscores, starting with a letter (for example DB_PASSWORD)")
    if not isinstance(value, str) or not value:
        raise ValueError("the secret's value is empty")
    set_secret(name, value)
    return {"name": name, "stored": True}


@app.delete("/api/secrets/{name}", dependencies=A)
def secret_delete(name: str):
    from .workspace import delete_secret

    delete_secret(name)
    return {"name": name, "deleted": True}


@app.get("/api/cloud/summary", dependencies=A)
def cloud_summary():
    from . import cloud

    return cloud.summary(ctx())


@app.get("/api/cloud/profiles", dependencies=A)
def cloud_profiles():
    from . import cloud

    return [{k: v for k, v in p.items() if k != "secret_fields"} | {"secret_fields_set": p.get("secret_fields", [])}
            for p in cloud.profiles()]


@app.post("/api/cloud/profiles", dependencies=A)
def cloud_add(body: dict):
    from . import cloud

    p = cloud.add_profile(body["name"], body["provider"], body["auth"], body.get("region"), body.get("fields") or {},
                          body.get("secrets") or {})
    return {k: v for k, v in p.items() if k != "secret_fields"}


@app.post("/api/cloud/profiles/{name}/test", dependencies=A)
def cloud_test(name: str):
    from . import cloud

    return cloud.test_profile(name)


@app.delete("/api/cloud/profiles/{name}", dependencies=A)
def cloud_delete(name: str):
    from . import cloud

    cloud.delete_profile(name)
    return {"ok": True}


@app.get("/api/workspaces", dependencies=A)
def workspaces():
    from . import cloud
    from .workspace import load_config

    cfg = load_config()
    cur = ctx().workspace.name
    return [{"name": n, "storage_uri": w["storage"], "provider": cloud.provider_of(w["storage"]),
             "profile": w.get("profile"), "current": n == cur} for n, w in sorted(cfg.get("workspaces", {}).items())]


@app.post("/api/workspaces", dependencies=A)
def workspace_add(body: dict):
    from .workspace import init_workspace

    c = init_workspace(body["name"], body["storage_uri"], body.get("org", "captain"), body.get("profile") or None)
    return {"name": c.workspace.name, "storage_uri": c.workspace.storage_uri}


@app.post("/api/workspaces/{name}/use", dependencies=A)
def workspace_use(name: str):
    global _ctx
    from .workspace import load_config, save_config

    cfg = load_config()
    if name not in cfg.get("workspaces", {}):
        raise ValueError(f"unknown workspace {name}")
    with _ctx_lock:
        _ctx = open_context(name)
    os.environ["CST_WORKSPACE"] = name
    cfg["current"] = name
    save_config(cfg)
    return {"current": name}


@app.get("/api/compute/kinds", dependencies=A)
def compute_kinds():
    from . import compute as C

    return C.kinds()


@app.get("/api/compute", dependencies=A)
def compute_list():
    from . import compute as C

    return C.listing(ctx())


@app.post("/api/compute", dependencies=A)
def compute_add(body: dict):
    from . import compute as C

    return C.add(ctx(), body["name"], body["kind"], body.get("profile") or None, body.get("region") or None, body.get("config") or {})


@app.post("/api/compute/{name}/test", dependencies=A)
def compute_test(name: str):
    from . import compute as C

    return C.test(ctx(), name)


@app.get("/api/jobs", dependencies=A)
def jobs_list(refresh: bool = False, limit: int = 200):
    """Jobs submitted to compute targets, newest first. refresh=true asks each unfinished job's provider first."""
    from . import jobs as J

    return J.listing(ctx(), refresh_active=refresh, limit=limit)


@app.get("/api/jobs/{job_id}/logs", dependencies=A)
def jobs_logs(job_id: str, since: str | None = None):
    from . import jobs as J

    return J.logs(ctx(), job_id, since)


@app.post("/api/jobs/{job_id}/cancel", dependencies=A)
def jobs_cancel(job_id: str):
    from . import jobs as J

    return J.cancel(ctx(), job_id)


@app.get("/api/engines", dependencies=A)
def engines():
    labels = {"local-bm25": "Local BM25 (in process)", "http": "Any HTTP search service", "elastic": "Elasticsearch / OpenSearch",
              "typesense": "Typesense", "algolia": "Algolia", "captain": "Captain /v1/rank"}
    return [{"engine": e, "label": labels[e], "template": t, "needs_catalog": e == "local-bm25"} for e, t in ops.TEMPLATE.items()]


@app.post("/api/pipelines", dependencies=A)
def pipeline_publish(body: dict):
    return ops.pipeline_publish(ctx(), body).model_dump(mode="json")


@app.post("/api/queries/generate", dependencies=A)
def queries_generate(body: dict):
    from .eval import queries as Q

    return ops.queries_gen(ctx(), body["catalog"], body["name"], body.get("classes") or Q.CLASSES, int(body.get("n", 500)),
                           int(body.get("seed", 7))).model_dump(mode="json")


@app.post("/api/queries/seal", dependencies=A)
def queries_seal(body: dict):
    return ops.queries_seal(ctx(), body["ref"], float(body.get("holdout", 0.3))).model_dump(mode="json")


@app.post("/api/judgments/run", dependencies=A)
def judgments_run(body: dict):
    c = ctx()
    return {"job": _job(lambda p: ops.ref(ops.judge_run(c, body["queries"], body["pipelines"], body.get("judge", "heuristic"),
                                                        body["into"], int(body.get("depth", 10)), progress=p)),
                        f"judging into {body['into']}")}


@app.post("/api/judgments/from-logs", dependencies=A)
def judgments_from_logs(body: dict):
    from . import historic as H

    return H.judgments_from_logs(ctx(), body["logs"], body["into"], int(body.get("min_impressions", 3))).model_dump(mode="json")


@app.get("/api/populations", dependencies=A)
def populations():
    c = ctx()
    out = []
    for n in c.registry.names("population"):
        m = c.registry.load("population", n)
        out.append({"name": n, "latest": m.version, "simulator": m.simulator, "size": m.size, "seed": m.seed,
                    "from_logs": m.from_logs, "click_model_judgments": m.click_model_judgments})
    return out


@app.post("/api/populations", dependencies=A)
def population_add(body: dict):
    return ops.population_create(ctx(), body["name"], body.get("simulator", "cascade-v1"), int(body.get("size", 4000)),
                                 int(body.get("seed", 1)), body.get("click_judgments") or None,
                                 from_logs=body.get("from_logs") or None).model_dump(mode="json")


@app.post("/api/backtests", dependencies=A)
def backtest_add(body: dict):
    return ops.backtest_plan(ctx(), body["name"], body["arms"], body.get("queries") or None, body.get("judgments") or None,
                             body.get("population") or None, body["hypothesis"], body.get("primary", "ndcg@10"),
                             float(body.get("mde", 0.003)), mode=body.get("mode", "simulated"), logs=body.get("logs") or None,
                             compute=body.get("compute") or None, surfaces=body.get("surfaces") or None).model_dump(mode="json")


@app.post("/api/registry/import", dependencies=A)
def registry_import(body: dict):
    from . import models as MD

    c = ctx()
    return {"job": _job(lambda p: (lambda s: f"{s.name}@v{s.version}")(MD.import_registry(c, body.get("uri") or None,
                                                                                         body.get("profile") or None)),
                        "importing the model registry")}


@app.get("/api/models", dependencies=A)
def models_list():
    from . import models as MD

    return MD.listing(ctx())


@app.get("/api/models/{mid:path}", dependencies=A)
def model_detail(mid: str):
    from . import models as MD

    return MD.detail(ctx(), mid)


@app.get("/api/notebook-formats", dependencies=A)
def notebook_formats():
    from .notebook import formats as F

    return F.FORMATS


@app.get("/api/commands", dependencies=A)
def commands_list():
    from . import commands as CMD

    return CMD.tree()


@app.post("/api/commands/run", dependencies=A)
def commands_run(body: dict):
    from . import commands as CMD

    return CMD.run(body["path"], body.get("args") or {}, workspace=ctx().workspace.name, dry_run=bool(body.get("dry_run")))


@app.get("/api/workflows", dependencies=A)
def workflows_list():
    from . import workflows as WF

    return WF.listing(ctx())


@app.get("/api/workflows/templates", dependencies=A)
def workflow_templates():
    from . import workflows as WF

    return [{"id": k, **v} for k, v in WF.TEMPLATES.items()]


@app.post("/api/workflows", dependencies=A)
def workflow_save(body: dict):
    from . import workflows as WF

    w = WF.save(ctx(), body["name"], body.get("steps") or [], body.get("params") or {}, body.get("schedule") or None,
                body.get("description", ""))
    return w.model_dump(mode="json")


@app.post("/api/workflows/{name:path}/run", dependencies=A)
def workflow_run(name: str, body: dict | None = None):
    from . import workflows as WF

    c = ctx()
    c.registry.load("workflow", name)
    holder = {}

    def go(p):
        def step(res):
            JOBS[holder["id"]]["log"].append(f"{res['id']}: {res['status']} ({res.get('command') or res.get('reason') or ''}){' ' + res['error'] if res.get('error') else ''}")
        r = WF.run(c, name, params=(body or {}).get("params"), on_step=step)
        return f"{r.name}@v{r.version} {r.status}"

    holder["id"] = _job(go, f"running workflow {name}")
    return {"job": holder["id"]}


@app.get("/api/workflows/{name:path}/runs", dependencies=A)
def workflow_runs(name: str):
    c = ctx()
    return [c.registry.load("workflow-run", name, v).model_dump(mode="json") for v in reversed(c.registry.versions("workflow-run", name))]


@app.get("/api/workflows/{name:path}", dependencies=A)
def workflow_get(name: str, v: int | None = None):
    from . import workflows as WF

    w = ctx().registry.load("workflow", name, v)
    return {**w.model_dump(mode="json"), "yaml": WF.to_yaml(w)}


# ---- entry --------------------------------------------------------------------------------


def ui_dir(explicit: str | None = None) -> str | None:
    """The built React app (app/dist) to serve at /, so a web view anywhere (the Captain fork of Herdr, a browser)
    can load the screens from the daemon itself: http://127.0.0.1:<port>/?embedded=1&screen=explore#token=<token>.
    The token stays in the URL fragment, which browsers never send to the server."""
    from pathlib import Path

    for cand in (explicit, os.environ.get("CST_UI_DIR"), str(Path(__file__).resolve().parents[2] / "app" / "dist")):
        if cand and os.path.isfile(os.path.join(cand, "index.html")):
            return cand
    return None


def mount_ui(path: str | None) -> str | None:
    from fastapi.staticfiles import StaticFiles

    d = ui_dir(path)
    if d and not any(getattr(r, "name", None) == "ui" for r in app.routes):
        app.mount("/", StaticFiles(directory=d, html=True), name="ui")  # added last, so every /api route wins
    return d


def serve(host: str = "127.0.0.1", port: int = 0, token: str | None = None, ui: str | None = None):
    import uvicorn

    global TOKEN
    if token:
        TOKEN = token
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("CST_ALLOW_REMOTE"):
        raise SystemExit("binding beyond localhost exposes SQL on your databases. Use a TLS proxy, then set CST_ALLOW_REMOTE=1.")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    port = sock.getsockname()[1]
    d = mount_ui(ui)
    info = {"port": port, "token": TOKEN, "pid": os.getpid(), "ui": f"http://127.0.0.1:{port}/" if d else None}
    os.makedirs(home(), exist_ok=True)
    p = os.path.join(home(), "serve.json")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(info, fh)
    print(json.dumps(info), flush=True)
    from .workflows import start_scheduler

    start_scheduler(ctx)
    config = uvicorn.Config(app, log_level="warning", ws_ping_interval=30)
    server = uvicorn.Server(config)
    import signal as _signal

    def _stop(*_):  # graceful on SIGTERM and SIGINT, so the cleanup below always runs
        server.should_exit = True

    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _signal.signal(sig, _stop)
        except ValueError:
            pass  # not the main thread (tests): uvicorn's own handling applies
    try:
        server.run(sockets=[sock])
    finally:
        for k in list(KERNELS.values()):
            try:
                k.shutdown()
            except Exception:
                pass
        # A stopped daemon must not leave its address behind: readers would trust a dead port.
        try:
            with open(p) as fh:
                if json.load(fh).get("pid") == os.getpid():
                    os.remove(p)
        except (OSError, ValueError):
            pass
