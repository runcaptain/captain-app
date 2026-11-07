"""MLflow bridge (spec: "MLflow interoperability"): read a team's MLflow without asking them to migrate.

Read-only, through MLflow's REST API (2.0), so it needs no mlflow package:
- an MLflow tracking server: `uri` like https://mlflow.example.com, with an optional token kept as a secret;
- Databricks' managed MLflow: name an existing Databricks connection, and its host and token are reused.

What it reads: experiments, runs (status, times, metrics, params, tags), and registered models with their versions and
the run each version came from. A model version can be linked to a Captain pipeline, which puts the MLflow run and
the model version into Captain's lineage, upstream of the pipeline and the backtests that compared it.

Captain writes nothing to MLflow in this version. Which system is authoritative for a record is answered simply:
MLflow is, for its runs and models, and Captain is, for its comparisons and decisions.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlparse

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "mlflow.yaml"


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(KEY)
        return yaml.safe_load(body) or {}, tag
    except NotFound:
        return {}, None


def _write(ctx: Context, change):
    for _ in range(10):
        doc, tag = _read(ctx)
        out = change(doc)
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            continue
    raise Conflict("the MLflow settings kept changing while saving. Try again.")


def configure(ctx: Context, uri: str | None = None, token_secret: str | None = None,
              databricks_connection: str | None = None) -> dict:
    if not uri and not databricks_connection:
        raise ValueError("give the MLflow tracking address, or a Databricks connection to use its managed MLflow")
    if databricks_connection:
        ctx.load_connection(databricks_connection)  # must exist

    def ch(doc):
        doc.update({"uri": (uri or "").rstrip("/") or None, "token_secret": token_secret,
                    "databricks_connection": databricks_connection, "configured_by": whoami(),
                    "configured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")})
        doc.setdefault("links", {})
        return {k: doc[k] for k in ("uri", "databricks_connection")}

    return _write(ctx, ch)


def _endpoint(ctx: Context) -> tuple[str, dict]:
    from .workspace import get_secret, render_secrets

    doc, _ = _read(ctx)
    if not doc:
        raise ValueError("MLflow isn't set up for this workspace. Configure it first.")
    if doc.get("databricks_connection"):
        conn = ctx.load_connection(doc["databricks_connection"])
        u = urlparse(render_secrets(conn.url))
        token = u.password or ""
        return f"https://{u.hostname}", {"Authorization": f"Bearer {token}"}
    headers = {"Authorization": f"Bearer {get_secret(doc['token_secret'])}"} if doc.get("token_secret") else {}
    return doc["uri"], headers


def _call(ctx: Context, method: str, path: str, body: dict | None = None) -> dict:
    import httpx

    base, headers = _endpoint(ctx)
    url = f"{base}/api/2.0/mlflow/{path}"
    try:
        r = httpx.request(method, url, json=body if method != "GET" else None,
                          params=body if method == "GET" else None, headers=headers, timeout=60)
    except httpx.HTTPError as e:
        raise ConnectionError(f"Couldn't reach MLflow at {base}: {type(e).__name__}") from None
    if r.status_code in (401, 403):
        try:
            said = str(r.json().get("message") or "").split(" [ReqId")[0]
        except ValueError:
            said = ""
        raise PermissionError("MLflow refused the login" + (f": {said}." if said else ".") + " For Databricks, make a "
                              "token with the mlflow scope (a SQL-only token can't read experiments).")
    if r.status_code == 404:
        raise NotFound(f"MLflow has no {path.split('/')[0]} at {base}: is this an MLflow tracking server?")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(f"MLflow said: {str(msg)[:300]}")
    return r.json()


def _ts(ms) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def experiments(ctx: Context, limit: int = 200) -> list[dict]:
    out = _call(ctx, "POST", "experiments/search", {"max_results": min(limit, 1000)})
    return [{"id": e["experiment_id"], "name": e.get("name"), "stage": e.get("lifecycle_stage"),
             "updated": _ts(e.get("last_update_time"))} for e in out.get("experiments", [])]


def runs(ctx: Context, experiment_id: str, limit: int = 100, order: str = "attributes.start_time DESC") -> list[dict]:
    out = _call(ctx, "POST", "runs/search", {"experiment_ids": [experiment_id], "max_results": min(limit, 1000),
                                              "order_by": [order]})
    res = []
    for r in out.get("runs", []):
        info, data = r.get("info", {}), r.get("data", {})
        res.append({"run_id": info.get("run_id"), "name": info.get("run_name"), "status": info.get("status"),
                    "started": _ts(info.get("start_time")), "ended": _ts(info.get("end_time")),
                    "metrics": {m["key"]: m["value"] for m in data.get("metrics", [])},
                    "params": {p["key"]: p["value"] for p in data.get("params", [])},
                    "tags": {t["key"]: t["value"] for t in data.get("tags", []) if not t["key"].startswith("mlflow.")},
                    "artifacts": info.get("artifact_uri")})
    return res


def status(ctx: Context) -> dict:
    """Where Captain reads MLflow from, and its links, without calling MLflow. No secrets."""
    doc, _ = _read(ctx)
    return {"configured": bool(doc), "uri": doc.get("uri"), "databricks_connection": doc.get("databricks_connection"),
            "links": doc.get("links", {})}


def models(ctx: Context, limit: int = 200) -> list[dict]:
    out = _call(ctx, "GET", "registered-models/search", {"max_results": min(limit, 1000)})
    res = []
    for m in out.get("registered_models", []):
        versions = _call(ctx, "GET", "model-versions/search", {"filter": "name='" + m["name"].replace("'", "\\'") + "'"}).get("model_versions", [])
        res.append({"name": m["name"], "description": m.get("description"), "updated": _ts(m.get("last_updated_timestamp")),
                    "versions": [{"version": v["version"], "run_id": v.get("run_id"), "status": v.get("status"),
                                  "stage": v.get("current_stage"), "source": v.get("source"),
                                  "created": _ts(v.get("creation_timestamp")),
                                  "aliases": v.get("aliases") or []} for v in versions]})
    return res


def link(ctx: Context, model: str, version: str, pipeline: str) -> dict:
    """Say which Captain pipeline serves an MLflow model version, for lineage."""
    ctx.registry.resolve("pipeline", pipeline)

    def ch(doc):
        doc.setdefault("links", {})[f"{model}@{version}"] = pipeline.split("@")[0]
        return {"model": model, "version": version, "pipeline": pipeline.split("@")[0]}

    return _write(ctx, ch)


def links(ctx: Context) -> dict:
    return (_read(ctx)[0] or {}).get("links", {})


def lineage(ctx: Context) -> dict:
    """Nodes and edges for linked model versions: MLflow run -> model version -> Captain pipeline. Run ids come from
    the links' MLflow records when MLflow can be reached, else the link alone."""
    lk = links(ctx)
    nodes, edges = {}, []
    if not lk:
        return {"nodes": [], "edges": []}
    try:
        reg = {(m["name"], v["version"]): v for m in models(ctx) for v in m["versions"]}
    except Exception:  # noqa: BLE001  lineage still draws from the links when MLflow is unreachable
        reg = {}
    for key, pipe in lk.items():
        name, _, ver = key.rpartition("@")
        mv = f"mlflow-model:{name}@{ver}"
        nodes[mv] = {"id": mv, "kind": "mlflow-model", "label": f"{name} v{ver}"}
        for v in ctx.registry.versions("pipeline", pipe)[-1:]:
            pid = f"pipeline:{pipe}@v{v}"
            nodes.setdefault(pid, {"id": pid, "kind": "pipeline", "label": f"{pipe}@v{v}"})
            edges.append({"src": mv, "dst": pid, "relation": "served_by"})
        run_id = (reg.get((name, ver)) or {}).get("run_id")
        if run_id:
            rn = f"mlflow-run:{run_id}"
            nodes[rn] = {"id": rn, "kind": "mlflow-run", "label": f"run {run_id[:8]}"}
            edges.append({"src": rn, "dst": mv, "relation": "produced"})
    return {"nodes": list(nodes.values()), "edges": edges}

