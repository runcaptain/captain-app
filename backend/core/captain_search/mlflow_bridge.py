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
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlparse

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "mlflow.yaml"


class RegistryDisabled(RuntimeError):
    """The workspace model registry is turned off (Databricks workspaces that use Unity Catalog for models)."""


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


def _call(ctx: Context, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    import httpx

    base, headers = _endpoint(ctx)
    url = f"{base}/api/2.0/mlflow/{path}"
    try:
        r = httpx.request(method, url, json=body if method != "GET" else None,
                          params=body if method == "GET" else None, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        # The person's own address, without the transport's words (no exception names): what to check, in words.
        host = base.split("://", 1)[-1].rstrip("/")
        slow = isinstance(e, httpx.TimeoutException)
        raise ConnectionError(f"Couldn't reach MLflow at {host}" + (": it didn't answer in time." if slow else ".")
                              + " Check the address in Connect MLflow, and that the server is running.") from None
    if r.status_code in (401, 403):
        try:
            said = str(r.json().get("message") or "").split(" [ReqId")[0]
        except ValueError:
            said = ""
        if "registry is disabled" in said.lower():
            # Not a login problem: this workspace keeps models in Unity Catalog instead.
            raise RegistryDisabled(said)
        hint = (" For Databricks, make a token with the mlflow scope (a SQL-only token can't read experiments)."
                if "scope" in said.lower() or not said else "")
        raise PermissionError("MLflow refused the login" + (f": {said}." if said else ".") + hint)
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


def _is_databricks(ctx: Context) -> bool:
    return bool(_read(ctx)[0].get("databricks_connection"))


def _uc(ctx: Context, path: str, params: dict | None = None, timeout: float = 60) -> dict:
    """Unity Catalog's REST API (2.1) on the same Databricks host and login."""
    import httpx

    import time

    base, headers = _endpoint(ctx)
    tries = 6 if timeout >= 30 else 2   # a read with a short limit does not wait out a rate limit
    for attempt in range(tries):
        r = httpx.get(f"{base}/api/2.1/unity-catalog/{path}", params=params, headers=headers, timeout=timeout)
        if r.status_code not in (429, 503):
            break
        # Databricks rate-limits bursts: wait as told, or back off.
        time.sleep(min(timeout, float(r.headers.get("Retry-After") or 0) or min(8.0, 0.5 * 2 ** attempt)))
    if r.status_code in (401, 403):
        raise PermissionError("Unity Catalog refused the login: the token needs access to Unity Catalog models.")
    if r.status_code >= 400:
        raise RuntimeError(f"Unity Catalog said: {str(r.json().get('message') or r.text)[:300]}")
    return r.json()


def _uc_models(ctx: Context, limit: int, include_system: bool = False, timeout: float = 60) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    models_, token = [], None
    while len(models_) < limit:
        page = _uc(ctx, "models", {"max_results": min(100, limit - len(models_)), **({"page_token": token} if token else {})}, timeout)
        # The `system` catalog holds Databricks' own built-in models, not the team's: hidden unless asked for.
        models_ += [m for m in page.get("registered_models", []) if include_system or m.get("catalog_name") != "system"]
        token = page.get("next_page_token")
        if not token:
            break

    def versions(m):
        try:
            vs = _uc(ctx, f"models/{m['full_name']}/versions", {"max_results": 50}, timeout).get("model_versions", [])
        except Exception:  # noqa: BLE001  one model's versions failing shouldn't hide the rest
            return None
        return [{"version": str(v.get("version")), "run_id": v.get("run_id"), "status": v.get("status"),
                 "stage": None, "source": v.get("source") or v.get("storage_location"),
                 "created": _ts(v.get("created_at")),
                 "aliases": [a.get("alias_name") for a in (v.get("aliases") or []) if a.get("alias_name")]} for v in vs]

    with ThreadPoolExecutor(8) as pool:
        vlists = list(pool.map(versions, models_))
    return [{"name": m["full_name"], "description": m.get("comment"), "updated": _ts(m.get("updated_at")),
             "registry": "unity-catalog", "versions": vs or [], "versions_unknown": vs is None}
            for m, vs in zip(models_, vlists)]


def models(ctx: Context, limit: int = 200, include_system: bool = False, timeout: float = 60) -> list[dict]:
    """Registered models. Databricks workspaces read Unity Catalog first (Databricks' default for models), then the
    legacy workspace registry if it's on. Other tracking servers use the MLflow registry. `timeout` is the limit
    on each call made to the server."""
    if _is_databricks(ctx):
        uc = _uc_models(ctx, limit, include_system, timeout)
        try:
            legacy = _legacy_models(ctx, limit, timeout)
        except RegistryDisabled:
            legacy = []
        return uc + legacy
    return _legacy_models(ctx, limit, timeout)


# ---- the models, kept ------------------------------------------------------------------------------------------
# Reading the registered models asks the tracking server once for the list and once more for each model's
# versions: 5 to 11 seconds at every start of the app, with the sidebar waiting. The screen is answered at once
# from what was read last (kept in memory, and in a small file so the next start has it too), and the list is
# read again behind that, each call limited to three seconds. When the new read differs, the change feed says so
# (`mlflow`) and the screen reads again.

KEEP_S = 30.0      # how long a kept answer is given without reading again behind it
QUICK_S = 3.0      # the limit on each call to the server when a screen is waiting or a refresh runs
_KEPT: dict = {}   # key -> {"at": monotonic, "value": [...]}
_REFRESHING: set = set()
_keep_lock = threading.Lock()


def _keep_key(ctx: Context, include_system: bool) -> str:
    doc = _read(ctx)[0]
    return hashlib.sha256(json.dumps([ctx.workspace.name, doc.get("uri"), doc.get("databricks_connection"), bool(include_system)]).encode()).hexdigest()[:20]


def _keep_file(key: str) -> str:
    from .workspace import home

    return os.path.join(home(), "cache", f"mlflow-models-{key}.json")


def _keep(key: str, value: list) -> None:
    with _keep_lock:
        _KEPT[key] = {"at": time.monotonic(), "value": value}
    try:
        p = _keep_file(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"value": value}, fh)
        os.replace(p + ".tmp", p)
    except OSError:
        pass   # the file is a convenience for the next start


def _refresh_behind(ctx: Context, key: str, include_system: bool) -> None:
    with _keep_lock:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def go():
        try:
            new = models(ctx, include_system=include_system, timeout=QUICK_S)
            with _keep_lock:
                old = (_KEPT.get(key) or {}).get("value")
            _keep(key, new)
            if new != old:
                from . import changes

                changes.bump("mlflow")   # the screen reads again, and gets the new list at once
        except Exception:  # noqa: BLE001  the server can't be reached just now: what was read last stands
            with _keep_lock:
                if key in _KEPT:
                    _KEPT[key]["at"] = time.monotonic()   # and it is not asked again for a while
        finally:
            with _keep_lock:
                _REFRESHING.discard(key)

    threading.Thread(target=go, daemon=True, name="mlflow-models").start()


def models_kept(ctx: Context, include_system: bool = False) -> list[dict]:
    """The registered models, at once when they have been read before (in this run or an earlier one), and read
    again behind that when the kept answer is older than half a minute. The first read ever waits for the server,
    three seconds a call at most."""
    key = _keep_key(ctx, include_system)
    with _keep_lock:
        hit = _KEPT.get(key)
    if hit is None:
        try:
            with open(_keep_file(key)) as fh:
                hit = {"at": float("-inf"), "value": json.load(fh)["value"]}   # from an earlier run: given, and read again now
            with _keep_lock:
                _KEPT.setdefault(key, hit)
        except (OSError, ValueError, KeyError):
            hit = None
    if hit is None:
        value = models(ctx, include_system=include_system, timeout=QUICK_S)
        _keep(key, value)
        return value
    if time.monotonic() - hit["at"] > KEEP_S:
        _refresh_behind(ctx, key, include_system)
    return hit["value"]


def forget_models() -> None:
    """Drop what is kept in memory (the settings changed, or a test)."""
    with _keep_lock:
        _KEPT.clear()


def _legacy_models(ctx: Context, limit: int = 200, timeout: float = 60) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    out = _call(ctx, "GET", "registered-models/search", {"max_results": min(limit, 1000)}, timeout)
    found = out.get("registered_models", [])

    def versions_of(m):
        return _call(ctx, "GET", "model-versions/search", {"filter": "name='" + m["name"].replace("'", "\\'") + "'"}, timeout).get("model_versions", [])

    # One call for each model's versions, eight at a time: one after another they were most of the wait.
    with ThreadPoolExecutor(8) as pool:
        vlists = list(pool.map(versions_of, found))
    res = []
    for m, versions in zip(found, vlists):
        res.append({"name": m["name"], "description": m.get("description"), "updated": _ts(m.get("last_updated_timestamp")),
                    "registry": "workspace", "versions": [{"version": v["version"], "run_id": v.get("run_id"), "status": v.get("status"),
                                  "stage": v.get("current_stage"), "source": v.get("source"),
                                  "created": _ts(v.get("creation_timestamp")),
                                  "aliases": v.get("aliases") or []} for v in versions]})
    return res


def link(ctx: Context, model: str, version: str, pipeline: str) -> dict:
    """Say which Captain pipeline serves an MLflow model version, for lineage."""
    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    pinned = f"{pm.name}@v{pm.version}"  # the exact version, so lineage doesn't drift to whatever is newest

    def ch(doc):
        doc.setdefault("links", {})[f"{model}@{version}"] = pinned
        return {"model": model, "version": version, "pipeline": pinned}

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
        name_, _, pv = pipe.partition("@v")
        # Links made before versions were pinned name only the pipeline: those still point at its newest version.
        for v in ([int(pv)] if pv.isdigit() else ctx.registry.versions("pipeline", pipe)[-1:]):
            pid = f"pipeline:{name_}@v{v}"
            nodes.setdefault(pid, {"id": pid, "kind": "pipeline", "label": f"{name_}@v{v}"})
            edges.append({"src": mv, "dst": pid, "relation": "served_by"})
        run_id = (reg.get((name, ver)) or {}).get("run_id")
        if run_id:
            rn = f"mlflow-run:{run_id}"
            nodes[rn] = {"id": rn, "kind": "mlflow-run", "label": f"run {run_id[:8]}"}
            edges.append({"src": rn, "dst": mv, "relation": "produced"})
    return {"nodes": list(nodes.values()), "edges": edges}

