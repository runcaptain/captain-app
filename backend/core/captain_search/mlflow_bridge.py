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


class RateLimited(RuntimeError):
    """The server said it is limiting requests. `wait` is how long it asked for, in seconds, or None."""

    def __init__(self, message: str, wait: float | None = None):
        super().__init__(message)
        self.wait = wait


# Every call made to the tracking server or the model catalog since the services started. A hosted catalog can have
# one rate limit for a whole organisation: what Captain asks of it is counted, and said in the log.
CALLS = {"n": 0}
PAUSE_S = 0.3                 # between two calls of a background read
_pace_lock = threading.Lock()
_paced_at = [0.0]


def _out(paced: bool):
    """Count one call to the server. A background read (`paced`) makes its calls one at a time with a small pause
    between them, however many threads are reading."""
    CALLS["n"] += 1
    if not paced:
        return
    with _pace_lock:
        wait = _paced_at[0] + PAUSE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _paced_at[0] = time.monotonic()


def _limited(status: int, text: str) -> bool:
    return status in (429, 503) or "rate limit" in str(text).lower() or "too many requests" in str(text).lower()


def _retry_after(r) -> float | None:
    try:
        return float(r.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


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


def _call(ctx: Context, method: str, path: str, body: dict | None = None, timeout: float = 60, paced: bool = False) -> dict:
    import httpx

    _out(paced)
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
    if _limited(r.status_code, r.text[:400]):
        raise RateLimited("MLflow is limiting requests.", _retry_after(r))
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


def experiments(ctx: Context, limit: int = 200, timeout: float = 60, paced: bool = False) -> list[dict]:
    out = _call(ctx, "POST", "experiments/search", {"max_results": min(limit, 1000)}, timeout, paced)
    return [{"id": e["experiment_id"], "name": e.get("name"), "stage": e.get("lifecycle_stage"),
             "updated": _ts(e.get("last_update_time"))} for e in out.get("experiments", [])]


def runs(ctx: Context, experiment_id: str, limit: int = 100, order: str = "attributes.start_time DESC", paced: bool = False) -> list[dict]:
    """An experiment's runs. `paced` is the app asking for a person who opened the experiment: it takes its turn
    behind the background reads, and while the server has said to slow down it is not asked at all."""
    if paced:
        left = limited_for(ctx)
        if left > 0:
            raise RateLimited(f"The model catalog is limiting requests. Try again in {_span(max(1, int(round(left))))}.", left)
    out = _call(ctx, "POST", "runs/search", {"experiment_ids": [experiment_id], "max_results": min(limit, 1000),
                                              "order_by": [order]}, CALL_S if paced else 60, paced)
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


def _uc(ctx: Context, path: str, params: dict | None = None, timeout: float = 60, paced: bool = False) -> dict:
    """Unity Catalog's REST API (2.1) on the same Databricks host and login. A person's command (not `paced`)
    waits out a short rate limit, as told. A background read never retries here: it raises RateLimited and the
    read as a whole waits, so a limit shared by a whole organisation is not pushed on."""
    import httpx

    base, headers = _endpoint(ctx)
    tries = 1 if paced else 6
    for attempt in range(tries):
        _out(paced)
        r = httpx.get(f"{base}/api/2.1/unity-catalog/{path}", params=params, headers=headers, timeout=timeout)
        if r.status_code not in (429, 503) or attempt == tries - 1:
            break
        # Databricks rate-limits bursts: wait as told, or back off.
        time.sleep(_retry_after(r) or min(8.0, 0.5 * 2 ** attempt))
    if r.status_code in (401, 403):
        raise PermissionError("Unity Catalog refused the login: the token needs access to Unity Catalog models.")
    if r.status_code >= 400:
        try:
            said = str(r.json().get("message") or r.text)[:300]
        except ValueError:
            said = r.text[:300]
        if _limited(r.status_code, said):
            raise RateLimited(f"Unity Catalog said: {said}", _retry_after(r))
        raise RuntimeError(f"Unity Catalog said: {said}")
    return r.json()


def _uc_models(ctx: Context, limit: int, include_system: bool = False, timeout: float = 60, paced: bool = False) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    models_, token = [], None
    while len(models_) < limit:
        page = _uc(ctx, "models", {"max_results": min(100, limit - len(models_)), **({"page_token": token} if token else {})}, timeout, paced)
        # The `system` catalog holds Databricks' own built-in models, not the team's: hidden unless asked for.
        models_ += [m for m in page.get("registered_models", []) if include_system or m.get("catalog_name") != "system"]
        token = page.get("next_page_token")
        if not token:
            break

    def versions(m):
        try:
            vs = _uc(ctx, f"models/{m['full_name']}/versions", {"max_results": 50}, timeout, paced).get("model_versions", [])
        except RateLimited:
            raise                  # told to slow down: stop asking, the whole read waits
        except Exception:  # noqa: BLE001  one model's versions failing shouldn't hide the rest
            return None
        return [{"version": str(v.get("version")), "run_id": v.get("run_id"), "status": v.get("status"),
                 "stage": None, "source": v.get("source") or v.get("storage_location"),
                 "created": _ts(v.get("created_at")),
                 "aliases": [a.get("alias_name") for a in (v.get("aliases") or []) if a.get("alias_name")]} for v in vs]

    if paced:
        vlists = [versions(m) for m in models_]          # one at a time, with the pause between calls
    else:
        with ThreadPoolExecutor(4) as pool:
            vlists = list(pool.map(versions, models_))
    return [{"name": m["full_name"], "description": m.get("comment"), "updated": _ts(m.get("updated_at")),
             "registry": "unity-catalog", "versions": vs or [], "versions_unknown": vs is None}
            for m, vs in zip(models_, vlists)]


def models(ctx: Context, limit: int = 200, include_system: bool = False, timeout: float = 60, paced: bool = False) -> list[dict]:
    """Registered models. Databricks workspaces read Unity Catalog first (Databricks' default for models), then the
    legacy workspace registry if it's on. Other tracking servers use the MLflow registry. `timeout` is the limit
    on each call to the server. `paced` is a background read: one call at a time, a pause between, no retries."""
    if _is_databricks(ctx):
        uc = _uc_models(ctx, limit, include_system, timeout, paced)
        try:
            legacy = _legacy_models(ctx, limit, timeout, paced)
        except RegistryDisabled:
            legacy = []
        return uc + legacy
    return _legacy_models(ctx, limit, timeout, paced)


# ---- the models, kept ------------------------------------------------------------------------------------------
# Reading the registered models asks the server once for the list and once more for each model's versions: about
# ten seconds on a hosted catalog, with the sidebar waiting, at every start. The screen is now always answered at
# once: with what was read last (kept in memory, and in a small file so the next start has it too), or with an
# empty list and a sentence saying it is being read. The reading happens behind that.
#
# A hosted catalog can limit requests for a whole organisation, so the background read is gentle: one call at a
# time, a pause between calls, ten seconds a call at most, and when the server says it is limiting requests the
# read stops and waits as long as it was told (or half a minute, then longer), never retrying in a loop. Nothing
# the server does is an error of the app's: the route never fails for it.

KEEP_S = 300.0         # how long a kept answer is given without reading again behind it: five minutes, since the
                       # server's limit can be a whole organisation's
AGAIN_S = 10.0         # the soonest a person's "Read again" reads again
CALL_S = 10.0          # the limit on each call of a background read
LIMITED_S = 30.0       # how long to wait after being told to slow down, when the server did not say
LIMITED_MAX_S = 600.0
FAILED_S = 60.0        # how long to wait after any other failure
_KEPT: dict = {}       # key -> {"at": monotonic, "value": [...]}
_STATE: dict = {}      # key -> {"state": reading|failed, "why": str, "until": monotonic}
_WAIT: dict = {}       # server -> {"until": monotonic, "tries": n}: told to slow down, for everything read from it
_REFRESHING: set = set()
_keep_lock = threading.Lock()

WHAT = {"models": "registered models", "experiments": "experiments"}


def _server(ctx: Context) -> str:
    doc = _read(ctx)[0]
    return hashlib.sha256(json.dumps([ctx.workspace.name, doc.get("uri"), doc.get("databricks_connection")]).encode()).hexdigest()[:20]


def _keep_key(ctx: Context, include_system: bool, kind: str = "models") -> str:
    return f"{kind}-{_server(ctx)}" + ("-sys" if include_system else "")


def _keep_file(key: str) -> str:
    from .workspace import home

    return os.path.join(home(), "cache", f"mlflow-{key}.json")


def _keep(key: str, value: list) -> None:
    try:   # the file first: once the answer is given out as read, the next start must find it too
        p = _keep_file(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"value": value}, fh)
        os.replace(p + ".tmp", p)
    except OSError:
        pass   # the file is a convenience for the next start
    with _keep_lock:
        _KEPT[key] = {"at": time.monotonic(), "value": value}
        _STATE.pop(key, None)


def _tell() -> None:
    try:
        from . import changes

        changes.bump("mlflow")   # the screen reads again: a new list, or new words about why there is none yet
    except Exception:  # noqa: BLE001
        pass


def _log(message: str) -> None:
    try:
        from . import applog

        applog.info("mlflow", message)
    except Exception:  # noqa: BLE001
        pass


def limited_for(ctx: Context) -> float:
    """How many seconds are left of a wait the server asked for. 0 when it has not, or the wait is over."""
    with _keep_lock:
        w = _WAIT.get(_server(ctx))
    return max(0.0, w["until"] - time.monotonic()) if w else 0.0


def _read_behind(ctx: Context, key: str, kind: str, read) -> bool:
    """Read in the background, once, however many asks arrive meanwhile. While the server has said to slow down,
    or just after a failure, nothing is asked: the next ask after the wait starts another read. Returns whether
    a read was started."""
    srv = _server(ctx)
    with _keep_lock:
        st, w = _STATE.get(key), _WAIT.get(srv)
        now = time.monotonic()
        if key in _REFRESHING or (w and w["until"] > now) or (st and st.get("until", 0) > now):
            return False
        _REFRESHING.add(key)
        _STATE[key] = {"state": "reading", "why": None, "until": 0}

    def go():
        began, calls = time.monotonic(), CALLS["n"]
        try:
            new = read()
            with _keep_lock:
                old = (_KEPT.get(key) or {}).get("value")
                was = _WAIT.pop(srv, None)
            _keep(key, new)
            _log(f"read {len(new)} {WHAT[kind]} with {CALLS['n'] - calls} calls in {time.monotonic() - began:.1f} s ({CALLS['n']} calls to the server since the services started)")
            if new != old or was:
                _tell()
        except RateLimited as e:
            with _keep_lock:
                tries = (_WAIT.get(srv) or {}).get("tries", 0)
                wait = e.wait if e.wait else min(LIMITED_MAX_S, LIMITED_S * 2 ** tries)
                _WAIT[srv] = {"until": time.monotonic() + wait, "tries": tries + 1}
                _STATE.pop(key, None)
            _log(f"the server is limiting requests: stopped after {CALLS['n'] - calls} calls, not asking again for {wait:.0f} s ({CALLS['n']} calls since the services started)")
            _tell()
        except Exception as e:  # noqa: BLE001  whatever the server did, the route does not fail for it
            with _keep_lock:
                _STATE[key] = {"state": "failed", "why": str(e)[:200] or "the server could not be reached", "until": time.monotonic() + FAILED_S}
            _log(f"the {WHAT[kind]} could not be read after {CALLS['n'] - calls} calls: {str(e)[:200]}")
            _tell()
        finally:
            with _keep_lock:
                _REFRESHING.discard(key)

    threading.Thread(target=go, daemon=True, name=f"mlflow-{kind}").start()
    return True


def _kept(ctx: Context, kind: str, key: str, read, fresh: bool = False) -> dict:
    """{<kind>, state, text, retry_in_s}, at once, always. See `models_kept`."""
    with _keep_lock:
        hit = _KEPT.get(key)
    if hit is None:
        try:
            with open(_keep_file(key)) as fh:
                hit = {"at": float("-inf"), "value": json.load(fh)["value"]}   # from an earlier run: given, and read again now
            with _keep_lock:
                hit = _KEPT.setdefault(key, hit)
        except (OSError, ValueError, KeyError):
            hit = None
    age = time.monotonic() - hit["at"] if hit else float("inf")
    # A person's "Read again" reads now, but not more often than every few seconds, and never past a wait the
    # server asked for: `_read_behind` holds to that.
    if hit is None or age > KEEP_S or (fresh and age > AGAIN_S):
        _read_behind(ctx, key, kind, read)
    with _keep_lock:
        st = dict(_STATE.get(key) or {})
    value = hit["value"] if hit else []
    wait = limited_for(ctx)
    if wait > 0:
        left = max(1, int(round(wait)))
        text = (f"Could not be read: the model catalog is limiting requests, trying again in {_span(left)}." if not hit
                else f"Showing the last read: the model catalog is limiting requests, trying again in {_span(left)}.")
        return {kind: value, "state": "limited", "text": text, "retry_in_s": left}
    if st.get("state") == "failed":
        left = max(0, int(round(st.get("until", 0) - time.monotonic())))
        again = f"Trying again in {_span(left)}." if left else "Trying again shortly."
        text = f"Could not be read: {_plain(st['why'])} {again}" if not hit else f"Showing the last read. {again}"
        return {kind: value, "state": "failed", "text": text, "retry_in_s": left}
    if hit is None:
        return {kind: [], "state": "reading", "text": f"Reading the {WHAT[kind]}. A hosted catalog can take a few seconds.", "retry_in_s": 0}
    if st.get("state") == "reading" and fresh:
        return {kind: value, "state": "reading", "text": "Reading again.", "retry_in_s": 0}
    return {kind: value, "state": "ready", "text": None, "retry_in_s": 0}


def models_kept(ctx: Context, include_system: bool = False, fresh: bool = False) -> dict:
    """{models, state, text, retry_in_s}, at once, always. `models` is what was read last (in this run or an
    earlier one), or an empty list when nothing has been read yet. `state` is "ready", "reading", "limited" (the
    server said to slow down) or "failed", and `text` says it in words when it is not ready. A read runs behind
    the answer when what is kept is older than five minutes, or when a person asks to read again (`fresh`), and
    the change feed says `mlflow` when it lands."""
    return _kept(ctx, "models", _keep_key(ctx, include_system, "models"),
                 lambda: models(ctx, include_system=include_system, timeout=CALL_S, paced=True), fresh)


def experiments_kept(ctx: Context, fresh: bool = False) -> dict:
    """{experiments, state, text, retry_in_s}: the experiments, by the same rules as `models_kept`."""
    return _kept(ctx, "experiments", _keep_key(ctx, False, "experiments"), lambda: experiments(ctx, timeout=CALL_S, paced=True), fresh)


def _span(seconds: int) -> str:
    return f"{seconds} s" if seconds < 90 else f"{round(seconds / 60)} min"


def _plain(why: str) -> str:
    why = str(why).strip()
    return why if why.endswith((".", "!", "?")) else why + "."


def forget_models() -> None:
    """Drop what is kept in memory (the settings changed, or a test)."""
    with _keep_lock:
        _KEPT.clear()
        _STATE.clear()
        _WAIT.clear()


def _legacy_models(ctx: Context, limit: int = 200, timeout: float = 60, paced: bool = False) -> list[dict]:
    out = _call(ctx, "GET", "registered-models/search", {"max_results": min(limit, 1000)}, timeout, paced)
    found = out.get("registered_models", [])

    def versions_of(m):
        return _call(ctx, "GET", "model-versions/search", {"filter": "name='" + m["name"].replace("'", "\\'") + "'"}, timeout, paced).get("model_versions", [])

    vlists = [versions_of(m) for m in found]   # one at a time, as it always was for this registry
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

