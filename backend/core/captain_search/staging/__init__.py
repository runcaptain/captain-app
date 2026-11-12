"""Staging: one step from a candidate to a live endpoint a person can try, in the team's own cloud.

A staged model runs where the team's compute target says: a SageMaker endpoint in their AWS account, or, with
`--target local`, a process on this machine bound to loopback. Captain never hosts it. Both run the same runtime
(runtime.py) over the same serving bundle, so what is tried locally is what the cloud serves.

  stage     package a model version (and the baseline beside it) into a bundle, start the endpoint, record it on
            the project's Ship state
  unstage   stop it and remove every resource Captain made
  status    is it up, what it serves, how long until it stops by itself
  query     ask the endpoint a question, and join the answer with the catalog (the Try it console)

A serving bundle is a folder in the workspace's storage, `staging/<project>/bundles/<id>/`: `bundle.json` names
each arm (baseline, candidate) with its model version, engine, spec and files; the artifact files (the trained
model, its feature spec and lookup tables) are copied beside it; the catalog is stored once per version under
`staging/<project>/catalogs/`.

What can be served: a keyword-search version or a starter ranker (Captain's own runtime), and any version that
ranks by calling a service the team already runs (it is wrapped: Captain's runtime sits in front and does the
logging and the split). A model trained outside Captain with no serving entry point is refused in a sentence;
bring an HTTP endpoint (`--endpoint`) or a container image (`--image`) for it.

Project state. `projects/<id>/state.yaml` gains `staging: {<env>: record}`. A record holds the endpoint, the
model version, when it started, its status and a cost note, and every cloud resource Captain created, so
`unstage` removes exactly those. The endpoint key lives in this computer's keychain; the record names it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets as _secrets
import signal
import socket
import subprocess
import sys
import time

from .. import project_state as PS
from ..model.entities import new_id
from ..storage.backend import AlreadyExists, Conflict, NotFound
from ..workspace import Context, whoami
from . import logs as L
from . import runtime as RT

ENV = "staging"
IDLE_MINUTES = 240
CAPTAIN_ENGINES = ("local-bm25", "bm25-ltr")
WRAPPED_ENGINES = ("http", "elastic", "typesense", "algolia", "captain")
KEEPS_BILLING = "This keeps billing until you stop it."


class NotServable(ValueError):
    """This version can't be served by Captain's runtime. The message says why and what to bring instead."""


class NeedsConfirm(ValueError):
    """A cloud deploy that hasn't been confirmed. `.plan` is what would be created and what it costs."""

    def __init__(self, msg: str, plan: dict):
        super().__init__(msg)
        self.plan = plan


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _ago(iso: str | None) -> float | None:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None


# ---- what can be served --------------------------------------------------------------------------


def servable(ctx: Context, pipeline: str) -> dict:
    """{ok, how, text}: can Captain's runtime serve this version, and how."""
    from ..ops import ref

    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    r = ref(pm)
    if pm.engine in CAPTAIN_ENGINES:
        if not pm.catalog:
            return {"ok": False, "pipeline": r, "how": None,
                    "text": f"{r} has no catalog, so there are no products to rank. Publish it with a catalog."}
        return {"ok": True, "pipeline": r, "how": "captain", "text": "Captain's own runtime serves it."}
    if pm.engine == "http" and (not (pm.spec or {}).get("url") or "set-me" in str(pm.spec.get("url"))):
        return {"ok": False, "pipeline": r, "how": None,
                "text": f"Captain can't serve {r}: it points at a model in a registry and has no serving entry point "
                        "Captain can load. Use an endpoint you already run, or give a container image that serves it."}
    if pm.engine in WRAPPED_ENGINES:
        return {"ok": True, "pipeline": r, "how": "wrapped",
                "text": "It ranks by calling a service you already run. Captain's runtime sits in front of it."}
    return {"ok": False, "pipeline": r, "how": None,
            "text": f"Captain can't serve {r}: it was built outside Captain ({pm.engine}) and has no serving entry "
                    "point Captain can load. Use an endpoint you already run, or give a container image that serves it."}


def cost(target: dict, ctx: Context) -> dict:
    """What a deploy on this target costs to keep up, in words a person reads before confirming. The number and
    its sentence come from the shared estimator (cost_estimates.py), so staging and every form agree."""
    from .. import cost_estimates as CE

    c = target.get("config") or {}
    local = target["kind"] == "local"
    size = {} if local else {"instances": int(c.get("instance_count") or 1), "instance_type": c.get("instance_type") or "ml.m5.large"}
    est = CE.estimate(ctx, "staging", target, size)
    if est.get("sleeps"):  # a host that sleeps when idle bills by the second awake, not by the hour up
        return {"instance_type": est.get("machine"), "instance_count": None, "hourly_usd": est.get("high"), "billing": True,
                "estimate": est, "sleeps": True, "text": est["text"]}
    if not local:
        from .. import compute as C

        if C.scale_to_zero(ctx, target):  # not priced yet (it has not run on a real account), and it still sleeps
            return {"instance_type": None, "instance_count": None, "hourly_usd": None, "billing": True, "estimate": est, "sleeps": True,
                    "text": est["text"] + " It bills only while a copy is awake, and sleeps when nobody calls it."}
    return {"instance_type": size.get("instance_type"), "instance_count": size.get("instances"),
            "hourly_usd": est.get("low"), "billing": not local, "estimate": est,
            "text": est["text"] + (" It stops when idle." if local else f" {KEEPS_BILLING}")}


# ---- bundle --------------------------------------------------------------------------------------


def _put(store, key: str, data: bytes) -> None:
    try:
        store.put_new(key, data)
    except AlreadyExists:
        pass


def _arm_spec(ctx: Context, pid: str, bundle_key: str, name: str, pipeline: str, store=None) -> dict:
    """Copy what one arm needs to rank into the bundle, and return its entry for bundle.json."""
    import pyarrow as pa

    from .. import ops
    from ..data.datasets import to_parquet
    from ..engines import artifact_reader, make_engine

    s = servable(ctx, pipeline)
    if not s["ok"]:
        raise NotServable(s["text"])
    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    spec = {k: v for k, v in (pm.spec or {}).items() if k != "train"}
    out = {"pipeline": ops.ref(pm), "engine": pm.engine, "spec": spec, "kind": s["how"], "catalog": None, "artifact": {}}
    if s["how"] == "wrapped":
        return out
    prods = ops.products_of(ctx, pm.catalog)
    eng = make_engine(pm.engine, pm.spec, prods, artifact=artifact_reader(ctx, pm))  # it loads here, or not at all
    fp = eng.fingerprint()
    if fp and pm.index_fingerprint and (fp.doc_count, fp.sample_hash) != (pm.index_fingerprint.doc_count, pm.index_fingerprint.sample_hash):
        raise NotServable(f"the catalog behind {ops.ref(pm)} changed since it was published, so serving it would not "
                          "serve what was evaluated. Publish a new version.")
    cat = pm.catalog.split(":")[0].replace("/", "_").replace("@", "-")
    out["catalog"] = f"staging/{pid}/catalogs/{cat}-{(fp.sample_hash[:12] if fp else 'x')}.parquet"
    store = store or ctx.store
    if not store.exists(out["catalog"]):
        _put(store, out["catalog"], to_parquet(pa.Table.from_pylist(prods)))
    for f in pm.files or []:
        if f.path.startswith("artifact/"):
            rel = f.path[len("artifact/"):]
            _put(store, f"{bundle_key}/{name}/{rel}", ctx.registry.get_blob(f))
            out["artifact"][rel] = f"{name}/{rel}"
    return out


def build_bundle(ctx: Context, pid: str, env: str, arms: dict[str, str | dict], serve: str,
                 idle_minutes: float = IDLE_MINUTES, store=None, sleeps: bool = False) -> tuple[str, dict]:
    """Write a serving bundle for these arms ({arm name: pipeline ref, or a ready arm entry}). bundle.json goes
    last, so a half-written bundle is never loaded."""
    bid = "stg-" + new_id().lower()[-10:]
    key = f"staging/{pid}/bundles/{bid}"
    doc = {"id": bid, "project": pid, "env": env, "created_at": _now(), "created_by": whoami(), "arms": {},
           "idle_minutes": idle_minutes,
           # On a host that sleeps when idle, the host stops the copies: the runtime never stops itself. And an
           # endpoint that only holds the bundle and the log never publishes the log: Captain does, from the workspace.
           **({"scale_to_zero": True} if sleeps else {}), **({"serving_store": True} if store is not None else {}),
           "control": {"serve": serve, "split": None, "experiment": None, "salt": bid,
                                                     "stage": bid}}
    for name, a in arms.items():
        doc["arms"][name] = a if isinstance(a, dict) else _arm_spec(ctx, pid, key, name, a, store)
    (store or ctx.store).put_new(f"{key}/bundle.json", json.dumps(doc, indent=1).encode())
    return key, doc


# ---- state ---------------------------------------------------------------------------------------


def state(ctx: Context, pid: str, env: str = ENV) -> dict | None:
    return ((PS.get(ctx, pid).get("staging") or {}).get(env)) or None


def _save(ctx: Context, pid: str, env: str, rec: dict | None = None, **fields) -> dict:
    def ch(doc):
        st = doc.setdefault("staging", {}) or {}
        cur = rec if rec is not None else dict(st.get(env) or {})
        cur.update(fields)
        st[env] = cur
        doc["staging"] = st
        return dict(cur)

    return PS._update(ctx, pid, ch)


def _note(ctx: Context, pid: str, what: str, **extra) -> None:
    PS.record_change(ctx, pid, {"what": what, "user": whoami(), **extra})


def key_name(pid: str, env: str) -> str:
    return ("STAGING_" + f"{pid}_{env}".upper().replace("-", "_").replace("/", "_") + "_KEY")


def _key(rec: dict) -> str | None:
    from ..workspace import get_secret

    if not rec.get("key_secret"):
        return None
    try:
        return get_secret(rec["key_secret"])
    except KeyError:
        raise ValueError("this computer doesn't hold the key for the staging endpoint (it is kept in the keychain "
                         "of the computer that staged it). Stage it again from here, or ask whoever staged it.") from None


def control(ctx: Context, pid: str, env: str = ENV) -> dict:
    try:
        return json.loads(L.shared(ctx, pid, env).get(RT.control_key(pid, env)))
    except NotFound:
        return {}


def set_control(ctx: Context, pid: str, env: str, **fields) -> dict:
    """Change what the running endpoint does (which arm it serves, the split, the idle limit). The runtime re-reads
    this every few seconds, and at once when asked."""
    k = RT.control_key(pid, env)
    store = L.shared(ctx, pid, env)
    for _ in range(20):
        try:
            body, tag = store.get_with_tag(k)
            doc = json.loads(body)
        except NotFound:
            doc, tag = {}, None
        doc.update(fields)
        doc["updated_at"] = _now()
        try:
            store.put_if(k, json.dumps(doc).encode(), tag)
            return doc
        except Conflict:  # someone else wrote: read again
            time.sleep(0.01)
    raise ValueError("the staging settings kept changing while saving. Try again.")


# ---- choosing what to stage ----------------------------------------------------------------------


def _pick(ctx: Context, doc: dict, candidate: str | None, baseline: bool) -> tuple[dict[str, str], str, str | None]:
    """(arms, the arm served, the candidate's experiment id)."""
    from ..ops import ref

    champ = doc.get("champion")
    exps = doc.get("experiments") or []
    if baseline:
        if not champ:
            raise ValueError("This project has no baseline yet. Set one first, then stage it.")
        return {"baseline": champ}, "baseline", None
    exp = None
    if candidate:
        exp = next((e for e in exps if e["id"] == candidate or e["pipeline"] == candidate), None)
        target = exp["pipeline"] if exp else ref(ctx.registry.resolve("pipeline", candidate)[0])
    else:
        rest = [e for e in exps if e["pipeline"] != champ]
        judged = [e for e in rest if e.get("status") not in ("trained", "failed", None)]
        exp = (judged or rest or [None])[-1]
        target = exp["pipeline"] if exp else champ
        if not target:
            raise ValueError("There is nothing to stage yet. Set a baseline or train a candidate first.")
    if target == champ:
        return {"baseline": champ}, "baseline", None
    arms = {"candidate": target}
    if champ and servable(ctx, champ)["ok"]:
        arms["baseline"] = champ
    return arms, "candidate", exp["id"] if exp else None


def _target(ctx: Context, name: str | None) -> dict:
    from .. import compute as C
    from .. import settings as ST

    name = name or ST.default_compute(ctx, "serve") or "local"
    t = C.get(ctx, name)
    if t["kind"] != "local" and not C.can_stage(ctx, t):
        raise ValueError(f"Compute target {name} ({t['kind']}) can't run a staging endpoint yet. Use a target that "
                         "serving setup made (AWS, Modal, Google Cloud or Azure), or stage on this computer with --target local.")
    return t


def plan(ctx: Context, pid: str, candidate: str | None = None, baseline: bool = False, env: str = ENV,
         target: str | None = None, endpoint: str | None = None, image: str | None = None, allow_large: bool = False) -> dict:
    """What staging would do, before anything is created: the versions, where, and what it costs."""
    doc = PS.get(ctx, pid)
    t = _target(ctx, "local" if endpoint and not target else target)
    if endpoint:
        arms, serve, exp = {"candidate": endpoint}, "candidate", None
        if doc.get("champion") and servable(ctx, doc["champion"])["ok"]:
            arms["baseline"] = doc["champion"]
    elif image:
        if t["kind"] == "local":
            raise ValueError("A container image runs on a cloud compute target. Pick a SageMaker target.")
        arms, serve, exp = {"candidate": image}, "candidate", None
    else:
        arms, serve, exp = _pick(ctx, doc, candidate, baseline)
        for r in arms.values():
            s = servable(ctx, r)
            if not s["ok"]:
                raise NotServable(s["text"])
    c = cost(t, ctx)
    cur = state(ctx, pid, env)
    pre = _preflight(ctx, t, pid, env)
    from . import fit as FT

    # Would it fit on this computer? Always answered, so the choice can show "On this computer" as it is.
    local_fit = FT.check(ctx, {k: v for k, v in arms.items() if not endpoint and not image}, allow_large=allow_large) if not image else None
    return {"project": pid, "env": env, "target": t["name"], "kind": t["kind"], "region": t.get("region"),
            "arms": arms, "serve": serve, "experiment": exp, "model_version": arms[serve], "cost": c,
            "needs_confirm": bool(c["billing"]), "local_fit": local_fit, "preflight": pre, "warnings": (pre or {}).get("warnings") or [], "replaces": cur["model_version"] if cur and cur.get("status") == "running" else None,
            "how": "endpoint" if endpoint else "image" if image else "captain",
            "text": (f"Stage {arms[serve]} on " + ("this computer" if t["kind"] == "local" else f"{t['name']} ({t['kind']})")
                     + (f", beside the baseline {arms['baseline']}" if serve == "candidate" and arms.get("baseline") else "")
                     + ". " + c["text"] + "".join(" " + w for w in (pre or {}).get("warnings") or []))}


def _preflight(ctx: Context, target: dict, pid: str, env: str) -> dict | None:
    """Before a cloud deploy: can the serving role write the workspace bucket (for the log) and delete its own
    endpoint (to stop when idle)? Asked of the cloud without changing anything. None for this computer."""
    from .. import compute as C

    if target["kind"] == "local":
        return None
    try:
        return C.stage_preflight(ctx, target, {"project": pid, "env": env, "id": "preflight"})
    except Exception as e:  # noqa: BLE001
        return {"checked": False, "can_delete": None, "can_write": None, "warnings": [],
                "note": f"Captain couldn't check what the serving role is allowed to do ({str(e)[:160]})."}


# ---- local processes -----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.waitpid(int(pid), os.WNOHANG)  # our own finished child stays in the process table until it is collected
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _start_local(ctx: Context, bundle_key: str, bid: str, key_sha: str) -> dict:
    """Start the runtime as a child process on a free loopback port and wait until it answers."""
    import httpx

    from ..workspace import home

    d = os.path.join(home(), "staging")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, f"{bid}.log")
    port = _free_port()
    py = [sys.executable, "-c", "import sys; from captain_search.cli.main import app; sys.argv[0] = 'cst'; app()"]
    argv = [*py, "-w", ctx.workspace.name, "projects", "staging-serve", bundle_key, "--port", str(port)]
    with open(log, "ab") as fh:
        p = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "CST_STAGING_KEY_SHA256": key_sha, "PYTHONUNBUFFERED": "1"})
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if p.poll() is not None:
            break
        try:
            if httpx.get(url + "/ping", timeout=1.0).status_code == 200:
                return {"url": url, "port": port, "pid": p.pid, "host": socket.gethostname(), "log": log}
        except httpx.HTTPError:
            time.sleep(0.15)
    _stop_local({"pid": p.pid, "host": socket.gethostname()})
    tail = ""
    try:
        with open(log, errors="replace") as fh:
            tail = " ".join(fh.read().strip().splitlines()[-3:])[:400]
    except OSError:
        pass
    if not tail or "MemoryError" in tail or "Killed" in tail:
        # A process the system ends for using too much memory leaves no message of its own. Say what most likely happened.
        raise ValueError("The staging endpoint ended while it was loading the catalog, most likely because this computer "
                         "ran out of memory. Close other work and try again, or stage it in your cloud.")
    raise ValueError("The staging endpoint did not start on this computer." + (f" It said: {tail}" if tail else ""))


def _stop_local(ep: dict) -> bool:
    if ep.get("host") != socket.gethostname() or not _alive(ep.get("pid")):
        return False
    try:
        os.killpg(int(ep["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    for _ in range(50):
        if not _alive(ep["pid"]):
            return True
        time.sleep(0.1)
    try:
        os.killpg(int(ep["pid"]), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


# ---- stage, unstage, status ----------------------------------------------------------------------


def _record_deploy(ctx: Context, pid: str, env: str, pipeline: str, target: dict, detail: dict) -> None:
    """Write the deploy into the environment history the existing promote path keeps (`cst deploy history`), so
    `monitor record` and rollback know what is on the endpoint."""
    from .. import ops
    from ..model.entities import Deployment

    name = f"{pid}-{env}"
    try:
        ops.env_load(ctx, name)
    except NotFound:
        ops.env_create(ctx, name, "demo", {"type": "record"} if target["kind"] == "local" else {"type": target["kind"]},
                       compute=None if target["kind"] == "local" else target["name"])
    prev = ops.current(ctx, name)
    d = Deployment(**ops._base(ctx), environment=name, pipeline=pipeline, action="promote",
                   gate_results={"tier": "demo", "gates": [], "overridden": [], "target": detail},
                   rollback_to=prev["pipeline"] if prev else None)
    ops._record(ctx, d, prev)


def stage(ctx: Context, pid: str, candidate: str | None = None, baseline: bool = False, env: str = ENV,
          target: str | None = None, endpoint: str | None = None, endpoint_auth_secret: str | None = None,
          image: str | None = None, confirm: bool = False, idle_minutes: float | None = None, progress=None,
          dry_run: bool = False, allow_large: bool = False) -> dict:
    """Package and start a staging endpoint, and record it on the project. Replaces one that is already running."""
    from .. import compute as C
    from ..workspace import set_secret

    p = plan(ctx, pid, candidate, baseline, env, target, endpoint, image, allow_large)
    if dry_run:
        return {**p, "dry_run": True}
    if p["kind"] == "local" and (p.get("local_fit") or {}).get("outcome") in ("wont-fit", "over-limit"):
        raise ValueError(p["local_fit"]["text"] + (" To stage it here anyway, raise the limit in Settings, or pass --allow-large."
                                                   if p["local_fit"]["outcome"] == "over-limit" else ""))  # refused before anything starts, in the sentence the choice shows
    if p["needs_confirm"] and not confirm:
        raise NeedsConfirm("Confirm the cost first. " + p["cost"]["text"] + "".join(" " + w for w in p["warnings"]), p)
    running_test = (PS.get(ctx, pid).get("ab") or {})
    if running_test.get("status") == "running" and running_test.get("mode") == "captain":
        raise ValueError("An A/B test is running on this endpoint. Roll it back or end it before staging another version.")
    say = progress or (lambda _t: None)
    t = C.get(ctx, p["target"])
    C._check_serving_storage(ctx, t)  # refused here, in a sentence, before a bundle is written
    idle = float(idle_minutes or (state(ctx, pid, env) or {}).get("idle_minutes") or IDLE_MINUTES)
    old = state(ctx, pid, env)
    from . import hosts as _H

    _h = _H.of_kind(t["kind"])
    rec = {"env": env, "status": "starting", "target": t["name"], "kind": t["kind"], "how": p["how"],
           # Which place this is, in the words the deploy surfaces use: local, aws, modal, gcp or azure, and the login.
           "host": _h.cloud if _h else None, "login": t.get("profile"),
           "model_version": p["model_version"], "arms": p["arms"], "serve": p["serve"], "experiment": p["experiment"],
           "started_at": _now(), "started_by": whoami(), "stopped_at": None, "stop_reason": None, "cost": p["cost"],
           "idle_minutes": idle, "resources": [], "endpoint": None, "error": None, "check": None,
           "key_secret": key_name(pid, env), "previous": (old or {}).get("model_version") if old else None,
           "previous_bundle": (old or {}).get("bundle") if old else None}
    # The endpoint key: generated here and kept in the keychain. A machine with no keychain (CI, a headless server)
    # sets it in the environment under the same name instead, and that value is used as is.
    given = os.environ.get(rec["key_secret"], "").strip()
    old_key = None
    if old and old.get("status") == "running" and old.get("key_secret") and not given:
        try:
            old_key = _key(old)  # kept, so a restage that fails can hand the endpoint its key back
        except ValueError:
            old_key = None
    key = given or _secrets.token_urlsafe(32)
    key_sha = hashlib.sha256(key.encode()).hexdigest()

    if p["how"] == "image":
        rec.update(id="stg-" + new_id().lower()[-10:], bundle=None, key_secret=None)
    else:
        say("Packaging the model version")
        arms: dict = dict(p["arms"])
        if p["how"] == "endpoint":
            headers = {"Authorization": f"Bearer {{secret_raw:{endpoint_auth_secret}}}"} if endpoint_auth_secret else {}
            arms["candidate"] = {"pipeline": endpoint, "engine": "http", "spec": {"url": endpoint, "headers": headers},
                                 "kind": "endpoint", "catalog": None, "artifact": {}}
        # A workspace kept on this computer, staged in a cloud: only the serving bundle and the request log go to
        # the serving bucket the target names. The workspace itself stays here.
        store = None
        uri = (t.get("config") or {}).get("serving_storage")
        # The same holds for a host that keeps storage of its own whatever the workspace is on (a Modal volume,
        # the serving bucket of Cloud Run or Container Apps).
        own = bool(uri) and t["kind"] != "local" and C.own_serving_storage(ctx, t)
        if t["kind"] != "local" and uri and uri != ctx.workspace.storage_uri and (
                own or not ctx.workspace.storage_uri.startswith(("s3://", "gs://", "gcs://", "az://", "azure://"))):
            from ..storage.backend import open_store

            store = open_store(uri, t.get("profile"))
            if not store.exists("workspace.yaml"):
                # So the endpoint can open it: a name, no data. The address in it is the serving folder, never the
                # folder on this computer: a path on someone's laptop has no business in a bucket.
                import yaml as _yaml

                doc = _yaml.safe_load(ctx.store.get("workspace.yaml")) or {}
                doc["storage_uri"] = uri
                _put(store, "workspace.yaml", _yaml.safe_dump(doc, sort_keys=False).encode())
            rec.update(serving_storage=uri, serving_profile=t.get("profile"))
        sleeps = t["kind"] != "local" and C.scale_to_zero(ctx, t)
        bundle_key, bundle = build_bundle(ctx, pid, env, arms, p["serve"], idle, store, sleeps=sleeps)
        rec.update(id=bundle["id"], bundle=bundle_key, **({"scale_to_zero": True} if sleeps else {}))
        _save(ctx, pid, env, rec)  # before the control document, so it is written where the endpoint reads it
        if not given:
            try:
                set_secret(rec["key_secret"], key)
            except Exception as e:  # noqa: BLE001
                raise ValueError("This computer has no keychain to keep the staging endpoint's key in "
                                 f"({type(e).__name__}). Set {rec['key_secret']} in the environment and stage again.") from None
        set_control(ctx, pid, env, stage=bundle["id"], serve=p["serve"], split=None, experiment=None, salt=bundle["id"],
                    idle_minutes=idle, shadow=None)
    _save(ctx, pid, env, rec)
    try:
        if t["kind"] == "local":
            say("Starting the endpoint on this computer")
            ep = _start_local(ctx, rec["bundle"], rec["id"], key_sha)
            if old and old.get("kind") == "local" and old.get("status") == "running":
                _stop_local(old.get("endpoint") or {})
            rec.update(endpoint=ep, status="running")
        else:
            if old and old.get("kind") == "local" and old.get("status") == "running":
                _stop_local(old.get("endpoint") or {})
            out = C.stage(ctx, t, {"project": pid, "env": env, "id": rec["id"], "bundle": rec["bundle"],
                                   "key_sha256": key_sha, "image": image, "idle_minutes": idle,
                                   "existing": [r for r in (old or {}).get("resources") or []]
                                   if old and old.get("status") == "running" and old.get("target") == t["name"] else []},
                          progress=say)
            rec.update(endpoint=out["endpoint"], resources=out["resources"], status="running")
    except BaseException as e:
        left = getattr(e, "left_behind", None) or []
        # A restage that failed must not orphan the endpoint that was already up: it is still running, so the
        # project keeps pointing at it, with its key, and says what went wrong.
        was_up = bool(old and old.get("status") == "running")
        still_up = was_up and (
            (old.get("kind") == "local" and _alive((old.get("endpoint") or {}).get("pid")))
            or (old.get("kind") != "local" and old.get("target") == t["name"] and bool(old.get("resources"))
                and {r["name"] for r in old["resources"]} <= {r["name"] for r in left}))
        if still_up:
            if old_key and old.get("key_secret"):
                set_secret(old["key_secret"], old_key)
            if old.get("bundle"):
                set_control(ctx, pid, env, stage=old["id"], serve=old.get("serve"), split=None, experiment=None, salt=old["id"])
            _save(ctx, pid, env, {**old, "error": f"Restaging {p['model_version']} did not work, so {old['model_version']} "
                                                  f"is still running. {str(e)[:400]}"})
        else:
            _save(ctx, pid, env, rec, status="failed", error=str(e)[:600], resources=left, stopped_at=_now(),
                  stop_reason="it did not start")
        raise
    rec = _save(ctx, pid, env, rec)
    for arm_name, r in p["arms"].items():
        if arm_name == p["serve"] and p["how"] == "captain":
            _record_deploy(ctx, pid, env, r, t, {"type": t["kind"], "executed": True,
                                                 "endpoint": (rec["endpoint"] or {}).get("name") or (rec["endpoint"] or {}).get("url")})
    _note(ctx, pid, f"staged {p['model_version']} on {'this computer' if t['kind'] == 'local' else t['name']}")
    return public(rec)


def unstage(ctx: Context, pid: str, env: str = ENV, reason: str | None = None) -> dict:
    """Stop the endpoint and remove every resource Captain created for it. Safe to run twice."""
    from .. import compute as C

    rec = state(ctx, pid, env)
    if not rec:
        raise ValueError(f"Nothing is staged for {pid}.")
    left: list = []
    if rec.get("status") in ("running", "starting", "failed") or rec.get("resources"):
        if rec.get("kind") == "local":
            if rec.get("status") == "running":
                try:
                    call(ctx, rec, "flush", {"publish": True})
                except Exception:  # noqa: BLE001  it may already be gone
                    pass
            _stop_local(rec.get("endpoint") or {})
        elif rec.get("resources"):
            if rec.get("status") == "running" and rec.get("how") != "image":
                try:
                    if not rec.get("scale_to_zero") or _awake(ctx, rec):  # a sleeping copy holds nothing to write
                        r = call(ctx, rec, "flush", {"publish": True})
                    else:
                        r = {"publish_note": "asleep"}
                    if r.get("publish_note"):  # the endpoint's role may only write the log: publish it from here
                        L.publish(ctx, pid, env, rec["id"])
                except Exception:  # noqa: BLE001
                    pass
            left = C.unstage(ctx, C.get(ctx, rec["target"]), rec["resources"])
    if rec.get("key_secret"):
        from ..workspace import delete_secret

        delete_secret(rec["key_secret"])
    was = rec.get("status")
    rec = _save(ctx, pid, env, status="stopped" if not left else "failed", resources=left,
                stopped_at=rec.get("stopped_at") if was == "stopped" else _now(),
                stop_reason=rec.get("stop_reason") if was == "stopped" else (reason or f"stopped by {whoami()}"),
                error=("Captain could not remove: " + ", ".join(f"{r['type']} {r['name']}" for r in left)) if left else None)
    if was != "stopped":
        _note(ctx, pid, f"stopped staging ({rec['stop_reason']})")
        up = _ago(rec.get("started_at"))
        if rec.get("kind") != "local" and up is not None:
            # What it cost while it was up, beside its estimate, and into the project's running total.
            try:
                from .. import compute as C2
                from .. import cost_estimates as CE

                spent = CE.note_actual(ctx, rec["id"], C2.get(ctx, rec["target"]), up, kind="staging", project=pid,
                                       title=f"Staging {rec.get('model_version')}")
                rec = _save(ctx, pid, env, spent={"usd": spent.get("usd"), "text": spent.get("text")})
            except Exception:  # noqa: BLE001  a cost that can't be worked out never blocks a stop
                pass

        def end(doc):  # a test cannot go on without its endpoint
            ab = doc.get("ab") or {}
            if ab.get("status") == "running" and ab.get("mode") == "captain":
                ab["status"], ab["ended_at"] = "finished", _now()
                doc["ab"] = ab

        PS._update(ctx, pid, end)
    return public(rec)


def public(rec: dict | None) -> dict | None:
    """A staging record as the app and agents see it: nothing secret, nothing that names a key."""
    if not rec:
        return None
    out = {k: v for k, v in rec.items() if k not in ("key_secret",)}
    ep = dict(rec.get("endpoint") or {})
    out["endpoint"] = {k: ep.get(k) for k in ("name", "url", "region", "log", "environment", "private", "resource_group") if ep.get(k)} or None
    out["needs_key"] = bool(rec.get("key_secret"))
    return out


def status(ctx: Context, pid: str, env: str = ENV, sweep: bool = True) -> dict:
    """Where staging stands now. Looks at the endpoint itself, records a stop it finds, and (with `sweep`) stops an
    endpoint that has been idle past its limit, so a forgotten one doesn't keep billing."""
    from .. import compute as C

    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "env": env, "status": "none", "staged": None,
                "text": "Nothing is staged. Staging runs a model version as a live endpoint you can try before launch review."}
    out = {"project": pid, "env": env, "status": rec.get("status"), "staged": public(rec), "health": None}
    if rec.get("status") != "running":
        return {**out, "text": _status_text(rec, None)}
    health, gone = None, None
    try:
        if rec.get("kind") == "local":
            if not _alive((rec.get("endpoint") or {}).get("pid")) and (rec.get("endpoint") or {}).get("host") == socket.gethostname():
                gone = "the process ended"
            elif (rec.get("endpoint") or {}).get("host") != socket.gethostname():
                return {**out, "text": f"Running on {(rec.get('endpoint') or {}).get('host')}. Only that computer can reach it."}
            else:
                health = call(ctx, rec, "status", {})
        else:
            s = C.staged_status(ctx, C.get(ctx, rec["target"]), rec)
            if s["state"] == "gone":
                gone = s.get("reason") or "the endpoint no longer exists"
            elif s["state"] == "failed":
                gone = s.get("reason") or "the endpoint failed"
            elif s["state"] == "running" and rec.get("scale_to_zero") and not s.get("copies") and not s.get("ask"):
                # Asleep, or the host doesn't say: asking the endpoint would wake it, and a page that asks every
                # few seconds would then keep it awake and billing. Say so instead. (A host that bills only while
                # it answers sets `ask`: there a question costs a few milliseconds.)
                out["asleep"] = s.get("copies") == 0
                out["copies"] = s.get("copies")
                return {**out, "text": _status_text(rec, None, asleep="yes" if out["asleep"] else "unknown")}
            elif s["state"] == "running" and rec.get("how") != "image":
                out["copies"] = s.get("copies")
                health = call(ctx, rec, "status", {})
            else:
                out["status"] = "starting" if s["state"] == "starting" else out["status"]
    except Exception as e:  # noqa: BLE001
        out["warning"] = f"Captain couldn't reach the endpoint: {str(e)[:200]}"
    if gone:
        why = gone
        try:
            why = json.loads(L.shared(ctx, pid, env).get(RT.stopped_key(pid, env, rec["id"])))["reason"]
        except Exception:  # noqa: BLE001
            pass
        rec = _save(ctx, pid, env, status="stopped", stopped_at=_now(), stop_reason=f"stopped by itself: {why}")
        if rec.get("resources"):
            unstage(ctx, pid, env)
        _note(ctx, pid, f"staging stopped by itself ({why})")
        rec = state(ctx, pid, env)
        return {**out, "status": "stopped", "staged": public(rec), "text": _status_text(rec, None)}
    if health and sweep and not rec.get("scale_to_zero") and health.get("idle_s", 0) >= float(health.get("idle_minutes") or rec.get("idle_minutes") or IDLE_MINUTES) * 60:
        unstage(ctx, pid, env, reason=f"stopped by itself: no requests for {rec.get('idle_minutes') or IDLE_MINUTES:g} minutes")
        rec = state(ctx, pid, env)
        return {**out, "status": "stopped", "staged": public(rec), "text": _status_text(rec, None)}
    out["health"] = health
    if health and health.get("stops_in_s") is not None:
        out["stops_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=health["stops_in_s"])).isoformat(timespec="seconds")
    return {**out, "text": _status_text(rec, health)}


def _span(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def _awake(ctx: Context, rec: dict) -> bool:
    """Is a copy of a sleeping host's endpoint up right now? Unknown counts as yes."""
    from .. import compute as C

    try:
        return C.staged_status(ctx, C.get(ctx, rec["target"]), rec).get("copies") != 0
    except Exception:  # noqa: BLE001
        return True


def _status_text(rec: dict, health: dict | None, asleep: str | None = None) -> str:
    s = rec.get("status")
    where = "on this computer" if rec.get("kind") == "local" else f"on {rec.get('target')}"
    if s == "running":
        from .traffic import where as _where

        w = _where(rec)["text"]
        t = f"{rec['model_version']} is {w[0].lower()}{w[1:]}"
        if health and health.get("shadow"):
            t += " Shadow mode is on: every request is answered with the baseline's results."
        if asleep == "yes":
            t += " Asleep: no copy is up, and it costs nothing. It wakes on the next request."
        elif asleep:
            t += " It sleeps when nobody calls it. Captain didn't ask it how it is, so as not to wake it."
        elif health and health.get("stops_in_s") is None:
            t += f" A copy has been up {_span(health['uptime_s'])}. It sleeps by itself when nobody uses it."
        elif health:
            t += f" Up {_span(health['uptime_s'])}. Stops by itself in {_span(health['stops_in_s'])} if nobody uses it."
        return t
    if s == "starting":
        return f"{rec['model_version']} is starting {where}."
    if s == "failed":
        return f"Staging failed: {rec.get('error') or 'no reason recorded'}"
    return f"Stopped ({rec.get('stop_reason') or 'no reason recorded'})."


def scale(ctx: Context, pid: str, env: str = ENV, min_copies: int | None = None, max_copies: int | None = None,
          idle_seconds: int | None = None) -> dict:
    """How many copies the staged endpoint may run and how long an idle one stays up. With a value, change it: the
    host applies it with no restage. On this computer and on SageMaker there is one copy and nothing to change."""
    from .. import compute as C

    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Staging is not running. Stage a model version first.")
    if rec.get("kind") == "local":
        if any(v is not None for v in (min_copies, max_copies, idle_seconds)):
            raise ValueError("On this computer there is one copy. Stage it in your cloud to run more.")
        return {"project": pid, "min_copies": 0, "max_copies": 1, "idle_seconds": float(rec.get("idle_minutes") or IDLE_MINUTES) * 60, "can_set": [],
                "text": "One copy on this computer. It stops when idle."}
    t = C.get(ctx, rec["target"])
    if all(v is None for v in (min_copies, max_copies, idle_seconds)):
        return {"project": pid, **C.staged_scale(ctx, t, rec)}
    for name, v in (("fewest", min_copies), ("most", max_copies)):
        if v is not None and not 0 <= int(v) <= 100:
            raise ValueError(f"The {name} copies is a number from 0 to 100.")
    if max_copies is not None and int(max_copies) < 1:
        raise ValueError("The most copies is at least 1.")
    out = C.set_staged_scale(ctx, t, rec, min_copies=min_copies, max_copies=max_copies, idle_seconds=idle_seconds)
    _save(ctx, pid, env, scale=out.pop("scale", None) or rec.get("scale"))
    _note(ctx, pid, "changed the staged endpoint's copies: " + out["text"])
    return {"project": pid, **out}


# ---- talking to the endpoint ---------------------------------------------------------------------


def call(ctx: Context, rec: dict, op: str, body: dict, arm: str | None = None, timeout: float = 30.0) -> dict:
    """One call to the staged endpoint: rank, outcome, flush, status or reload."""
    import httpx

    from .. import compute as C

    if rec.get("status") != "running":
        raise ValueError("Staging is not running. Stage a model version first.")
    if rec.get("kind") == "local":
        base = (rec.get("endpoint") or {}).get("url")
        if op == "status":
            r = httpx.get(base + "/healthz", timeout=timeout)
        else:
            path = {"rank": "/rank" + (f"/{arm}" if arm else ""), "outcome": "/outcome", "flush": "/flush",
                    "reload": "/reload"}[op]
            r = httpx.post(base + path, json=body, headers={"Authorization": f"Bearer {_key(rec)}"}, timeout=timeout)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            raise ValueError(f"The staging endpoint refused the request: {detail}")
        return r.json()
    t = C.get(ctx, rec["target"])
    if rec.get("how") == "image":
        if op != "rank":
            raise ValueError("Your own container answers rank requests only. Captain doesn't log or split inside it.")
        t0 = time.perf_counter()
        j = C.invoke_staged(ctx, t, rec, {"query": body.get("query"), "k": body.get("k") or 10})
        return {"request_id": body.get("request_id"), "ids": [str(i) for i in j.get("ids") or []],
                "scores": j.get("scores") or [None] * len(j.get("ids") or []), "arm": "candidate",
                "model_version": rec["model_version"], "ms": round((time.perf_counter() - t0) * 1000, 1)}
    payload = {"op": op, "key": _key(rec), **body}
    if arm:
        payload["arm"] = arm
    return C.invoke_staged(ctx, t, rec, payload)


_products: dict[str, tuple[dict, str, list]] = {}


def read_flush(ctx: Context, rec: dict | None) -> bool:
    """Ask a running endpoint to write its log, before something reads it. This is for reads the page repeats
    (the request list, the readout, shadow mode, how much traffic arrived): on a host that sleeps when idle it
    does nothing while no copy is up, so looking at a page never wakes one and never keeps one awake. Never raises:
    a log a few seconds old is still a log. Returns whether it asked."""
    if not rec or rec.get("status") != "running" or rec.get("how") == "image":
        return False
    awake = globals().get("_awake")  # there once a host that sleeps is supported
    try:
        if rec.get("scale_to_zero") and awake is not None and not awake(ctx, rec):
            return False
        call(ctx, rec, "flush", {})
        return True
    except Exception:  # noqa: BLE001
        return False


def _catalog(ctx: Context, pipeline: str | None):
    """(products by id, title column, image columns with their address templates) for a version's catalog."""
    from .. import features as F
    from .. import media as MD
    from .. import ops

    try:
        pm, _ = ctx.registry.resolve("pipeline", pipeline)
    except Exception:  # noqa: BLE001  an endpoint the team runs has no version here
        return {}, "title", []
    if not pm.catalog:
        return {}, "title", []
    k = f"{ctx.workspace.name}:{pm.catalog}"
    if k not in _products:
        rows = ops.products_of(ctx, pm.catalog)
        cols = list(rows[0]) if rows else []
        try:
            title = F.detect_spec(cols)["title"]
        except ValueError:
            title = "title" if "title" in cols else (cols[1] if len(cols) > 1 else "id")
        name = pm.catalog.split("@")[0]
        tmpl = MD.templates(ctx, name)
        sample = [[r.get(c) for c in cols] for r in rows[:50]]
        images = [(d["column"], tmpl.get(d["column"])) for d in MD.detect(cols, sample)]
        images += [(c, t) for c, t in tmpl.items() if c not in {i[0] for i in images}]
        _products[k] = ({str(r["id"]): r for r in rows}, title, images)
        if len(_products) > 8:
            _products.pop(next(iter(_products)))
    return _products[k]


def _joined(ctx: Context, res: dict, catalog_of: str | None) -> list[dict]:
    from .. import media as MD

    prods, title, images = _catalog(ctx, catalog_of)
    out = []
    for i, pid in enumerate(res["ids"]):
        row = prods.get(str(pid)) or {}
        img = None
        for col, tmpl in images:
            vals = MD._items(row.get(col))
            if vals:
                src = MD.fill(tmpl, vals[0], row)
                if MD.absolute(src):
                    img = src
                    break
        price = row.get("price")
        out.append({"id": str(pid), "position": i + 1, "title": str(row.get(title) or pid), "image": img,
                    "price": price if isinstance(price, (int, float)) else None,
                    "score": res["scores"][i] if i < len(res.get("scores") or []) else None})
    return out


def query(ctx: Context, pid: str, q: str, k: int = 10, env: str = ENV) -> dict:
    """Ask the staged endpoint for results, with the baseline's results for the same query beside them. Console
    requests are logged as test traffic, never as shoppers."""
    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Staging is not running. Stage a model version first.")
    if not str(q or "").strip():
        raise ValueError("Type a query to try.")
    arms = rec.get("arms") or {}
    body = {"query": q, "k": int(k), "traffic": "test", "session_id": "console-" + new_id().lower()[-8:]}
    serve = rec.get("serve") or next(iter(arms))
    cat_of = next((r for r in (arms.get("baseline"), arms.get("candidate")) if r and servable_quiet(ctx, r) == "captain"), None)

    def side(arm: str) -> dict:
        from .traffic import signal_label

        t0 = time.perf_counter()
        res = call(ctx, rec, "rank", {**body, "explain": True}, arm=None if rec.get("how") == "image" else arm)
        rows = _joined(ctx, res, cat_of)
        imp = res.get("importance") or {}
        top = [n for n, _ in sorted(imp.items(), key=lambda kv: -kv[1])[:4]]
        for x in rows:  # "Why is this here?": the signals the model leans on most, with this product's values
            sig = (res.get("signals") or {}).get(x["id"])
            x["why"] = ([{"signal": n, "label": signal_label(n), "value": sig.get(n), "weight": imp[n]} for n in top if n in sig]
                        if sig else None)
        return {"arm": arm, "model_version": res["model_version"], "ms": res["ms"],
                "round_trip_ms": round((time.perf_counter() - t0) * 1000, 1), "request_id": res["request_id"],
                "results": rows,
                "why_note": None if imp else "This version ranks by keyword match alone: there is no trained model to explain."}

    out = {"project": pid, "query": q, "k": int(k), "staged": side(serve), "baseline": None, "traffic": "test"}
    if not rec.get("tried"):
        _save(ctx, pid, env, tried=True)
    if serve != "baseline" and arms.get("baseline") and rec.get("how") != "image":
        out["baseline"] = side("baseline")
        a, b = [r["id"] for r in out["staged"]["results"]], [r["id"] for r in out["baseline"]["results"]]
        out["same_order"] = a == b
        out["moved"] = sum(1 for i, x in enumerate(a) if i >= len(b) or b[i] != x)
        for i, x in enumerate(out["staged"]["results"]):  # against the baseline: up, down, new, or where it was
            was = b.index(x["id"]) if x["id"] in b else None
            x["move"] = "new" if was is None else "same" if was == i else "up" if was > i else "down"
            x["was"] = None if was is None else was + 1
    return out


def servable_quiet(ctx: Context, pipeline: str) -> str | None:
    try:
        return servable(ctx, pipeline)["how"]
    except Exception:  # noqa: BLE001
        return None


# ---- the Ship path, from the project's state only (no call to the endpoint) -------------------------


def ship_state(ctx: Context, doc: dict, env: str = ENV) -> dict:
    """Where the project stands on the way to launch review: what is staged, the steps with their status, and the
    next thing to do. Reads the state doc only, so the project page can ask for it on every load."""
    rec = (doc.get("staging") or {}).get(env) or None
    on = bool(rec and rec.get("status") == "running")
    champ = doc.get("champion")
    exps = [e for e in doc.get("experiments") or [] if e["pipeline"] != champ]
    judged = [e for e in exps if e.get("status") not in ("trained", "failed", "backtesting", None)]
    cand = (judged or exps or [None])[-1]
    steps = [
        {"n": 1, "id": "stage", "title": "Stage", "done": on,
         "status": (f"{rec['model_version']} is running " + ("on this computer. Only this computer can reach it." if rec.get("kind") == "local" else f"on {rec.get('target')}. Your site can call it.")) if on
         else "Not staged." if not rec or rec.get("status") == "stopped" else f"Staging {rec.get('status')}.",
         "action": None if on else {"action": "stage", "experiment": cand["id"]} if cand else {"action": "stage", "baseline": True} if champ else None},
        {"n": 2, "id": "try", "title": "Try it", "done": on and bool(rec.get("tried")),
         "status": "Tried." if on and rec.get("tried") else "Type a query and see the results." if on else "Waits for staging.",
         "action": {"action": "try"} if on and not rec.get("tried") else None},
    ]
    connected = on and bool(rec.get("connected_at"))
    local = bool(rec and rec.get("kind") == "local")
    sh = doc.get("shadow") or None
    steps.append({"n": 3, "id": "connect", "title": "Connect your site", "done": connected,
                  "status": ("Your site is sending requests." + (" Shadow mode is on: shoppers see the baseline." if sh and sh.get("status") == "running" else "")) if connected
                  else "A site can't reach an endpoint on this computer. Stage it in your cloud, or keep testing here with simulated shoppers." if on and local
                  else "Waiting for the first request from your site." if on else "Waits for staging.",
                  "action": {"action": "connect"} if on and not connected else None})
    ab = doc.get("ab") or None
    both = on and {"baseline", "candidate"} <= set(rec.get("arms") or {}) and rec.get("how") != "image"
    last = (ab or {}).get("last_readout") or {}
    brought = ((ab or {}).get("brought") or [None])[-1]
    ab_done = bool(ab and (last.get("enough") or brought))
    if not ab:
        ab_status = ("Split traffic between the baseline and the candidate." if both else
                     "Waits for a candidate staged beside the baseline.")
    elif ab["status"] == "running":
        ab_status = ((f"The candidate gets {ab['split']:.0%} of traffic. " if ab.get("split") is not None else "Running in your own A/B system. ")
                     + (last.get("text") or (brought or {}).get("text") or "No readout yet."))
    else:
        ab_status = ("Rolled back to the baseline. " if ab["status"] == "rolled_back" else "Ended. ") + (last.get("text") or (brought or {}).get("text") or "")
    steps.append({"n": 4, "id": "ab", "title": "A/B test", "done": ab_done, "status": ab_status.strip(),
                  "action": {"action": "ab-start"} if both and (not ab or ab["status"] != "running") else None})
    review_of = (ab or {}).get("arms", {}).get("candidate") or (rec or {}).get("arms", {}).get("candidate") or champ
    chk = (rec or {}).get("check") or {}
    sent = False
    if review_of and (rec or ab):
        from .. import handoff as HO

        try:
            sent = bool(HO.listing(ctx, review_of))
        except Exception:  # noqa: BLE001
            sent = False
    steps.append({"n": 5, "id": "launch-review", "title": "Launch review", "done": sent,
                  "status": ("Sent to launch review." if sent else
                             "Served features don't match training, so launch review will ask for a reason to go on." if chk.get("status") == "fail" else
                             "Check it against your launch criteria and pass it to your own deployment process. Captain deploys nothing itself."),
                  "action": {"action": "handoff", "pipeline": review_of} if review_of and not sent else None})
    # The next thing to do. A test that is running comes first; an ended one no longer decides anything.
    live_ab = bool(ab and ab["status"] == "running")
    shipped = bool(ab and (ab.get("decision") or {}).get("choice") == "ship")
    real = not (ab or {}).get("practice") and (not last.get("evidence") or last.get("evidence") == "REAL_TRAFFIC")
    nxt = None
    if shipped and not sent and review_of:
        nxt = {"action": "handoff", "pipeline": review_of, "text": f"Send {review_of} to launch review."}
    elif judged and not on:
        nxt = {"action": "stage", "experiment": judged[-1]["id"], "text": f"Stage {judged[-1]['pipeline']} to try it live."}
    elif on and not rec.get("tried"):
        nxt = {"action": "try", "text": "Try the staged model: type a query under Ship, step 2."}
    elif live_ab and not ab_done:
        nxt = ({"action": "ab-wait", "text": "The practice test needs more sessions. Send simulated shoppers at staging."} if ab.get("practice")
               else {"action": "ab-watch", "text": "The A/B test is collecting sessions. The readout under Ship says how many more it needs."})
    elif live_ab and real and not ab.get("decision"):
        nxt = {"action": "ab-decide", "text": "The A/B test has enough sessions. Decide: ship it, keep the baseline, or keep testing."}
    elif live_ab and ab.get("practice"):
        nxt = {"action": "connect", "text": "That was practice on simulated shoppers. To test for real, connect your site (Ship, step 3)."}
    elif on and not connected and not live_ab:
        nxt = ({"action": "connect", "text": "A site can't reach this computer. Stage it in your cloud, or practice here with simulated shoppers (Ship, step 3)."}
               if local else {"action": "connect", "text": "Connect your site: copy the snippet under Ship, step 3, and send the first request."})
    elif both and connected and not live_ab:
        nxt = ({"action": "shadow-start", "text": "Your site is connected. Start shadow mode to see what the candidate would change, before any shopper sees it."}
               if not sh else {"action": "ab-start", "text": "Start an A/B test: split traffic between the baseline and the candidate."})
    return {"staging": public(rec), "steps": steps, "next": nxt}


def agent_summary(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Staging as an agent reads it (`cst context show`): what runs where, and the steps. No key, no secret."""
    doc = PS.get(ctx, pid)
    sh = ship_state(ctx, doc, env)
    rec = sh["staging"] or {}
    return {"project": pid, "status": rec.get("status") or "none", "model_version": rec.get("model_version"),
            "arms": rec.get("arms"), "runs_on": "this computer" if rec.get("kind") == "local" else rec.get("target"),
            "endpoint": rec.get("endpoint"), "started_at": rec.get("started_at"), "cost": (rec.get("cost") or {}).get("text"),
            "stops_after_idle_minutes": rec.get("idle_minutes"), "stop_reason": rec.get("stop_reason"),
            "served_features_check": (rec.get("check") or {}).get("text"),
            "ab_test": ({k: (doc.get("ab") or {}).get(k) for k in ("id", "status", "mode", "split", "primary", "guardrails", "arms")}
                        | {"readout": ((doc.get("ab") or {}).get("last_readout") or {}).get("text")}) if doc.get("ab") else None,
            "steps": [{"label": f"{s['n']} {s['title']}", "done": s["done"], "status": s["status"]} for s in sh["steps"]],
            "next": sh["next"], "commands": {"stage": f"cst projects stage {pid} --target local", "status": f"cst projects staging {pid}",
                                             "try": f"cst projects staging-query {pid} \"<query>\"", "check": f"cst projects staging-check {pid}",
                                             "ab": f"cst projects ab {pid}", "stop": f"cst projects unstage {pid}"}}
