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


_req_scope: "_cv.ContextVar[str | None]" = _cv.ContextVar("cst_scope", default=None)


@app.middleware("http")
async def _workspace_from_request(request: Request, call_next):
    name = request.headers.get("x-cst-workspace") or request.query_params.get("workspace")
    tok = _req_ws.set(name or None)
    # The Explorer's pick travels like the workspace: X-CST-Scope: <scope|engine|project|view>:<id>
    stok = _req_scope.set(request.headers.get("x-cst-scope") or request.query_params.get("scope") or None)
    try:
        return await call_next(request)
    finally:
        _req_scope.reset(stok)
        _req_ws.reset(tok)


def _member(c, project: str | None, kind: str):
    """A name -> bool predicate for list routes: ?project= and the Explorer's X-CST-Scope, both applied."""
    from . import projects as PR
    from . import scopes as SC

    by_project = PR.member_filter(c, project, kind)
    pick = _req_scope.get() or ""
    skind, _, sid = pick.partition(":")
    if not sid or skind == "view":
        return by_project
    if skind == "project":
        in_proj = PR.member_filter(c, sid, kind)
        return lambda name, **kw: by_project(name, **kw) and in_proj(name, **kw)
    if skind in ("scope", "engine"):
        in_scope = SC.scope_filter(c, sid if skind == "scope" else None, sid if skind == "engine" else None, kind)
        return lambda name, **kw: by_project(name, **kw) and in_scope(name, **kw)
    raise ValueError(f"X-CST-Scope must be scope:, engine:, project: or view:, not {pick!r}")


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

    code = (404 if isinstance(exc, NotFound) else 409 if isinstance(exc, (Conflict, AlreadyExists))
            # Another service refused us, or couldn't be reached (MLflow, a warehouse): its sentence, not a bare 500.
            else 403 if isinstance(exc, PermissionError) else 502 if isinstance(exc, ConnectionError) else 400)
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
             _AlreadyExists, PermissionError, ConnectionError):
    app.add_exception_handler(_cls, _err)


@app.exception_handler(HTTPException)
async def _http(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


A = [Depends(auth)]

from .collaboration.routes import router as team_router
app.include_router(team_router, dependencies=A)

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
def connections(project: str | None = None):
    import yaml

    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "connection")
    out = []
    for n in c.connections():
        if not inp(n):
            continue
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


@app.get("/api/connections/{name}/suggestions", dependencies=A)
def connection_suggestions(name: str):
    """Sample queries for this source, from its tables (read them first) or, for PostHog and Google Analytics, from
    what those products record. Each is bounded by a LIMIT or a time window."""
    from . import suggest as SG

    return SG.suggestions(ctx(), name)


@app.post("/api/connections/{name}/preview", dependencies=A)
def connection_preview(name: str, body: dict):
    """{query, limit<=1000}: the rows, without saving anything. Save with POST /api/sql {conn, sql, save, kind,
    project?}."""
    from . import suggest as SG

    q = str(body.get("query", "")).strip()
    if not q:
        raise ValueError("write a query, or pick a suggested one")
    return SG.preview(ctx(), name, q, int(body.get("limit", 1000)))


@app.get("/api/lineage/index", dependencies=A)
def lineage_index_state():
    """{complete}: is every version in the lineage index? Versions made before the index existed are not, until
    they are read in once (POST /api/lineage/reindex, or `cst lineage reindex`)."""
    from . import lineage_index as LI

    return {"complete": bool(LI.Index(ctx().store).complete)}


@app.post("/api/lineage/reindex", dependencies=A)
def lineage_reindex():
    """Read every version into the lineage index. Safe to run twice. {complete, ...what it indexed}."""
    from . import lineage_index as LI

    c = ctx()
    out = LI.reindex(c)
    return {**(out if isinstance(out, dict) else {}), "complete": bool(LI.Index(c.store).complete)}


@app.get("/api/lineage/graph", dependencies=A)
def lineage_graph(node: str | None = None, up: int = 2, down: int = 2):
    """Data and model lineage. With node=<id>, only what's within `up` steps upstream and `down` downstream, each node
    with its level (negative is upstream). Without a node, the whole graph."""
    from . import lineage as LN

    c = ctx()
    if node:
        return LN.neighborhood(c, node, max(0, min(up, 10)), max(0, min(down, 10)))
    return LN.graph(c)


@app.get("/api/lineage/columns", dependencies=A)
def lineage_columns(dataset: str, column: str):
    """Which warehouse table columns one column of a SQL-made dataset came from."""
    from . import lineage as LN

    return LN.columns(ctx(), dataset, column)


@app.post("/api/lineage/import/{conn}", dependencies=A)
def lineage_import(conn: str, body: dict | None = None):
    """Read Databricks Unity Catalog's or Snowflake's own lineage into the graph. Runs as a job."""
    from . import lineage as LN

    c = ctx()
    c.load_connection(conn)
    days = int((body or {}).get("days", 30))
    return {"job": _job(lambda p: LN.import_external(c, conn, days), f"read {conn}'s lineage")}


@app.get("/api/profile", dependencies=A)
def profile(dataset: str):
    """Quality per column (null share, distinct values, min, max) and freshness, for a dataset version."""
    from . import lineage as LN

    return LN.profile_dataset(ctx(), dataset)


@app.post("/api/prepare/preview", dependencies=A)
def prepare_preview(body: dict):
    """Training-ready data, previewed: {source: {conn, table} | {conn, sql} | {dataset}, role?, mapping?, options?}.
    Returns the role guess, the column mapping (editable), what's missing, the cleaned first rows, and a report of
    every change, all from a sample. Nothing is saved."""
    from .data import prepare as PP

    return PP.preview(ctx(), dict(body.get("source") or {}), body.get("role"), body.get("mapping"), body.get("options"))


@app.post("/api/prepare/import", dependencies=A)
def prepare_import(body: dict):
    """Prepare the whole source like the preview and save it as a dataset of the role's kind. Runs as a job.
    {source, role?, mapping?, options?, name?, folder?, project?}"""
    from .data import prepare as PP

    c = ctx()
    src = dict(body.get("source") or {})
    kw = {k: body.get(k) for k in ("role", "mapping", "options", "name", "folder", "project")}
    kw["opts"] = kw.pop("options")
    return {"job": _job(lambda p: PP.import_prepared(c, src, progress=p, **kw), "prepare data for training")}


@app.get("/api/mlflow", dependencies=A)
def mlflow_status():
    """Where Captain reads MLflow from (read-only), and which model versions are linked to pipelines."""
    from . import mlflow_bridge as MF

    return MF.status(ctx())


@app.put("/api/mlflow", dependencies=A)
def mlflow_configure(body: dict):
    """{uri, token_secret?} for a tracking server, or {databricks_connection} for Databricks' managed MLflow."""
    from . import mlflow_bridge as MF

    return MF.configure(ctx(), body.get("uri"), body.get("token_secret"), body.get("databricks_connection"))


@app.get("/api/mlflow/experiments", dependencies=A)
def mlflow_experiments():
    from . import mlflow_bridge as MF

    return MF.experiments(ctx())


@app.get("/api/mlflow/experiments/{exp_id}/runs", dependencies=A)
def mlflow_runs(exp_id: str, limit: int = 100):
    from . import mlflow_bridge as MF

    return MF.runs(ctx(), exp_id, limit)


@app.get("/api/mlflow/models", dependencies=A)
def mlflow_models(include_system: bool = False):
    """Registered models. On Databricks: Unity Catalog, then the legacy workspace registry when it's on. Databricks'
    built-in models (the system catalog) are left out unless include_system=true."""
    from . import mlflow_bridge as MF

    return MF.models(ctx(), include_system=include_system)


@app.post("/api/mlflow/links", dependencies=A)
def mlflow_link(body: dict):
    """Say which Captain pipeline serves an MLflow model version: {model, version, pipeline}."""
    from . import mlflow_bridge as MF

    return MF.link(ctx(), body["model"], str(body["version"]), body["pipeline"])


@app.get("/api/mlflow/lineage", dependencies=A)
def mlflow_lineage():
    from . import mlflow_bridge as MF

    return MF.lineage(ctx())


@app.post("/api/sandbox/sweep", dependencies=A)
def sandbox_sweep(body: dict):
    """Turn up to three knobs: {pipeline, queries, judgments, knobs?, sample?, top?, seed?, metric?}. Runs the grid on
    a stratified sample, then the best few on every query, against the current values. Runs as a job."""
    from . import sandbox as SB

    c = ctx()
    return {"job": _job(lambda p: SB.sweep(c, body["pipeline"], body["queries"], body["judgments"], body.get("knobs"),
                                           sample=int(body.get("sample", 60)), top=int(body.get("top", 3)),
                                           seed=int(body.get("seed", 7)), metric=body.get("metric", "ndcg@10"),
                                           progress=p), f"sweep {body.get('pipeline')}")}


@app.post("/api/sandbox/ripple", dependencies=A)
def sandbox_ripple(body: dict):
    """What one setting changes, query by query: {pipeline, queries, judgments, values, must_not_break?}."""
    from . import sandbox as SB

    return SB.ripple(ctx(), body["pipeline"], body["queries"], body["judgments"], dict(body["values"]),
                     body.get("must_not_break"))


@app.post("/api/sandbox/promote", dependencies=A)
def sandbox_promote(body: dict):
    """Publish a setting as the pipeline's next version, for a pre-registered backtest. Nothing goes live."""
    from . import sandbox as SB

    return SB.promote(ctx(), body["pipeline"], dict(body["values"]), body.get("note", ""))


@app.post("/api/evals/health", dependencies=A)
def evals_health(body: dict):
    """How well an evaluation set still tells the truth: {queries, judgments, logs?, catalog?, lifetime_days?}.
    Returns representativeness, staleness, coverage, reliability, findings in words, and a status."""
    from . import evals as EV

    return EV.health(ctx(), body["queries"], body["judgments"], body.get("logs"), body.get("catalog"),
                     int(body.get("lifetime_days") or EV.LIFETIME_DAYS))


@app.post("/api/evals/refresh", dependencies=A)
def evals_refresh(body: dict):
    """Refresh an evaluation set as new versions: {queries, judgments, logs, catalog?, add?, seed?, dry_run?}. Runs
    as a job. dry_run returns the plan and changelog without publishing."""
    from . import evals as EV

    c = ctx()
    return {"job": _job(lambda p: EV.refresh(c, body["queries"], body["judgments"], body["logs"], body.get("catalog"),
                                             int(body.get("add", 50)), int(body.get("seed", 7)),
                                             bool(body.get("dry_run"))), "refresh evaluation set")}


@app.get("/api/rubrics", dependencies=A)
def rubrics_list():
    """The team's domain rubrics from captain/rubrics/*.md."""
    from . import rubrics as RB

    return RB.listing(ctx().workspace.name)


@app.post("/api/rankings/validate", dependencies=A)
def rankings_validate(body: dict):
    """Check a rankings file before importing: {path | dataset, mapping?, judgments?}. Returns {ok, errors, warnings,
    stats, available}, all in plain words."""
    from . import rankings as RK
    from .data import datasets as DS

    c = ctx()
    if body.get("path"):
        import os

        t = DS.read_file(os.path.expanduser(body["path"]))
    else:
        t = DS.load_table(c, c.registry.resolve("dataset", body["dataset"])[0])
    return RK.validate(t, body.get("mapping"), judgments=bool(body.get("judgments")))


@app.post("/api/rankings/import", dependencies=A)
def rankings_import(body: dict):
    """Import ranked results from a team's own system as a comparison run. Runs as a job.
    {name, path | dataset, mapping?, baseline?, labels_from?, judgments?, primary_metric?, mde?, hypothesis?, project?}"""
    from . import rankings as RK

    c = ctx()
    src = body.get("path") or body.get("dataset")
    if not src or not body.get("name"):
        raise ValueError("give the rankings (a path or a dataset) and a name for the comparison")
    kw = {k: body[k] for k in ("mapping", "baseline", "labels_from", "judgments", "primary_metric", "mde", "hypothesis",
                               "project", "catalog") if body.get(k) not in (None, "")}
    return {"job": _job(lambda p: RK.import_rankings(c, body["name"], src, **kw), f"import rankings {body['name']}")}


@app.get("/api/explorer", dependencies=A)
def explorer_tree():
    """The Explorer: All engines, All experiments, the scope tree (Brand > Site > Engine by default), Projects and
    Settings. Ids are stable. The same engine can appear under several scopes."""
    from . import scopes as SC

    return SC.explorer(ctx())


@app.get("/api/scope-model", dependencies=A)
def scope_model_get():
    from . import scopes as SC

    return SC.model(ctx())


@app.put("/api/scope-model", dependencies=A)
def scope_model_put(body: dict):
    """Replace the scope model (types, terminology, hierarchy, explorer). Refused with reasons if invalid.
    Send expect_revision to avoid overwriting someone else's change."""
    from . import scopes as SC

    rev = body.pop("expect_revision", None)
    return SC.set_model(ctx(), body, rev)


@app.post("/api/scopes", dependencies=A)
def scope_add(body: dict):
    from . import scopes as SC

    return SC.add_scope(ctx(), body.get("type", ""), str(body.get("name", "")), body.get("parent"), body.get("fields"))


@app.put("/api/scopes/{sid}", dependencies=A)
def scope_update(sid: str, body: dict):
    from . import scopes as SC

    return SC.update_scope(ctx(), sid, body.get("name"), body["parent"] if "parent" in body else False,
                           body.get("archived"), body.get("fields"))


@app.get("/api/engines", dependencies=A)
def engines_list():
    from . import scopes as SC

    return SC._data(ctx())["engines"]


@app.post("/api/engines", dependencies=A)
def engine_add(body: dict):
    from . import scopes as SC

    return SC.add_engine(ctx(), str(body.get("name", "")), body.get("type", "search"), body.get("scopes"),
                         body.get("pipelines"), body.get("description", ""))


@app.put("/api/engines/{eid}", dependencies=A)
def engine_update(eid: str, body: dict):
    from . import scopes as SC

    return SC.update_engine(ctx(), eid, **{k: body.get(k) for k in ("name", "type", "scopes", "pipelines",
                                                                    "description", "archived")})


@app.post("/api/scopes/link", dependencies=A)
def scope_link(body: dict):
    """{items: [{kind, name}], scope? | engine?}: which scope or engine records belong to. Neither unlinks."""
    from . import scopes as SC

    return SC.link(ctx(), list(body.get("items") or []), body.get("scope"), body.get("engine"))


@app.get("/api/projects", dependencies=A)
def projects_list():
    """Projects (one per company) with what each holds: connections, datasets, pipelines, models, backtests,
    investigations and registries."""
    from . import projects as PR

    return PR.listing(ctx())


@app.post("/api/projects", dependencies=A)
def project_create(body: dict):
    """{name, description?, brands?, icon?, kind?, what?, slots?, trainer?}. `kind` is a recipe id (GET /api/recipes)
    or "other" with `what`, the person's own description of what they are building. `slots` is the list of inputs
    as the form was left: [{label, kind?, id?, required?, why?}], which replaces the recipe's when given. `trainer` is
    {how: builtin | command | repo, command?, repo?}. Everything is checked before the project exists."""
    from . import project_state as PS
    from . import projects as PR
    from . import recipes as RC

    c = ctx()
    kind = body.get("kind") or None
    if kind and kind != PS.OTHER:
        RC.get(c, kind)  # an unknown kind fails before the project exists
    custom = any(body.get(k) is not None for k in ("what", "slots", "trainer")) or kind == PS.OTHER
    if custom:
        try:  # the same checks `start` makes, on nothing, so a bad form leaves no half-made project
            if kind == PS.OTHER and not str(body.get("what") or "").strip():
                raise ValueError("Say what you are building, in a few words.")
            seen = set()
            for raw in body.get("slots") or []:
                if not str((raw or {}).get("label") or "").strip():
                    raise ValueError("Every input needs a name.")
                got = RC.check_slot({**({"id": raw["id"]} if raw.get("id") else {}), "label": raw["label"], "accepts": [raw["kind"]] if raw.get("kind") else ["any"]})
                if got["id"] in seen:
                    raise ValueError(f"Two inputs are named {str(raw['label']).strip()!r}. Give each its own name.")
                seen.add(got["id"])
            t = body.get("trainer")
            if t is not None and str((t or {}).get("how") or "") not in PS.TRAINERS:
                raise ValueError("The trainer is one of: builtin, command or repo.")
            if t and t.get("how") == "repo" and not str(t.get("repo") or "").strip():
                raise ValueError("Give the address of your training repo.")
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    p = PR.create(c, str(body.get("name", "")), body.get("description", ""), body.get("brands"), body.get("icon"))
    if custom:
        try:
            PS.start(c, p["id"], kind, body.get("what"), body.get("slots"), body.get("trainer"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    elif kind:
        PS.set_kind(c, p["id"], kind)
    return {**p, "kind": kind}


@app.get("/api/recipes", dependencies=A)
def recipes_list():
    """What a project can build: built-in recipes, the workspace's recipes/ and the repo's captain/recipes/."""
    from . import recipes as RC

    rs, errors = RC.load(ctx())
    return {"recipes": RC.listing(ctx()), "errors": errors}


@app.delete("/api/projects/{pid}", dependencies=A)
def project_delete(pid: str, force: bool = False):
    """Delete a project and its state. Datasets, pipelines, models and runs stay. 409 with the handoffs when a live
    handoff of its champion or experiments waits for a deployment, unless force=true."""
    from . import projects as PR

    try:
        return PR.delete(ctx(), pid, force=force)
    except ValueError as e:
        if getattr(e, "handoffs", None):
            return JSONResponse({"error": str(e), "handoffs": e.handoffs}, status_code=409)
        raise


@app.post("/api/projects/assign", dependencies=A)
def project_assign(body: dict):
    """{project: id or null, items: [{kind, name}]}. Moves nothing: it only says which project each item is in."""
    from . import projects as PR

    return PR.assign(ctx(), body.get("project"), list(body.get("items") or []), bool(body.get("clear")))


@app.get("/api/projects/{pid}/home", dependencies=A)
def project_home(pid: str):
    """A project's home screen: its datasets, models, notebooks and recent runs."""
    from . import projects as PR

    return PR.home(ctx(), pid)


@app.get("/api/services", dependencies=A)
def services_list(check: bool = False):
    """Every outside service this workspace uses, for Settings: storage, connections, compute (with defaults),
    registries, MLflow and model keys. check=true asks each whether it answers now."""
    from . import services as SV

    return SV.listing(ctx(), check)


def _settings_out(d: dict) -> dict:
    """Settings as the screens and agents get them: the alert webhook's address can hold a secret, so only whether
    one is set leaves the service."""
    out = dict(d)
    out["alerts"] = {"webhook_set": bool((d.get("alerts") or {}).get("webhook_url"))}
    return out


@app.get("/api/settings", dependencies=A)
def settings_get():
    from . import settings as ST

    return _settings_out(ST.get(ctx()))


@app.put("/api/settings/compute", dependencies=A)
def settings_compute(body: dict):
    """{backtest?, serve?}: the compute targets backtests and serving use unless they name their own."""
    from . import settings as ST

    return _settings_out(ST.set_compute(ctx(), body.get("backtest"), body.get("serve"), clear_serve="serve" in body and not body["serve"]))


@app.get("/api/serving/machines", dependencies=A)
def serving_machines():
    """For Settings: per cloud a model can be deployed to, the sizes on offer, cheapest first, and the one chosen
    in place of the smallest that fits (null when none is). {hosts: [{host, label, chosen, machines: [{name,
    memory_mb, hourly_usd, text}]}]}. Which sizes a model fits on depends on the model, so that is checked when a
    deploy is planned."""
    from . import settings as ST
    from .staging import sizing as SZ

    picked = ((ST.get(ctx()).get("serving") or {}).get("machine") or {})
    return {"hosts": [{"host": h, "label": label, "chosen": picked.get(h), "machines": SZ.offered(h, None)} for h, label in SZ.WHERE.items()]}


@app.put("/api/settings/serving", dependencies=A)
def settings_serving(body: dict):
    """{host, machine}: the machine a deploy on that host uses, in place of the smallest the model fits on. `host` is
    aws, modal, gcp or azure. `machine` is a size's name (as GET .../deploy/hosts lists under `machines`), or empty
    to go back to the smallest that fits. Or {name_prefix}: the start of the name of everything a first deploy makes
    in a cloud account ("captain" when empty)."""
    from . import settings as ST

    try:
        if "name_prefix" in body:
            got = ST.set_serving_prefix(ctx(), body.get("name_prefix"))
            if "host" not in body:
                return got
        return ST.set_serving_machine(ctx(), str(body.get("host") or ""), body.get("machine"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.put("/api/settings/staging", dependencies=A)
def settings_staging(body: dict):
    """{local_limit_mb}: the largest model to stage on this computer: 250, 500, 1024, 2048, 4096, or 0 for no limit."""
    from . import settings as ST

    try:
        return _settings_out(ST.set_local_limit(ctx(), body.get("local_limit_mb")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.put("/api/settings/costs", dependencies=A)
def settings_costs(body: dict):
    """{confirm_above_usd}: the estimate above which a form asks again before starting work on a cloud."""
    from . import settings as ST

    return _settings_out(ST.set_costs(ctx(), body.get("confirm_above_usd")))


@app.post("/api/costs/estimate", dependencies=A)
def costs_estimate(body: dict):
    """{kind, where?, size?}: what work costs before it runs. kind is training, evaluation, synthetic-events,
    snapshot, judging, staging or workflow. where is a compute target's name ("local" is this computer), a
    connection for a snapshot, a model for judging. Answers {low, high, currency, unit: run|hour, basis: [sentences],
    priced_as_of, confidence: rough|measured, free, text, confirm, threshold, minutes_low, minutes_high}. low and
    high are null, and text says why, where Captain has no estimate."""
    from . import cost_estimates as CE

    try:
        return CE.estimate(ctx(), str(body.get("kind") or ""), body.get("where") or None, body.get("size") or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/costs/runs", dependencies=A)
def costs_runs(project: str | None = None, limit: int = 100):
    """Recorded runs with their estimate and what they then cost, newest first."""
    from . import cost_estimates as CE

    return CE.ledger(ctx(), project=project, limit=min(limit, 400))


@app.get("/api/projects/{pid}/costs", dependencies=A)
def project_costs(pid: str):
    """{usd, runs, text, recent}: what this project's runs on the cloud have cost so far. Also settles training
    jobs that finished since the last look, so their candidates carry their cost."""
    from . import cost_estimates as CE
    from . import starter as STR

    c = ctx()
    try:
        STR.settle_all(c, pid)
    except Exception:  # noqa: BLE001  the total still answers when a provider can't be asked
        pass
    return CE.project_total(c, pid)


@app.get("/api/scope-model/suggested", dependencies=A)
def scope_model_suggested():
    """A starting set of levels (Brand, then Site) to offer in Settings. The model itself starts empty."""
    from . import scopes as SC

    return {"types": SC.SUGGESTED}


@app.put("/api/projects/{pid}/icon", dependencies=A)
def project_icon(pid: str, body: dict):
    """{icon: {kind: glyph, name, color} | {kind: brand, slug} | {kind: logo, sha, ext} | null}. null clears it."""
    from . import projects as PR

    return PR.set_icon(ctx(), pid, body.get("icon"))


@app.post("/api/projects/{pid}/logo", dependencies=A)
def project_logo(pid: str, body: dict):
    """{path}: a PNG, JPEG or SVG on this computer, put in the workspace store (so teammates see it) and made the
    project's icon."""
    from . import projects as PR

    return PR.upload_logo(ctx(), pid, str(body.get("path") or ""))


@app.post("/api/projects/{pid}/logo/file", dependencies=A)
def project_logo_file(pid: str, body: dict):
    """{dest}: write the logo, small, into <app home>/cache/project-logos for a screen to draw. Returns {file}."""
    from . import projects as PR

    return PR.logo_file(ctx(), pid, str(body.get("dest") or ""))


@app.get("/api/projects/{pid}", dependencies=A)
def project_detail(pid: str):
    from . import projects as PR

    return PR.contents(ctx(), pid)


@app.put("/api/projects/{pid}", dependencies=A)
def project_update(pid: str, body: dict):
    from . import projects as PR

    return PR.update(ctx(), pid, body.get("name"), body.get("description"), body.get("archived"), body.get("brands"))


@app.get("/api/projects/{pid}/lineage", dependencies=A)
def project_lineage(pid: str):
    """The project's models (from attached registries, by brand), the pipelines serving them, and the backtests
    that compared those pipelines."""
    from . import projects as PR

    return PR.lineage(ctx(), pid)


@app.get("/api/projects/{pid}/state", dependencies=A)
def project_state(pid: str):
    """The project's state doc: pinned inputs, experiments, champion and registry place."""
    from . import project_state as PS

    return PS.get(ctx(), pid)


@app.get("/api/projects/{pid}/stages", dependencies=A)
def project_stages(pid: str):
    """Each pinned input with "newer version available" info, the experiments' counts and the champion, with the
    next action for each. Read from the state doc and the lineage index only, never a registry listing."""
    from . import project_state as PS

    return PS.stages(ctx(), pid)


@app.post("/api/projects/{pid}/pin", dependencies=A)
def project_pin(pid: str, body: dict):
    """{slots?: {slot: ref}, catalog?, events?, queries?, labels?, holdout?, arch?, brand?, registry?, dry_run?}. A
    dataset name alone pins its newest version; "" or null unpins."""
    from . import project_state as PS

    c, dry = ctx(), bool(body.get("dry_run"))
    refs = {k: body[k] for k in PS.SLOTS if k in body}
    refs.update(body.get("slots") or {})  # {slot: ref} for any slot, including the project's own
    out = PS.pin(c, pid, refs, dry_run=dry) if refs else {"project": pid, "changed": {}}
    if any(k in body for k in ("arch", "brand", "registry")):
        out["registry"] = PS.set_registry(c, pid, body.get("arch"), body.get("brand"), body.get("registry"), dry_run=dry)["registry"]
    return out


@app.get("/api/projects/{pid}/experiments", dependencies=A)
def project_experiments(pid: str):
    """Each experiment with its evaluation against the champion, label independence and verdict."""
    from . import project_state as PS

    return PS.experiments(ctx(), pid)


@app.post("/api/projects/{pid}/experiments", dependencies=A)
def project_experiment_add(pid: str, body: dict):
    """{pipeline, training_set?, labels?, trainer?, note?, dry_run?}: record a trained version as an experiment."""
    from . import project_state as PS

    if not body.get("pipeline"):
        raise HTTPException(400, "name the trained model version")
    return PS.experiment_add(ctx(), pid, body["pipeline"], body.get("training_set"), body.get("labels"),
                             body.get("trainer"), body.get("note", ""), dry_run=bool(body.get("dry_run")))


@app.get("/api/projects/{pid}/champion", dependencies=A)
def project_champion(pid: str):
    from . import project_state as PS

    d = PS.get(ctx(), pid)
    return {"project": pid, "champion": d.get("champion"), "history": d.get("champion_history") or []}


@app.post("/api/projects/{pid}/champion", dependencies=A)
def project_champion_set(pid: str, body: dict):
    """{experiment | pipeline, expect?, dry_run?}. expect is the champion the page showed: if it changed since,
    nothing is written (409). experiment and pipeline both null clears it."""
    from . import project_state as PS

    which = body.get("experiment") or body.get("pipeline")
    return PS.set_champion(ctx(), pid, which, body["expect"] if "expect" in body else "", dry_run=bool(body.get("dry_run")))


@app.post("/api/projects/{pid}/experiments/{eid}/backtest", dependencies=A)
def project_experiment_backtest(pid: str, eid: str, body: dict | None = None):
    """{compute?, dry_run?}: backtest the experiment against the champion on the sealed holdout, as a job. Returns
    {job, live}; the run lands on the experiment when it finishes."""
    from . import live as LV
    from . import project_state as PS
    from . import settings as ST
    from . import compute as CMP

    body = body or {}
    c = ctx()
    if body.get("dry_run"):
        return PS.experiment_backtest(c, pid, eid, body.get("compute"), dry_run=True)
    plan = PS.experiment_backtest(c, pid, eid, body.get("compute"), dry_run=True)  # fail fast, before a job starts
    compute = body.get("compute") or ST.default_compute(c, "backtest") or "local"
    local = CMP.get(c, compute)["kind"] == "local"
    lv = LV.start("backtest", plan["backtest"], of=plan["backtest"], arms=list(plan["arms"])) if local else None
    live_id = None if local else LV.start_remote(c, "backtest", plan["backtest"], of=plan["backtest"],
                                                 arms=list(plan["arms"]), where=compute)

    def go(p):
        try:
            r = PS.experiment_backtest(c, pid, eid, compute, progress=p, live=lv, live_id=live_id)
        except Exception as e:
            if lv:
                lv.end("failed", error=str(e))
            if live_id:
                LV.note_remote(c, live_id, status="failed", error=f"Couldn't submit the job: {e}")
            raise
        if live_id and r.get("job"):
            LV.note_remote(c, live_id, job=r["job"])
        if lv:
            lv.end("done", ref=r.get("run"), summary=f"Compared {plan['arms'][1]} with the baseline")
        return r

    jid = _job(go, f"comparing {plan['arms'][1]} with the baseline on {compute}")
    JOBS[jid]["compute"] = compute
    rid = lv.id if lv else live_id
    JOBS[jid]["live"] = rid
    return {"job": jid, "live": rid, "experiment": plan["experiment"], "backtest": plan["backtest"]}


@app.post("/api/projects/{pid}/kind", dependencies=A)
def project_kind(pid: str, body: dict):
    """{kind}: start from a recipe's slots and evaluation. Inputs whose slot id is still there are kept."""
    from . import project_state as PS

    return PS.set_kind(ctx(), pid, str(body.get("kind") or ""))


@app.post("/api/projects/{pid}/slots", dependencies=A)
def project_slot_add(pid: str, body: dict):
    """{id?, label, role?, accepts?, required?, why?}: an input slot of the project's own."""
    from . import project_state as PS

    return PS.slot_add(ctx(), pid, body)


@app.put("/api/projects/{pid}/slots/{slot}", dependencies=A)
def project_slot_update(pid: str, slot: str, body: dict):
    from . import project_state as PS

    return PS.slot_update(ctx(), pid, slot, body)


@app.delete("/api/projects/{pid}/slots/{slot}", dependencies=A)
def project_slot_remove(pid: str, slot: str):
    """Remove an optional slot. A required one is refused: make it optional first."""
    from . import project_state as PS

    return PS.slot_remove(ctx(), pid, slot)


@app.put("/api/projects/{pid}/evaluation", dependencies=A)
def project_evaluation(pid: str, body: dict):
    """{method: auto | holdout | logged-clicks | trainer-reported | none}."""
    from . import project_state as PS

    return PS.set_evaluation(ctx(), pid, str(body.get("method") or ""))


@app.get("/api/projects/{pid}/baseline-start", dependencies=A)
def project_baseline_plan(pid: str, title: str | None = None):
    """What "Start with keyword search as the baseline" would do: {ready, text, title, columns, ask_title}."""
    from . import starter as STR

    return STR.baseline_plan(ctx(), pid, title)


@app.post("/api/projects/{pid}/baseline-start", dependencies=A)
def project_baseline_start(pid: str, body: dict | None = None):
    """{title?, dry_run?}: make a keyword-search version over the project's catalog (no training) and set it as the
    baseline. 400 with a sentence when there is no catalog, no title column can be found, or a baseline exists."""
    from . import starter as STR

    b = body or {}
    try:
        return STR.baseline_start(ctx(), pid, b.get("title") or None, dry_run=bool(b.get("dry_run")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/projects/{pid}/starter", dependencies=A)
def project_starter_plan(pid: str):
    """What a starter ranker would train on: the inputs it uses, the cutoff dates it picked and why, whether the
    events are simulated, whether the trainer needs its one-time setup, and what is missing."""
    from . import starter as STR

    return STR.plan(ctx(), pid)


@app.post("/api/projects/{pid}/starter/train", dependencies=A)
def project_starter_train(pid: str, body: dict | None = None):
    """{params?, compute?}: train a starter ranker with no code, as a job with a live run. With a compute target
    it trains on that cloud and answers {job, live, pending: true, estimate}. It sets keyword search as the
    baseline when there is none, builds the training set, trains, and records the candidate. Returns {job, live};
    the job's result is {experiment, pipeline, training_set, baseline, baseline_set, metrics, simulated, next}."""
    from . import live as LV
    from . import starter as STR

    c, b = ctx(), body or {}
    p = STR.plan(c, pid)
    if not p["ready"]:
        raise HTTPException(400, p["text"])
    compute = b.get("compute") or "local"
    from . import compute as CMP

    if CMP.get(c, compute)["kind"] != "local":
        # On the person's cloud: the job is submitted now and this answers at once. The candidate lands when the
        # job finishes, and the run streams to Runs through the workspace bucket.
        try:
            r = STR.train_on(c, pid, compute, params=b.get("params") or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"job": r["job"], "live": r["live_id"], "pending": True, "compute": compute, "estimate": r["estimate"],
                "simulated": p["simulated"], "baseline_will_be_set": bool((p.get("baseline") or {}).get("will_set")),
                "text": r["text"]}
    lv = LV.start("training", f"Starter ranker for {pid}", of=pid)

    def go(_p):
        try:
            r = STR.train(c, pid, live=lv, params=b.get("params") or None)
        except BaseException as e:
            lv.end("failed", error=str(e) or "stopped")
            raise
        lv.end("done", ref=r["pipeline"], summary=r["text"])
        return r

    jid = _job(go, f"training a starter ranker for {pid}")
    JOBS[jid]["live"] = lv.id
    return {"job": jid, "live": lv.id, "simulated": p["simulated"], "baseline_will_be_set": bool((p.get("baseline") or {}).get("will_set"))}


# ---- staging: a live endpoint for a model version, on the team's own compute or on this computer ------------


def _staging_call(fn, *a, **kw):
    """Staging's refusals are sentences for the person: 400 with the sentence, and the plan when a cost needs confirming."""
    from .compute_providers import ProviderError
    from .staging import NeedsConfirm, NotServable

    try:
        return fn(*a, **kw)
    except NeedsConfirm as e:
        return JSONResponse({"detail": str(e), "needs_confirm": True, "plan": e.plan}, status_code=409)
    except (NotServable, ProviderError, ValueError) as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/projects/{pid}/staging", dependencies=A)
def project_staging(pid: str, env: str = "staging", sweep: bool = True):
    """Where staging stands: {status: none|starting|running|stopped|failed, staged, health, stops_at, text}. With
    sweep (the default) an endpoint past its idle limit is stopped here."""
    from . import staging as SG

    c = ctx()
    cloud = None
    rec = SG.state(c, pid, env)
    if rec and rec.get("kind") != "local":
        # The cloud is the truth for a model deployed there: its record is set right from the cloud before it is
        # read (staging/fleet.py, kept a few seconds). A model deployed, stopped or replaced from somewhere else
        # then shows as it is. If the cloud can't be asked just now, the record stands as it is.
        try:
            from .staging import fleet as FL

            row = FL.one(c, pid, env)
            if row:
                cloud = {k: row.get(k) for k in ("state", "state_label", "state_text", "link", "corrected", "copies")}
        except Exception:  # noqa: BLE001
            cloud = None
    out = _staging_call(SG.status, c, pid, env, sweep)
    return {**out, "cloud": cloud} if isinstance(out, dict) else out


@app.post("/api/projects/{pid}/stage", dependencies=A)
def project_stage(pid: str, body: dict | None = None):
    """{candidate?, baseline?, env?, target?, endpoint?, endpoint_auth_secret?, image?, idle_minutes?, confirm?,
    dry_run?}: stage a model version, as a job. dry_run returns the plan with its cost. A cloud deploy without
    confirm is refused with 409 {needs_confirm, plan}: show the cost, then send confirm."""
    from . import staging as SG

    c, b = ctx(), body or {}
    args = (b.get("candidate") or None, bool(b.get("baseline")), b.get("env") or "staging", b.get("target") or None,
            b.get("endpoint") or None, b.get("image") or None)
    p = _staging_call(SG.plan, c, pid, *args, allow_large=bool(b.get("allow_large")))
    if isinstance(p, JSONResponse):
        return p
    if b.get("dry_run"):
        return {**p, "dry_run": True}
    if p["kind"] == "local" and (p.get("local_fit") or {}).get("outcome") in ("wont-fit", "over-limit"):
        raise HTTPException(400, p["local_fit"]["text"])  # refused here, in the sentence the choice shows, not inside a job
    if p["needs_confirm"] and not b.get("confirm"):
        return JSONResponse({"detail": "Confirm the cost first. " + p["cost"]["text"], "needs_confirm": True, "plan": p},
                            status_code=409)

    def go(_p):
        say = lambda t: JOBS[jid]["log"].append(str(t)) or JOBS[jid].__setitem__("message", str(t))  # noqa: E731
        return SG.stage(c, pid, args[0], args[1], args[2], args[3], args[4], b.get("endpoint_auth_secret") or None,
                        args[5], confirm=True, idle_minutes=b.get("idle_minutes") or None, progress=say,
                        allow_large=bool(b.get("allow_large")))

    jid = _job(go, f"staging {p['model_version']} for {pid}")
    JOBS[jid]["compute"] = p["target"]
    return {"job": jid, "plan": p}


@app.post("/api/projects/{pid}/unstage", dependencies=A)
def project_unstage(pid: str, body: dict | None = None):
    """{env?}: stop staging and remove what Captain created, as a job. The result is the staging record."""
    from . import staging as SG

    c, env = ctx(), (body or {}).get("env") or "staging"
    if not SG.state(c, pid, env):
        raise HTTPException(400, f"Nothing is deployed for {pid}.")
    return {"job": _job(lambda _p: SG.unstage(c, pid, env), f"stopping staging for {pid}")}


@app.post("/api/projects/{pid}/staging/query", dependencies=A)
def project_staging_query(pid: str, body: dict):
    """{query, k?, env?}: the staged endpoint's results joined with catalog titles and images, with the baseline's
    results for the same query and each side's latency. Logged as test traffic."""
    from . import staging as SG

    return _staging_call(SG.query, ctx(), pid, str(body.get("query") or ""), int(body.get("k") or 10),
                         body.get("env") or "staging")


@app.post("/api/projects/{pid}/staging/check", dependencies=A)
def project_staging_check(pid: str, body: dict | None = None):
    """{env?, sample?, min_match?}: compare the features the endpoint served with the features training computes.
    {status: pass|fail|nothing-to-check|unavailable, text, checked, match_rate, mismatch_rate, missing_rate,
    extra_rate, features: [{feature, mismatch_rate, relative_error}], examples}. A fail blocks launch review."""
    from .staging import consistency as SC

    b = body or {}
    return _staging_call(SC.check, ctx(), pid, b.get("env") or "staging", int(b.get("sample") or SC.SAMPLE),
                         float(b.get("min_match") or SC.MIN_MATCH))


def _host_options(q: dict) -> dict:
    """The setup options a host was given: everything but the words the routes themselves use."""
    return {k: v for k, v in q.items() if k not in ("cloud", "confirm", "plan") and v not in (None, "")}


@app.get("/api/projects/{pid}/staging/scale", dependencies=A)
def project_staging_scale(pid: str):
    """{min_copies, max_copies, idle_seconds, can_set, text}: how many copies the staged endpoint may run. (The
    copies that are running now are GET .../staging/copies, the Copies tab.)"""
    from . import staging as SG

    return _staging_call(SG.scale, ctx(), pid)


@app.post("/api/projects/{pid}/staging/scale", dependencies=A)
def project_staging_scale_set(pid: str, body: dict):
    """{min_copies?, max_copies?, idle_seconds?}: change them. The host applies it with no restage."""
    from . import staging as SG

    num = lambda k: None if body.get(k) in (None, "") else int(body[k])  # noqa: E731
    return _staging_call(SG.scale, ctx(), pid, min_copies=num("min_copies"), max_copies=num("max_copies"), idle_seconds=num("idle_seconds"))


@app.get("/api/projects/{pid}/deploy/hosts", dependencies=A)
def project_deploy_hosts(pid: str, candidate: str | None = None, baseline: bool = False):
    """The places this project's model can be deployed: this computer, and one entry per cloud login connected in
    Captain. {model_version, beside, hosts: [{host, label, login, region, can_deploy, why_not, first_time, proven,
    proven_text, sleeps, reachable, estimate, minutes, fit?, last}]}. Calls no cloud."""
    from .staging import deploy as DP

    return _staging_call(DP.offers, ctx(), pid, candidate or None, bool(baseline))


@app.get("/api/projects/{pid}/deploy", dependencies=A)
def project_deploy_status(pid: str):
    """Where the last deploy stands: {status: none | running | done | failed | removed, host, login, run, steps:
    [{id, title, status, seconds, lines}], failed_step, error, exists_now, can: [retry, remove], policy, staged}."""
    from .staging import deploy as DP

    return _staging_call(DP.status, ctx(), pid)


@app.post("/api/projects/{pid}/deploy", dependencies=A)
def project_deploy(pid: str, body: dict | None = None):
    """{host, login?, candidate?, baseline?, allow_large?, confirm?, dry_run?}: deploy the project's model there,
    first-time work included, as one job with named steps. dry_run returns the plan. A cloud without confirm is
    refused with 409 {needs_confirm, plan}. With confirm: {job, run, plan}; `run` is a live run (GET /api/live/{run})
    whose stages are the plan's steps. Safe to send again: what exists is kept, and a failed deploy is picked up."""
    from . import live as LV
    from .staging import deploy as DP

    c, b = ctx(), body or {}
    args = dict(host=str(b.get("host") or "local"), login=b.get("login") or None, candidate=b.get("candidate") or None,
                baseline=bool(b.get("baseline")), allow_large=bool(b.get("allow_large")))
    p = _staging_call(DP.plan, c, pid, **args)
    if isinstance(p, JSONResponse):
        return p
    if b.get("dry_run"):
        return {**p, "dry_run": True}
    if p["host"] == "local" and (p.get("fit") or {}).get("outcome") in ("wont-fit", "over-limit"):
        raise HTTPException(400, p["fit"]["text"])
    if p["needs_confirm"] and not b.get("confirm"):
        return JSONResponse({"detail": "Confirm first. " + p["cost"]["text"], "needs_confirm": True, "plan": p}, status_code=409)
    lr = LV.start("custom", f"Deploying {p['model_version']} to {p['label']}", of=pid, source="deploy")

    def go(prog):
        def hear(kind, **d):
            if kind == "step":
                lr.stage(d["title"], d["index"], d["of"])
                prog((d["index"] - 1) / d["of"])
                JOBS[jid].update(message=d["title"], step=d["id"])
                JOBS[jid]["log"].append(d["title"])
            elif kind == "line":
                lr.log(d["text"])
                JOBS[jid]["log"].append(d["text"])

        try:
            out = DP.run(c, pid, confirm=True, on_event=hear, **args)
            lr.end("done", ref=p["model_version"], summary=f"Ready at {out.get('address')}")
            return out
        except BaseException as e:  # noqa: BLE001
            JOBS[jid]["policy"] = getattr(e, "policy", None)
            lr.end("failed", error=str(e)[:600])
            raise

    jid = _job(go, f"deploying {p['model_version']} to {p['label']}")
    JOBS[jid].update(compute=p["host"], run=lr.id)
    return {"job": jid, "run": lr.id, "plan": p}


@app.delete("/api/projects/{pid}/deploy", dependencies=A)
def project_deploy_remove(pid: str):
    """Stop the model and, when this project's deploy did the first-time work and nothing else uses it, remove
    that too. As a job."""
    from .staging import deploy as DP

    c = ctx()

    def go(_p):
        say = lambda t: JOBS[jid]["log"].append(str(t)) or JOBS[jid].__setitem__("message", str(t))  # noqa: E731
        return DP.remove(c, pid, progress=say)

    jid = _job(go, f"removing the deploy of {pid}")
    return {"job": jid}


@app.get("/api/serving/hosts", dependencies=A)
def serving_hosts():
    """Every place a model can be hosted, as a card: label, whether it sleeps when idle, where the bundle and log
    are kept, what its address looks like, when it last ran on a real account, and what is set up there."""
    from .staging import hosts as H

    return H.listing(ctx())


@app.get("/api/serving/setup", dependencies=A)
def serving_setup_status(request: Request, profile: str | None = None, region: str | None = None, image: str = "auto",
                         cloud: str = "aws", plan: bool = False):
    """What serving setup exists ({status, created, target, image}). With profile and region: also `plan`, what
    setting it up would create, what it costs, and the policy the login needs. `cloud` picks the host (aws by
    default; modal, gcp, azure): its options ride as query parameters, and `plan=true` asks for the plan."""
    from .staging import serving_setup as SS

    c = ctx()
    if cloud != "aws":
        from .staging import hosts as H

        h = _staging_call(H.get, cloud)
        out = h.status(c)
        if plan:
            try:
                out["plan"] = h.plan(c, **_host_options(dict(request.query_params)))
            except Exception as e:  # noqa: BLE001
                out["plan_error"] = str(e)[:300]
        return out
    out = SS.status(c)
    if profile and region:
        try:
            out["plan"] = SS.plan(c, profile, region, source=image)
        except Exception as e:  # noqa: BLE001
            out["plan_error"] = str(e)[:300]
    return out


@app.post("/api/serving/setup", dependencies=A)
def serving_setup_run(body: dict):
    """{profile, region, name_prefix?, instance_type?, image?, bucket?, confirm}: set up serving, as a job. Without
    confirm: 409 with the plan. A missing permission fails the job with the step, the action and the policy."""
    from .staging import serving_setup as SS

    c, b = ctx(), body
    if (b.get("cloud") or "aws") != "aws":
        from .staging import hosts as H

        h = _staging_call(H.get, b["cloud"])
        opts = _host_options(b)
        hp = _staging_call(h.plan, c, **opts)
        if not b.get("confirm"):
            return JSONResponse({"detail": "Confirm what will be created first.", "needs_confirm": True, "plan": hp}, status_code=409)

        def go_host(_p):
            say = lambda t: JOBS[hjid]["log"].append(str(t)) or JOBS[hjid].__setitem__("message", str(t))  # noqa: E731
            try:
                return h.setup(c, confirm=True, progress=say, **opts)
            except H.MissingPermission as e:
                JOBS[hjid]["policy"] = e.policy
                raise

        hjid = _job(go_host, f"setting up serving in your {h.label} account")
        return {"job": hjid, "plan": hp}
    kw = dict(name_prefix=b.get("name_prefix") or "captain", instance_type=b.get("instance_type") or "ml.m5.large",
              source=b.get("image") or "auto", bucket=b.get("bucket") or None)
    p = _staging_call(SS.plan, c, str(b.get("profile") or ""), str(b.get("region") or ""), **kw)
    if not b.get("confirm"):
        return JSONResponse({"detail": "Confirm what will be created first.", "needs_confirm": True, "plan": p}, status_code=409)

    def go(_p):
        say = lambda t: JOBS[jid]["log"].append(str(t)) or JOBS[jid].__setitem__("message", str(t))  # noqa: E731
        try:
            return SS.setup(c, b["profile"], b["region"], confirm=True, progress=say, **kw)
        except SS.MissingPermission as e:
            JOBS[jid]["policy"] = e.policy
            raise

    jid = _job(go, "setting up serving in your AWS account")
    return {"job": jid, "plan": p}


@app.delete("/api/serving/setup", dependencies=A)
def serving_setup_remove(cloud: str = "aws"):
    """Remove what setup made, as a job. Refused while a model is staged on the target."""
    from .staging import serving_setup as SS

    c = ctx()
    if cloud != "aws":
        from .staging import hosts as H

        h = _staging_call(H.get, cloud)
        if h.status(c)["status"] == "none":
            raise HTTPException(400, f"Serving was never set up in {h.label} for this workspace.")
        return {"job": _job(lambda _p: h.teardown(c), "removing the serving setup")}
    if not SS.read(c):
        raise HTTPException(400, "Serving was never set up for this workspace.")
    return {"job": _job(lambda _p: SS.teardown(c), "removing the serving setup")}


@app.get("/api/projects/{pid}/staging/connect", dependencies=A)
def project_staging_connect(pid: str, env: str = "staging"):
    """Use it from your site: {reachable, address, text, live, real, test, simulated, last_real_at, connected, snippets,
    key_note, fallback, blocked?}. Only real requests count as a site calling."""
    from .staging import traffic as TF

    return _staging_call(TF.connect, ctx(), pid, env)


@app.get("/api/projects/{pid}/staging/key", dependencies=A)
def project_staging_key(pid: str, env: str = "staging"):
    """The endpoint key, for the app's Copy button: it goes to the clipboard and is never drawn or logged."""
    from .staging import traffic as TF

    return _staging_call(TF.key_for_copy, ctx(), pid, env)


@app.post("/api/projects/{pid}/staging/test", dependencies=A)
def project_staging_test(pid: str, body: dict | None = None):
    """{query?}: one test request through the door a site uses. Test traffic: never counted as a shopper."""
    from .staging import traffic as TF

    return _staging_call(TF.test_request, ctx(), pid, (body or {}).get("query") or None)


@app.get("/api/projects/{pid}/staging/differ", dependencies=A)
def project_staging_differ(pid: str, env: str = "staging"):
    """Queries where the staged candidate and the baseline differ most, on a sample of the project's own queries:
    {checked, same, same_share, mostly_same, text, queries: [{query, differs, moved}], none_differ}."""
    from .staging import traffic as TF

    return _staging_call(TF.differ, ctx(), pid, env)


@app.get("/api/projects/{pid}/staging/requests", dependencies=A)
def project_staging_requests(pid: str, limit: int = 50):
    """The newest requests the endpoint answered: {rows: [{request_id, ts, query, arm, model_version, ms, results,
    traffic, status}], total}."""
    from .staging import traffic as TF

    return _staging_call(TF.requests, ctx(), pid, "staging", min(200, max(1, limit)))


@app.get("/api/projects/{pid}/staging/requests/{rid}", dependencies=A)
def project_staging_request(pid: str, rid: str):
    """One request in full: its results with the signals served for each, what the shopper did, and the shadowed
    candidate's results when there were any."""
    from .staging import traffic as TF

    return _staging_call(TF.request, ctx(), pid, rid)


@app.get("/api/projects/{pid}/staging/health", dependencies=A)
def project_staging_health(pid: str, history: int = 0):
    """The deployed model's health as last recorded: {status: healthy | unhealthy | unknown | asleep | stopped,
    text, since, checked_at, answering, requests, errors, error_rate, p50_ms, p95_ms, target, reasons}. With
    `history=N`, also the last N recorded checks. Never runs a check."""
    from .staging import health as HL

    c = ctx()
    out = _staging_call(HL.get, c, pid)
    return {**out, "history": HL.history(c, pid, limit=history)["rows"]} if history else out


@app.post("/api/projects/{pid}/staging/health/check", dependencies=A)
def project_staging_health_check(pid: str):
    """Run one check now and record it. {…the health…, changed, alert: the message that went out, or null}."""
    from .staging import health as HL

    return _staging_call(HL.check, ctx(), pid)


@app.put("/api/projects/{pid}/staging/health/target", dependencies=A)
def project_staging_health_target(pid: str, body: dict):
    """{p95_ms?, error_rate?}: what healthy means for this deployment."""
    from .staging import health as HL

    return _staging_call(HL.set_target, ctx(), pid, body.get("p95_ms"), body.get("error_rate"))


@app.get("/api/alerts", dependencies=A)
def alerts_get():
    """{webhook_set, unhealthy: [{project, model_version, text, since}]}: whether a webhook is set (never its
    address), and every deployed model that is unhealthy now. The app reads this to show a notification."""
    from . import project_state as PS
    from . import projects as PR
    from . import settings as ST

    c = ctx()
    bad = []
    for pr in PR.listing(c):
        try:
            for rec in (PS.read(c, pr["id"])[0].get("staging") or {}).values():
                h = (rec or {}).get("health") or {}
                if rec and rec.get("status") == "running" and h.get("status") == "unhealthy":
                    bad.append({"project": pr["id"], "name": pr.get("name") or pr["id"], "model_version": rec.get("model_version"), "text": h.get("text"), "since": h.get("since")})
        except Exception:  # noqa: BLE001
            continue
    return {"webhook_set": bool(((ST.get(c).get("alerts") or {}).get("webhook_url"))), "unhealthy": bad}


@app.put("/api/settings/alerts", dependencies=A)
def settings_alerts(body: dict):
    """{webhook_url}: where a message goes when a deployed model turns unhealthy or recovers. "" turns it off.
    Answers {webhook_set}: the address is never sent back."""
    from . import settings as ST

    try:
        out = ST.set_alerts(ctx(), body.get("webhook_url"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"webhook_set": bool((out.get("alerts") or {}).get("webhook_url"))}


@app.get("/api/projects/{pid}/staging/live", dependencies=A)
def project_staging_live(pid: str):
    """The small card that follows every tab: {state: stopped | idle | handling, label, text, copies, running, line}."""
    from .staging import observe as OB

    return _staging_call(OB.live, ctx(), pid)


@app.get("/api/projects/{pid}/staging/copies", dependencies=A)
def project_staging_copies(pid: str):
    """Each running copy: {rows: [{copy, where, machine, model_version, created_at, started_at, ready_s, requests,
    running, state, status, text}], text}."""
    from .staging import observe as OB

    return _staging_call(OB.copies, ctx(), pid)


@app.get("/api/projects/{pid}/staging/logs", dependencies=A)
def project_staging_logs(pid: str, q: str | None = None, limit: int = 300, request_id: str | None = None, since: str | None = None):
    """The endpoint's log lines, oldest first: {rows: [{ts, text, level, request_id?}], total, matching, streaming,
    last, note}. `q` filters, `since` follows."""
    from .staging import observe as OB

    return _staging_call(OB.logs, ctx(), pid, "staging", q, limit, request_id, since)


@app.get("/api/projects/{pid}/staging/settings", dependencies=A)
def project_staging_settings(pid: str):
    """How the endpoint is set up: {groups: [{title, rows: [[label, value]], edit?}], idle_minutes, idle_choices, can_edit}."""
    from .staging import observe as OB

    return _staging_call(OB.settings, ctx(), pid)


@app.put("/api/projects/{pid}/staging/settings", dependencies=A)
def project_staging_settings_put(pid: str, body: dict):
    """{idle_minutes}: how long an idle endpoint stays up. Takes effect in a few seconds, with no restart."""
    from .staging import observe as OB

    return _staging_call(OB.set_idle, ctx(), pid, body.get("idle_minutes"))


@app.get("/api/projects/{pid}/staging/cost", dependencies=A)
def project_staging_cost(pid: str):
    """What this deployment has cost: {month_text, day_text, rate_text, rows: [{machine, usd, hours, text}], free, note}."""
    from .staging import observe as OB

    return _staging_call(OB.cost, ctx(), pid)


@app.get("/api/projects/{pid}/staging/metrics", dependencies=A)
def project_staging_metrics(pid: str, range: str = "1h"):  # noqa: A002  the query parameter is named for what it is
    """Requests, response time and errors over one time range (15m, 1h, 24h, 7d): {buckets: [{at, requests, errors,
    p50, p95, site_p50, site_p95}], markers, summary}."""
    from .staging import observe as OB

    return _staging_call(OB.metrics, ctx(), pid, "staging", range)


@app.get("/api/deployed", dependencies=A)
def deployed(fresh: bool = False):
    """What is deployed, as each connected cloud reports it, with Captain's records corrected from that:
    {rows, asked, text, read_at}. One row per deployed model across projects and clouds: project, model_version,
    cloud, region, machine, copies, state (starting, running, asleep, stopped, failed, gone, unknown) with the
    cloud's reason, since, by, and `link`: linked, unlinked (in the cloud, no project knows it), gone (removed
    outside Captain), local, or unknown (the cloud could not be asked). Kept for a few seconds. `fresh=true` asks now."""
    from .staging import fleet as FL

    return FL.deployed(ctx(), fresh=fresh)


@app.get("/api/projects/{pid}/deployed/numbers", dependencies=A)
def deployed_numbers(pid: str, range: str = "1h", env: str = "staging"):  # noqa: A002
    """Traffic and numbers for one deployment, from the cloud's own metrics, with the request log as the second
    source: {source, source_text, buckets, summary, cost, not_read, text}. `range` is 15m, 1h, 24h or 7d. Each
    bucket: {t, requests, errors, p50_ms, p95_ms, cpu, memory, copies}. `cost`: {hourly_usd, today_usd, month_usd,
    text}. What a host does not report is said in `not_read`, never shown as a zero."""
    from .staging import numbers as NB

    try:
        return NB.numbers(ctx(), pid, env, range)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/deployed/link", dependencies=A)
def deployed_link(body: dict):
    """{id, project}: give a project something found in a cloud that no project knows (a row of GET /api/deployed
    whose `link` is unlinked). Captain does not know its model version or its key, and the answer says so."""
    from .staging import fleet as FL

    try:
        return FL.link(ctx(), str(body.get("id") or ""), str(body.get("project") or ""), str(body.get("env") or "staging"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/deployed/remove", dependencies=A)
def deployed_remove(body: dict):
    """{id, confirm}: remove from the cloud something Captain made that no project knows. Without `confirm` it
    answers 409 with what would be deleted. It cannot be undone."""
    from .staging import fleet as FL

    try:
        return FL.remove_found(ctx(), str(body.get("id") or ""), confirm=bool(body.get("confirm")))
    except ValueError as e:
        raise HTTPException(409 if str(e).startswith("Confirm first") else 400, str(e)) from None


@app.get("/api/deployed/created", dependencies=A)
def deployed_created():
    """What Captain created in each cloud account the first time a model was deployed there, with what each thing
    is for: {places: [{cloud, cloud_label, login, region, things: [{kind, name, what_for}], in_use_by, can_remove,
    text}], text}. `DELETE /api/serving/setup?cloud=` removes one place's."""
    from .staging import fleet as FL

    return FL.created(ctx())


@app.post("/api/deployed/forget", dependencies=A)
def deployed_forget(body: dict):
    """{project, env?, confirm}: forget a model that was stopped earlier. Captain removes its own record of the
    deployment. Nothing in any cloud changes: nothing of it is there. Without `confirm` it answers 409 with what
    would happen. Refused while anything of it is still in a cloud."""
    from .staging import fleet as FL

    try:
        return FL.forget_stopped(ctx(), str(body.get("project") or ""), str(body.get("env") or "staging"), confirm=bool(body.get("confirm")))
    except ValueError as e:
        raise HTTPException(409 if str(e).startswith("Confirm first") else 400, str(e)) from None


@app.get("/api/deployed/differences", dependencies=A)
def deployed_differences():
    """What differs between Captain's records and the cloud, after correcting it: {corrected, unlinked, not_asked,
    in_sync, text}."""
    from .staging import fleet as FL

    return FL.differences(ctx())


@app.get("/api/staging/deployments", dependencies=A)  # not /api/deployments: that is the deploy history, and stays
def staged_deployments(which: str = "live"):
    """Every staged model across projects: {rows: [{project, name, model_version, state, status, where, by,
    since_text, copies, running, requests_24h, series, errors}]}. `which` is live, stopped or all."""
    from .staging import observe as OB

    try:
        return OB.deployments(ctx(), which)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/projects/{pid}/staging/versions", dependencies=A)
def project_staging_versions(pid: str):
    """Every version put on this endpoint, newest first: {rows: [{n, model_version, at, by, live, can_roll_back}]}."""
    from .staging import traffic as TF

    return _staging_call(TF.versions, ctx(), pid)


@app.post("/api/projects/{pid}/staging/versions/{n}/rollback", dependencies=A)
def project_staging_rollback_to(pid: str, n: int, body: dict | None = None):
    """{reason}: put version n back, through the same one-job deploy, as a job. The reason is required and kept
    with who did it. Follow it with GET /api/projects/{pid}/deploy, like any deploy."""
    from .staging import rollback as RB

    c = ctx()
    why = " ".join(str((body or {}).get("reason") or "").split())
    if not why:
        raise HTTPException(400, "Say why: a roll back is recorded with its reason.")
    if not any(r["n"] == n for r in _staging_call(RB.earlier, c, pid)):
        raise HTTPException(400, f"There is no earlier version {n} to go back to.")
    return {"job": _job(lambda _p: RB.go(c, pid, n, why), f"rolling {pid} back to version {n}")}


@app.get("/api/projects/{pid}/staging/protect", dependencies=A)
def project_staging_protect(pid: str):
    """What the deployed model accepts: {limits: {max_request_kb, rate_per_s}, budget: {ms, fallback}, has_fallback,
    size_text, rate_text, budget_text, key: {rotated_at, previous_until, previous_valid, can_rotate, why_not, text}}."""
    from .staging import protect as PT

    return _staging_call(PT.get, ctx(), pid)


@app.put("/api/projects/{pid}/staging/protect", dependencies=A)
def project_staging_protect_put(pid: str, body: dict):
    """Any of {max_request_kb, rate_per_s, budget_ms, fallback}. Takes effect in a few seconds, with no restart."""
    from .staging import protect as PT

    return _staging_call(lambda c, p: PT.change(c, p, **{k: body[k] for k in ("max_request_kb", "rate_per_s", "budget_ms", "fallback") if k in body}), ctx(), pid)


@app.post("/api/projects/{pid}/staging/key/rotate", dependencies=A)
def project_staging_key_rotate(pid: str, body: dict | None = None):
    """{grace_minutes?}: make a new key. The old one keeps working for the grace period (an hour unless given).
    The answer never holds the key: it is copied the way it always is (GET .../staging/key)."""
    from .staging import protect as PT

    return _staging_call(PT.rotate_key, ctx(), pid, (body or {}).get("grace_minutes"))


@app.get("/api/projects/{pid}/staging/rollbacks", dependencies=A)
def project_staging_rollbacks(pid: str):
    """Every roll back of this deployment, newest first: {rows: [{at, by, from, to, version, reason, auto}], auto:
    {minutes, on, text}, text}."""
    from .staging import rollback as RB

    return _staging_call(RB.history, ctx(), pid)


@app.put("/api/projects/{pid}/staging/rollbacks/auto", dependencies=A)
def project_staging_rollback_auto(pid: str, body: dict):
    """{minutes}: for the first so many minutes after each deploy, put the previous version back by itself when a
    health check finds the new one unhealthy. 0 or null turns it off."""
    from .staging import rollback as RB

    return _staging_call(RB.set_auto, ctx(), pid, body.get("minutes"))


@app.get("/api/projects/{pid}/shadow", dependencies=A)
def project_shadow(pid: str, traffic: str | None = None):
    """The shadow readout: {status, requests, top1_differs, top10_differs, overlap, queries, ms, evidence, label, text}."""
    from .staging import traffic as TF

    return _staging_call(TF.shadow, ctx(), pid, traffic)


@app.post("/api/projects/{pid}/shadow/start", dependencies=A)
def project_shadow_start(pid: str, body: dict | None = None):
    from .staging import traffic as TF

    return _staging_call(TF.shadow_start, ctx(), pid)


@app.post("/api/projects/{pid}/shadow/stop", dependencies=A)
def project_shadow_stop(pid: str, body: dict | None = None):
    from .staging import traffic as TF

    return _staging_call(TF.shadow_stop, ctx(), pid)


@app.get("/api/sources", dependencies=A)
def sources_list():
    """What a project input can come from: connections and what each offers, datasets, and local folders that hold
    table files. Reads cached facts only."""
    from . import project_inputs as PI

    return PI.sources(ctx())


@app.get("/api/sources/{conn}/events", dependencies=A)
def source_events(conn: str, days: int = 30):
    """PostHog or Google Analytics event names with their counts over the last `days` days."""
    from . import project_inputs as PI

    return PI.event_types(ctx(), conn, days)


@app.post("/api/projects/{pid}/inputs/{slot}", dependencies=A)
def project_input_set(pid: str, slot: str, body: dict):
    """{source: {from: connection|local|dataset, ...}, mode: snapshot|live, as_of?, kind?}. A snapshot from a connection
    or a local path runs as a job (its result is {slot, input}). Everything else answers now."""
    from . import project_inputs as PI
    from . import project_state as PS

    c = ctx()
    src, mode = body.get("source") or {}, body.get("mode")
    if mode not in PI.MODES:
        raise HTTPException(400, "choose snapshot (copy the rows now) or live (read the source at each run)")
    PS.slot(PS.get(c, pid), slot)  # an unknown project or slot fails now, not inside the job
    if PI.needs_job(src, mode):
        jid = _job(lambda p: PI.set_input(c, pid, slot, src, mode, body.get("as_of"), body.get("kind"), p),
                   f"Take a snapshot for {pid}'s {slot} input")
        return {"slot": slot, "mode": mode, "job": jid, "input": None}
    r = PI.set_input(c, pid, slot, src, mode, body.get("as_of"), body.get("kind"))
    return {"slot": slot, "mode": mode, "job": None, "input": r["input"]}


@app.post("/api/projects/{pid}/inputs/{slot}/generate", dependencies=A)
def project_input_generate(pid: str, slot: str, body: dict | None = None):
    """{sessions?, days?, seed?, end?}: generate SIMULATED shopper events from the project's catalog and set them as
    this events input. Runs as a job; its result is {slot, input, dataset, stats, simulated}. 400 when the project has
    no catalog or the slot doesn't take events."""
    from . import project_inputs as PI
    from .sim import events as SE

    c, b = ctx(), body or {}
    try:
        sessions, days, seed = (int(d if b.get(k) is None else b[k]) for k, d in (("sessions", 20000), ("days", 30), ("seed", 7)))
        SE.check(sessions, days)
        plan = PI.events_plan(c, pid, slot)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    jid = _job(lambda p: PI.generate_events(c, pid, slot, sessions, days, seed, b.get("end"), p),
               f"Generate simulated shopper events for {pid}")
    return {"slot": slot, "job": jid, "simulated": True, "catalog": plan["catalog"], "dataset": plan["name"]}


@app.delete("/api/projects/{pid}/inputs/{slot}", dependencies=A)
def project_input_clear(pid: str, slot: str):
    from . import project_inputs as PI

    return PI.clear_input(ctx(), pid, slot)


@app.post("/api/projects/{pid}/assist", dependencies=A)
def project_assist(pid: str, body: dict | None = None):
    """{ask?}: a prompt for a coding agent to propose inputs and a trainer. The app opens the agent with it. Nothing
    is changed until a person accepts the proposal."""
    from . import project_assist as PA

    return PA.assist(ctx(), pid, str((body or {}).get("ask") or ""))


@app.get("/api/projects/{pid}/proposals", dependencies=A)
def project_proposals(pid: str):
    from . import project_assist as PA

    return PA.listing(ctx(), pid)


@app.get("/api/projects/{pid}/proposals/{prop}/script", dependencies=A)
def project_proposal_script(pid: str, prop: str):
    """The proposal's training script as text: {path, key, text}."""
    from . import project_assist as PA

    return PA.script(ctx(), pid, prop)


@app.post("/api/projects/{pid}/proposals/{prop}/accept", dependencies=A)
def project_proposal_accept(pid: str, prop: str, body: dict | None = None):
    """{only?: [slot ids, "trainer", "evaluation"]}. Returns {proposal, state, applied, to_connect, trainer}."""
    from . import project_assist as PA

    return PA.accept(ctx(), pid, prop, (body or {}).get("only") or None)


@app.post("/api/projects/{pid}/proposals/{prop}/reject", dependencies=A)
def project_proposal_reject(pid: str, prop: str, body: dict | None = None):
    from . import project_assist as PA

    return PA.reject(ctx(), pid, prop, str((body or {}).get("note") or ""))


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
    """Prove the login works and the place it points at exists, and say plainly what's wrong if not."""
    from .connectors.checks import verify

    return {"name": name, **verify(ctx().load_connection(name))}


@app.get("/api/data/tables", dependencies=A)
def data_tables(q: str | None = None, kind: str | None = None, project: str | None = None):
    """Every table in one list: the datasets Captain keeps versions of and each connected database's tables.
    {rows: [{kind: dataset | table, name, where, rows, versions, last_read, columns, ...}], connections: [...]}.
    q: every word in the name, where it lives, the description or a column's name. kind: dataset or table.
    ?project= and the Explorer's X-CST-Scope filter the datasets; a database's tables belong to no project, so
    they are left out while something is picked."""
    from .data import catalog as CAT

    c = ctx()
    pick = (_req_scope.get() or "").partition(":")
    scoped = bool(project) or (bool(pick[2]) and pick[0] != "view")
    return CAT.tables(c, _member(c, project, "dataset"), q, kind if kind in ("dataset", "table") else None, scoped)


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


@app.get("/api/tables/{conn}/{schema}/{table}/rows", dependencies=A)
def table_rows(conn: str, schema: str, table: str, offset: int = 0, limit: int = 100, order: str | None = None,
               desc: bool = False, where: str | None = None, count: bool = False):
    """Page through a whole warehouse table, every column, with the same shape as a dataset's rows."""
    from . import metastore as MS

    return MS.table_rows(ctx(), conn, schema, table, offset, limit, order, desc, where, count)


@app.get("/api/tables/{conn}/{schema}/{table}/sample", dependencies=A)
def table_sample(conn: str, schema: str, table: str, n: int = 20, where: str | None = None,
                 order: str | None = None, desc: bool = False):
    from . import metastore as MS

    return MS.sample(ctx(), conn, schema, table, n, where, order, desc)


@app.post("/api/sql/format", dependencies=A)
def sql_format(body: dict):
    """{sql, case: upper|lower, conn? | dialect?}: pretty-printed, keywords in that case."""
    from . import sqltools as ST

    return {"sql": ST.format_sql(str(body.get("sql", "")), body.get("case", "upper"), ST._dialect(ctx(), body.get("conn"), body.get("dialect")))}


@app.post("/api/sql/transpile", dependencies=A)
def sql_transpile(body: dict):
    """{sql, to, conn? | from?}: the query in another engine's dialect, with warnings for what didn't carry over."""
    from . import sqltools as ST

    return ST.transpile_sql(str(body.get("sql", "")), ST._dialect(ctx(), body.get("conn"), body.get("from")), str(body.get("to", "")))


@app.post("/api/sql/explain", dependencies=A)
def sql_explain(body: dict):
    """{conn, sql}: the engine's plan, through the read-only guard."""
    from . import sqltools as ST

    return ST.explain(ctx(), body["conn"], str(body.get("sql", "")))


@app.get("/api/sql/history", dependencies=A)
def sql_history(conn: str | None = None, status: str | None = None, source: str | None = None, q: str | None = None,
                days: int = 30, limit: int = 200):
    """Queries run through Captain, newest first. status: ok | failed. source: app | notebook | preview | cli."""
    from . import sqltools as ST

    return ST.history(ctx(), conn, status, source, q, max(1, min(days, 365)), max(1, min(limit, 1000)))


@app.get("/api/snippets", dependencies=A)
def snippets_list(engine: str | None = None, q: str | None = None):
    from . import sqltools as ST

    return ST.snippets(ctx(), engine, q)


@app.post("/api/snippets", dependencies=A)
def snippets_add(body: dict):
    """{title, sql, description?, engine?, golden?}. {{name}} in the SQL becomes a field when it's inserted."""
    from . import sqltools as ST

    return ST.save_snippet(ctx(), body)


@app.put("/api/snippets/{sid}", dependencies=A)
def snippets_change(sid: str, body: dict):
    from . import sqltools as ST

    return ST.save_snippet(ctx(), body, sid)


@app.delete("/api/snippets/{sid}", dependencies=A)
def snippets_delete(sid: str):
    from . import sqltools as ST

    return ST.delete_snippet(ctx(), sid)


@app.post("/api/sql", dependencies=A)
def sql(body: dict):
    from .connectors.sql import run_sql

    c = ctx()
    conn = c.load_connection(body["conn"])
    from .metastore import record_query

    try:
        r = run_sql(conn, body["sql"], limit=body.get("limit"))
    except Exception:
        record_query(c, body["conn"], body["sql"], source="app", ok=False)  # query history shows failures too
        raise
    record_query(c, body["conn"], body["sql"], rows=r.table.num_rows, ms=r.elapsed_ms, source="app")
    from .data.cells import columns_from_arrow

    out = {"columns": columns_from_arrow(r.table.schema),
           "rows": [list(map(_jsonable, row.values())) for row in r.table.slice(0, 1000).to_pylist()],
           "row_count": r.table.num_rows, "truncated": r.truncated, "ms": r.elapsed_ms}
    if body.get("save"):
        m = ds.snapshot(c, body["save"], r.table, kind=DatasetKind(body.get("kind", "generic")),
                        provenance=Provenance(source="sql", connection=body["conn"], sql=body["sql"]),
                        description=body.get("description", ""), truncated=r.truncated)
        out["dataset"] = ops.ref(m)
        if body.get("project"):
            from . import projects as PR

            PR.assign(c, body["project"], [{"kind": "dataset", "name": m.name}])
    return out


def _jsonable(v):
    from .data.cells import cell

    return cell(v)


@app.get("/api/datasets", dependencies=A)
def datasets(kind: str | None = None, cls: str | None = Query(None, alias="class"), project: str | None = None,
             q: str | None = None):
    """Every dataset, with its columns' names. q: every word in its name, description or a column's name."""
    from . import classes as CL
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "dataset")
    out = []
    for n in c.registry.names("dataset"):
        if not inp(n):
            continue
        m = c.registry.load("dataset", n)
        if kind and m.dataset_kind.value != kind:
            continue
        mine = CL.classes_of(c, n)
        if cls and cls not in mine:
            continue
        cols = [f.get("name") for f in (m.schema_fields or []) if isinstance(f, dict) and f.get("name")]
        if q and not all(w in " ".join([n, m.description or "", *cols]).lower() for w in q.lower().split()):
            continue
        out.append({"name": n, "latest": m.version, "kind": m.dataset_kind.value, "classes": mine, "rows": m.row_count,
                    "by": m.created_by, "created": m.created_at.isoformat(), "description": m.description,
                    "columns": cols})
    return out


@app.get("/api/dataset-classes", dependencies=A)
def dataset_classes(project: str | None = None):
    """Classes and their dataset counts. Counts follow ?project= and the Explorer's X-CST-Scope, like the lists."""
    from . import classes as CL

    c = ctx()
    return {"classes": CL.listing(c, _member(c, project, "dataset")), "roles": CL.ROLES}


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
def dataset_tree(cls: str | None = Query(None, alias="class"), project: str | None = None):
    """The workspace as folders, the way the person organized it (see library.py). ?project= shows one project's."""
    from . import classes as CL
    from . import library as LIB
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "dataset")

    def entry(n):
        if (cls and cls not in CL.classes_of(c, n)) or not inp(n):
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
    project = body.get("project")
    return {"job": _job(lambda p: IM.import_paths(c, paths, body.get("folder", ""), progress=p, project=project),
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


@app.get("/api/registries/discover", dependencies=A)
def registries_discover(profile: str | None = None):
    """Every S3 bucket the login can see, each marked as a registry, empty, other, or unreadable. Reads only."""
    from . import attach as AT
    from . import registry_setup as RS

    c = ctx()
    if profile is None:
        _, profile = AT.resolve(c, "s3://x", None)
    return {**RS.discover(c, profile), "profile": profile}


@app.get("/api/registries/template", dependencies=A)
def registries_template(bucket: str, region: str = "us-west-1", format: str = "terraform"):  # noqa: A002
    from . import registry_setup as RS

    return RS.template(bucket, region, format)


@app.post("/api/registries/create", dependencies=A)
def registries_create(body: dict):
    """Make a registry bucket through a CloudFormation stack, then attach it. Runs as a job."""
    from . import attach as AT
    from . import registry_setup as RS

    c = ctx()
    bucket = RS.check_name(str(body.get("bucket", "")))
    region = str(body.get("region") or "us-west-1")
    profile = body.get("profile") or None
    if profile is None:
        _, profile = AT.resolve(c, f"s3://{bucket}", None)
    return {"job": _job(lambda p: RS.create(c, bucket, region, profile, body.get("label"), progress=p),
                        f"create registry {bucket}")}


@app.delete("/api/registries", dependencies=A)
def registries_detach(uri: str):
    from . import attach as AT

    return AT.detach(ctx(), uri)


@app.put("/api/datasets/{name:path}/classes", dependencies=A)
def dataset_classify(name: str, body: dict):
    from . import classes as CL

    return CL.classify(ctx(), name, body.get("classes") or [])


@app.get("/api/datasets/{name:path}/insights", dependencies=A)
def dataset_insights(name: str):
    """What uses this dataset (pipelines, runs, environments, datasets made from it), the notebooks that name it, who
    works with it, and what happened to it lately. From Captain's own records."""
    from .data import insights as IN

    return IN.dataset_insights(ctx(), name)


def _ws_profile(c):
    from .workspace import load_config

    return (load_config().get("workspaces", {}).get(c.workspace.name) or {}).get("profile")


@app.get("/api/datasets/{name:path}/media", dependencies=A)
def dataset_media(name: str, v: int | None = None, file: str | None = None):
    """The dataset's image columns, and where relative ones live (saved, else a guess Captain checked)."""
    from . import browse
    from . import media as MD

    c = ctx()
    m = c.registry.load("dataset", name, v)
    prof = _ws_profile(c)
    page = browse.rows(c, m, file, 0, 20, None, False, None, None, False, prof)
    cols = [x["name"] if isinstance(x, dict) else x for x in page.get("columns", [])]
    rows = page.get("rows", [])
    saved = MD.templates(c, m.name)
    out = []
    for d in MD.detect(cols, rows):
        d["template"] = saved.get(d["column"])
        d["guessed"] = False
        out.append(d)
    return {"columns": out}


@app.post("/api/datasets/{name:path}/media/locate", dependencies=A)
def dataset_media_locate(name: str, body: dict):
    """Find where a column's bare image names live (siblings' addresses, next to the file, the login's buckets), and
    save it when found. Runs as a job; the result says where, or what was searched."""
    from . import browse
    from . import media as MD

    c = ctx()
    m = c.registry.load("dataset", name, body.get("v"))
    col, file = str(body.get("column") or ""), body.get("file")
    prof = _ws_profile(c)

    def go(p):
        page = browse.rows(c, m, file, 0, 20, None, False, None, None, False, prof)
        cols = [x["name"] if isinstance(x, dict) else x for x in page.get("columns", [])]
        rows = [dict(zip(cols, r)) for r in page.get("rows", [])]
        t = next((x for x in browse.tables(c, m) if not file or x["file"] == file), None)
        r = MD.locate(c, m.name, t["uri"] if t else None, cols, rows, col, prof, p)
        if r.get("template"):
            MD.set_template(c, m.name, col, r["template"])
        return r

    return {"job": _job(go, f"looking for {m.name}'s pictures")}


@app.put("/api/datasets/{name:path}/media", dependencies=A)
def dataset_media_set(name: str, body: dict):
    """Say where a column's relative images live. Checked on the first row's image before it's kept."""
    from . import browse
    from . import media as MD

    c = ctx()
    m = c.registry.load("dataset", name, body.get("v"))
    col, template = str(body.get("column") or ""), (body.get("template") or "").strip() or None
    if body.get("folder") and not template:
        # A folder picked from the suggestions: find the layout inside it.
        prof = _ws_profile(c)
        page = browse.rows(c, m, body.get("file"), 0, 20, None, False, None, None, False, prof)
        cols = [x["name"] if isinstance(x, dict) else x for x in page.get("columns", [])]
        template = MD.from_folder(str(body["folder"]), cols, [dict(zip(cols, r)) for r in page.get("rows", [])], col, prof)
        if not template:
            raise ValueError(f"No image for the first rows under {body['folder']}, as <folder>/<id>/<name> or <folder>/<name>.")
    if template:
        prof = _ws_profile(c)
        page = browse.rows(c, m, body.get("file"), 0, 20, None, False, None, None, False, prof)
        cols = [x["name"] if isinstance(x, dict) else x for x in page.get("columns", [])]
        if col not in cols:
            raise ValueError(f"no column {col!r} in {m.name}")
        j = cols.index(col)
        first = next((r for r in page.get("rows", []) if MD._items(r[j])), None)
        if first is not None:
            src = MD.fill(template, MD._items(first[j])[0], dict(zip(cols, first)))
            if not MD._exists(src, prof):
                raise ValueError(f"There's no image at {src}. Check the address.")
    return {"column": col, "template": template, "saved": MD.set_template(c, m.name, col, template)}


@app.post("/api/media/thumbs", dependencies=A)
def media_thumbs(body: dict):
    """Thumbnails for a page of image addresses, saved in the app's cache/thumbs folder."""
    from . import media as MD

    return MD.thumbs([str(x) for x in body.get("srcs") or []], str(body.get("dest") or ""), int(body.get("size") or 160),
                     _ws_profile(ctx()))


@app.get("/api/datasets/{name:path}/rows", dependencies=A)
def dataset_rows(name: str, v: int | None = None, file: str | None = None, offset: int = 0, limit: int = 100,
                 order: str | None = None, desc: bool = False, search: str | None = None, where: str | None = None,
                 count: bool = False):
    from . import browse

    c = ctx()
    m = c.registry.load("dataset", name, v)
    from .workspace import load_config

    profile = (load_config().get("workspaces", {}).get(c.workspace.name) or {}).get("profile")
    out = browse.rows(c, m, file, offset, limit, order, desc, search, where, count, profile)
    from .data import reads as RD

    RD.record(c, m.name, m.version, "app")  # who reads it (Insights); once per reader per 10 minutes
    return out


@app.get("/api/datasets/{name:path}/head", dependencies=A)
def dataset_head(name: str, v: int | None = None, n: int = 50):
    c = ctx()
    m = c.registry.load("dataset", name, v)
    from .data import reads as RD

    RD.record(c, m.name, m.version, "app")
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
def pipelines(project: str | None = None):
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "pipeline")
    out = []
    for n in c.registry.names("pipeline"):
        if not inp(n):
            continue
        m = c.registry.load("pipeline", n)
        train = (m.spec or {}).get("train")
        out.append({"name": n, "latest": m.version, "engine": m.engine, "repro_status": m.repro_status.value,
                    "by": m.created_by, "created": m.created_at.isoformat(), "description": m.description,
                    "trains": isinstance(train, dict) and bool(train.get("command")),
                    # the place its training command declares, so a retrain opens on it
                    "train_compute": train.get("compute") if isinstance(train, dict) else None})
    return out


@app.post("/api/pipelines/{name:path}/retrain", dependencies=A)
def pipeline_retrain(name: str, body: dict | None = None):
    """Retrain from the pipeline's declared training command, here or on a compute target, streaming to Runs.
    Starting it from the app is the person's go-ahead to run the command here (the CLI's --local-exec).
    Body: {compute?, inputs?: [ref or role=ref], params?: {}, as_of?, project?}. With project, the new version is
    recorded as that project's experiment when it lands here. On a compute target the job's result is
    {pending: true, job, live_id}: the new version appears when that job finishes."""
    from . import compute as CMP
    from . import live as LV
    from . import retrain as RT

    c = ctx()
    pm = c.registry.load("pipeline", name)
    train = (pm.spec or {}).get("train") or {}
    if not isinstance(train, dict) or not train.get("command"):
        raise ValueError(f"{pm.name} declares no training command (spec.train.command), so there's nothing to run.")
    compute = (body or {}).get("compute") or train.get("compute") or "local"
    local = CMP.get(c, compute)["kind"] == "local"
    lv = LV.start("training", f"Retrain {pm.name}", of=f"{pm.name}@v{pm.version}") if local else None
    live_id = None if local else LV.start_remote(c, "training", f"Retrain {pm.name}", of=f"{pm.name}@v{pm.version}", where=compute)

    def go(p):
        try:
            b = body or {}
            inputs = b.get("inputs") or None
            if inputs is None and b.get("project"):
                # From a project page: train on the project's own inputs, live ones read again now.
                from . import project_inputs as PI

                inputs = PI.run_inputs(c, b["project"]) or None
            r = RT.retrain(c, name, compute, local_exec=True, live=lv, live_id=live_id, inputs=inputs,
                           params=b.get("params") or None, as_of=b.get("as_of") or None)
        except Exception as e:
            if lv:
                lv.end("failed", error=str(e))
            raise
        if lv:
            lv.end("done", ref=r.get("pipeline"), summary=f"Published {r['pipeline']} from {r['parent']} in {r['wall_s']:.0f} s")
        project = (body or {}).get("project")
        if project and r.get("pipeline") and not r.get("pending"):
            # Started from a project page: the new version is that project's experiment, so its row appears there.
            from . import project_state as PS

            try:
                r["experiment"] = PS.experiment_add(c, project, r["pipeline"], trainer=str(train.get("command"))[:200])["id"]
            except Exception as e:  # noqa: BLE001  the version stands; the person can add it to the project by hand
                r["experiment_error"] = str(e)
        return r

    jid = _job(go, f"retraining {pm.name} on {compute}")
    rid = lv.id if lv else live_id
    JOBS[jid]["live"] = rid
    return {"job": jid, "live": rid}


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

    from . import live as LV

    c = ctx()
    doc = PR.get(c, pid)
    lv = LV.start("backtest", doc.get("title") or pid, of=f"proposals/{pid[:10]}", arms=list(doc.get("arms") or []))

    def go(p):
        try:
            out = PR.run(c, pid, progress=p, live=lv)
        except Exception as e:
            lv.end("failed", error=str(e))
            raise
        lv.end("done", ref=out.get("run"), summary=f"Ran the recommendation: {doc.get('title') or pid}")
        return f"ran {out['run']}"

    return {"job": _job(go, f"running proposal {pid}"), "live": lv.id}


@app.get("/api/grading", dependencies=A)
def grading_list(project: str | None = None):
    """Grading queues. Under an Explorer pick or ?project=: queues linked there, or grading a pipeline that is."""
    from . import grading as GR

    c = ctx()
    out = GR.listing(c)
    if not (project or _req_scope.get()):
        return out
    own, pipe = _member(c, project, "grading"), _member(c, project, "pipeline")
    return [g for g in out if own(g["name"]) or any(pipe(p.split("@")[0]) for p in g.get("pipelines", []))]


@app.post("/api/grading", dependencies=A)
def grading_create(body: dict):
    """Start a grading queue: the products these pipelines return for these queries, for people to grade."""
    from . import grading as GR

    pipes = body.get("pipelines") or []
    if isinstance(pipes, str):
        pipes = [p.strip() for p in pipes.split(",") if p.strip()]
    if not pipes:
        raise ValueError("pick at least one model version whose results people will rate")
    return GR.create(ctx(), body.get("name", ""), body.get("queries", ""), pipes, int(body.get("depth") or 10),
                     int(body["sample"]) if body.get("sample") else None, float(body.get("overlap", 0.1)),
                     rubric=body.get("rubric") or None)


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
def investigations_list(state: str | None = None, q: str | None = None, project: str | None = None):
    from . import investigations as INV
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "investigation")
    return [i for i in INV.listing(c, state, q) if inp(i["id"])]


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
                        f"running experiment {iid}")}


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
def signals_list(project: str | None = None):
    """Candidate signals. Under an Explorer pick or ?project=: signals linked there, or recorded as evidence on an
    investigation that is."""
    from . import investigations as INV

    c = ctx()
    out = INV.signals(c)
    if not (project or _req_scope.get()):
        return out
    own, inv = _member(c, project, "signal"), _member(c, project, "investigation")
    via = {}
    for i in INV.listing(c):
        if inv(i["id"]):
            for s in (INV.get(c, i["id"]).get("signals") or []):
                via[s] = True
    return [s for s in out if own(s["id"]) or via.get(s["id"])]


@app.get("/api/runs", dependencies=A)
def runs_list(backtest: str | None = None, project: str | None = None, limit: int = 200):
    """Every backtest run, newest first, under ?project= and the Explorer's pick (by its backtest or arms)."""
    c = ctx()
    bt_in, pipe_in = _member(c, project, "backtest"), _member(c, project, "pipeline")
    scoped = bool(project or _req_scope.get())
    out = []
    for n in c.registry.names("backtest-run"):
        if backtest and n != backtest:
            continue
        try:
            bt = c.registry.load("backtest", n)
        except Exception:  # noqa: BLE001
            bt = None
        if scoped and not (bt_in(n) or (bt and any(pipe_in(a.split("@")[0]) for a in bt.arms))):
            continue
        for v in c.registry.versions("backtest-run", n):
            r = c.registry.load("backtest-run", n, v)
            out.append({"name": n, "version": v, "created": r.created_at.isoformat(), "mode": r.mode,
                        "compute": r.compute, "arms": [a.arm for a in r.arms], "complete": r.complete,
                        "wall_s": r.wall_s, "by": r.created_by})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out[: max(1, min(limit, 2000))]


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
def backtests(project: str | None = None):
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "backtest")
    out = []
    for n in c.registry.names("backtest"):
        if not inp(n):
            continue
        m = c.registry.load("backtest", n)
        runs = c.registry.versions("backtest-run", n)
        lr = c.registry.load("backtest-run", n, runs[-1]) if runs else None
        out.append({"name": n, "latest": m.version, "arms": m.arms, "primary_metric": m.preregistration.primary_metric,
                    "runs": len(runs), "created": m.created_at.isoformat(), "mode": m.mode,
                    "last_run": lr.created_at.isoformat() if lr else None,
                    # A run is saved only when it finished. `complete` means its top results are graded enough
                    # (judged@10 of 0.9 or more) to trust relevance metrics, not that it finished.
                    "last_run_graded": lr.complete if lr else None})
    out.sort(key=lambda x: x["last_run"] or x["created"], reverse=True)  # newest first: latest run, else creation
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


def _start_health_watch(every_s: float = 60.0) -> threading.Thread:
    """While the service runs, check every deployed model once a minute and record the result. A model that turns
    unhealthy sends its message from here, whether or not a window is open on it."""
    def loop():
        import time

        from .staging import health as HL

        while True:
            time.sleep(every_s)
            try:
                HL.watch_once(ctx(), system=True)  # the app's service: a change is a notification on this computer
            except Exception:  # noqa: BLE001  the watch never takes the service down
                pass

    th = threading.Thread(target=loop, daemon=True, name="cst-health-watch")
    th.start()
    return th


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
    from . import settings as ST

    compute = (body or {}).get("compute") or bt.compute or ST.default_compute(c, "backtest") or "local"
    from . import live as LV

    from . import compute as CMP

    # A run here streams to Runs from this process. One on a compute target streams through the workspace bucket:
    # its run is made now, queued, and the job's container writes to it.
    local = CMP.get(c, compute)["kind"] == "local"
    lv = LV.start("backtest", name, of=name, arms=list(bt.arms)) if local else None
    live_id = None if local else LV.start_remote(c, "backtest", name, of=name, arms=list(bt.arms), where=compute)

    def go(p):
        try:
            r = ops.backtest_launch(c, name, compute, progress=p, live=lv, live_id=live_id)
        except Exception as e:
            if lv:
                lv.end("failed", error=str(e))
            if live_id:
                LV.note_remote(c, live_id, status="failed", error=f"Couldn't submit the job: {e}")
            raise
        if live_id and r.get("job"):
            LV.note_remote(c, live_id, job=r["job"])
        out = r.get("run") or r
        if lv:
            lv.end("done", ref=out if isinstance(out, str) else None, summary=f"Finished {name}")
        return out

    jid = _job(go, f"running {name} on {compute}")
    JOBS[jid]["compute"] = compute
    rid = lv.id if lv else live_id
    JOBS[jid]["live"] = rid
    return {"job": jid, "live": rid}


@app.get("/api/live", dependencies=A)
def live_list(active: bool = False, limit: int = 30):
    """Live runs, newest first: on this computer, and on compute targets (through the workspace bucket)."""
    from . import live as LV

    limit = min(limit, 200)
    rows = LV.listing(limit=limit)
    try:
        rows += LV.listing_remote(ctx(), limit=limit)
    except Exception:  # noqa: BLE001  a bucket that can't be listed leaves this computer's runs
        pass
    if active:
        rows = [r for r in rows if r.get("status") in ("running", "queued")]
    return sorted(rows, key=lambda r: r.get("started", ""), reverse=True)[:limit]


@app.get("/api/live/{rid}", dependencies=A)
def live_read(rid: str, since: int = 0):
    from . import live as LV

    c = ctx()
    return LV.read_remote(c, rid, since=max(0, since)) if LV.is_remote(c, rid) else LV.read(rid, since=max(0, since))


@app.get("/api/jobs/{jid}", dependencies=A)
def job(jid: str):
    """A job started by this daemon (progress of a local run), or else a recorded compute job (jobs.py)."""
    if jid in JOBS:
        return JOBS[jid]
    from . import jobs as J

    return J.show(ctx(), jid)


@app.get("/api/explain", dependencies=A)
def explain_query(pipeline: str, q: str, vs: str | None = None, query_id: str | None = None,
                  judgments: str | None = None, k: int = 50):
    """Why a pipeline ranked this: a trace per stage. With vs=<pipeline>, both traces and the first stage where a
    relevant result was lost (needs query_id and judgments for grades)."""
    from . import explain as EX

    c = ctx()
    k = max(1, min(int(k), 200))
    if vs:
        return EX.compare_traces(c, pipeline, vs, q, query_id, judgments, k)
    return EX.trace(c, pipeline, q, query_id, judgments, k)


@app.post("/api/runs/{name:path}/profile", dependencies=A)
def run_profile(name: str, body: dict | None = None):
    """Time and memory per stage for each replayable arm over the run's queries, with budgets and the p99 change
    against the baseline. Offline replay on this computer. Runs as a job. {max_queries?, memory?}"""
    from . import profile as PF

    c = ctx()
    b = body or {}
    return {"job": _job(lambda p: PF.profile_run(c, name, int(b.get("max_queries", 200)), bool(b.get("memory", True)),
                                                 progress=p), f"profile {name}")}


@app.post("/api/profile/pipeline", dependencies=A)
def pipeline_profile(body: dict):
    """{pipeline, queries: dataset ref (with :split) or [query text], memory?}. Runs as a job."""
    from . import ops
    from . import profile as PF

    c = ctx()
    qsrc = body.get("queries")
    if isinstance(qsrc, list):
        qs = [{"query": str(x)} for x in qsrc]
    elif isinstance(qsrc, str):
        qs = ops.rows_of(c, qsrc)[1]
    else:
        raise ValueError("give queries: a dataset ref or a list of query texts")
    return {"job": _job(lambda p: PF.profile_pipeline(c, body["pipeline"], qs, memory=bool(body.get("memory", True)),
                                                      progress=p), f"profile {body.get('pipeline')}")}


@app.get("/api/runs/{name:path}/queries/{qid}/explain", dependencies=A)
def run_query_explain(name: str, qid: str, a: str | None = None, b: str | None = None, k: int = 50):
    """Explain one query of a comparison run, both arms, with its grades and the first divergence."""
    from . import explain as EX

    return EX.from_run(ctx(), name, qid, a, b, max(1, min(int(k), 200)))


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


@app.get("/api/regression", dependencies=A)
def regression(model: str | None = None, project: str | None = None, limit: int = 50):
    """Regression testing: every evaluation run, newest first, with the model judged, the model it is compared
    with and the change on the primary metric. ?model= keeps one model's runs. See compare.regression."""
    from . import compare as CP

    c = ctx()
    return CP.regression(c, _member(c, project, "backtest"), model, limit)


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

    def page(msg):
        # The grid's page of a result: {type: "page", req, var, offset, limit, order?, desc?, where?, count?}. Its
        # answer comes back as {type: "page", cell_id: req, ...one row shape} (or an error), then done.
        with lock:
            req = str(msg.get("req") or "page")
            try:
                code = (f"__cst.page({str(msg.get('var') or '')!r}, {int(msg.get('offset') or 0)}, {int(msg.get('limit') or 100)}, "
                        f"{msg.get('order')!r}, {bool(msg.get('desc'))}, {msg.get('where')!r}, {bool(msg.get('count'))})")
                for ev in k.execute(req, code):
                    if ev.get("type") != "status":  # a page is not the notebook running
                        loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "cell_id": req, "ename": type(e).__name__, "evalue": str(e), "traceback": []})
                loop.call_soon_threadsafe(q.put_nowait, {"type": "done", "cell_id": req, "ms": 0})

    async def pump():
        while True:
            await ws.send_json(await q.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "execute":
                threading.Thread(target=run, args=(msg,), daemon=True).start()
            elif msg.get("type") == "page":
                threading.Thread(target=page, args=(msg,), daemon=True).start()
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


@app.get("/api/engine-templates", dependencies=A)
def engine_templates():
    """What a new pipeline can rank with, and a starting spec for each. (It was GET /api/engines, which the
    persistent engines route above already answered, so this one was never reached.)"""
    labels = {"local-bm25": "Local BM25 (in process)", "http": "Any HTTP search service", "elastic": "Elasticsearch / OpenSearch",
              "typesense": "Typesense", "algolia": "Algolia", "captain": "Captain /v1/rank",
              "bm25-ltr": "Local BM25, then a trained ranker on the top results"}
    return [{"engine": e, "label": labels.get(e, e), "template": t, "needs_catalog": e in ("local-bm25", "bm25-ltr")}
            for e, t in ops.TEMPLATE.items()]


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
    """{name, arms, queries?, judgments?, population?, hypothesis, primary?, mde?, mode?, logs?, compute?, surfaces?,
    population_size?}. population_size is how many simulated shoppers a replay of logged searches makes (4000 unless
    given): a limit the form shows as a field rather than hiding."""
    return ops.backtest_plan(ctx(), body["name"], body["arms"], body.get("queries") or None, body.get("judgments") or None,
                             body.get("population") or None, body["hypothesis"], body.get("primary", "ndcg@10"),
                             float(body.get("mde", 0.003)), mode=body.get("mode", "simulated"), logs=body.get("logs") or None,
                             compute=body.get("compute") or None, surfaces=body.get("surfaces") or None,
                             population_size=int(body.get("population_size") or 4000)).model_dump(mode="json")


@app.get("/api/evaluations/options", dependencies=A)
def evaluation_options():
    """What the New evaluation form offers, straight from what a run can do: the measures (each with one line and
    what it needs), the fixed depths they look at, the slices a run always reports, and the limits with their
    defaults. Nothing here is a setting the runner ignores."""
    from . import testsets as TS
    from .eval import evalform as EF

    return EF.options() | {"grades": TS.GRADE_WORDS}


@app.post("/api/testsets/preview", dependencies=A)
def testset_preview(body: dict):
    """{source: {dataset | file}, mapping?: {query, product, grade?, slice?}, default_grade?, limit?}: the table's
    columns and first rows, the mapping (guessed when not given), and what it would make. Writes nothing."""
    from . import testsets as TS

    return TS.preview(ctx(), body.get("source") or {}, body.get("mapping") or None, int(body.get("default_grade", 3)),
                      max(1, min(int(body.get("limit", 8)), 50)))


@app.post("/api/testsets", dependencies=A)
def testset_create(body: dict):
    """{name, source, mapping, default_grade?}: make <name>/questions and <name>/answers from a table of the person's
    own questions and wanted products. Returns {queries, judgments, questions, answers, skipped}."""
    from . import testsets as TS

    return TS.from_pairs(ctx(), body["name"], body.get("source") or {}, body.get("mapping") or {}, int(body.get("default_grade", 3)))


@app.post("/api/registry/import", dependencies=A)
def registry_import(body: dict):
    from . import models as MD

    c = ctx()
    return {"job": _job(lambda p: (lambda s: f"{s.name}@v{s.version}")(MD.import_registry(c, body.get("uri") or None,
                                                                                         body.get("profile") or None)),
                        "importing the model registry")}


@app.get("/api/models", dependencies=A)
def models_list(project: str | None = None):
    from . import models as MD
    from . import projects as PR

    c = ctx()
    inp = _member(c, project, "model")
    return [m for m in MD.listing(c) if inp(m["id"], brand=m.get("brand"))]


@app.post("/api/models/pull", dependencies=A)
def models_pull(body: dict):
    """Download a model version into a folder, checked against its SHA256SUMS. Runs as a job."""
    from . import models as MD

    c, ref = ctx(), str(body.get("model") or "")
    m, _ = MD.find(c, ref)  # fail now on an unknown model, not inside the job
    dest = body.get("dest") or os.path.expanduser(f"~/Downloads/{m['brand']}-{m['version']}")
    return {"job": _job(lambda p: MD.pull(c, m["id"], dest, body.get("parts"), body.get("profile"), p),
                        f"Download {ref}")}


@app.post("/api/models/publish", dependencies=A)
def models_publish(body: dict):
    """Publish a pipeline version to a model registry as a write-once <arch>/<brand>/vN. Runs as a job.
    Body: {pipeline: name@vN, arch, brand, registry?, profile?, status?}."""
    from . import model_versions as MV

    c, pipe = ctx(), str(body.get("pipeline") or "")
    for k in ("arch", "brand"):
        if not body.get(k):
            raise ValueError(f"{k} is required")
    pm, _ = c.registry.resolve("pipeline", pipe)  # fail now on an unknown pipeline, not inside the job
    pin = f"{pm.name}@v{pm.version}"
    return {"job": _job(lambda p: MV.publish(c, pin, body["arch"], body["brand"], body.get("registry") or None,
                                             body.get("profile") or None, body.get("status") or "candidate", p),
                        f"Publish {pin} to the registry")}


@app.post("/api/models/import-version", dependencies=A)
def models_import_version(body: dict):
    """Register a model trained elsewhere as a new pipeline version. Runs as a job (it may call MLflow).
    Body: {pipeline, mlflow?: name@version, mlflow_run?, registry_path?: arch/brand/version, from?, description?}."""
    from . import model_versions as MV

    c = ctx()

    def go(p):
        v = MV.import_version(c, str(body.get("pipeline") or ""), body.get("mlflow") or None, body.get("mlflow_run") or None,
                              body.get("registry_path") or None, body.get("from") or None, body.get("description") or None)
        return {"pipeline": f"{v.name}@v{v.version}", "source": v.source, "metrics": v.train_metrics, "parents": v.parents}

    return {"job": _job(go, f"Import a model version into {body.get('pipeline')}")}


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
    _start_health_watch()
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
