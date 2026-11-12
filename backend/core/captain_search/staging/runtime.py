"""The staging runtime: one process that loads a serving bundle and answers `POST /rank`.

The same code runs as a local process on a loopback port (`--target local`) and inside the runner image on the
team's own cloud (SageMaker). It reads the bundle from the workspace's storage (bundle.py wrote it), so a local
staging endpoint and a cloud one serve exactly the same files.

  POST /rank        {query, k?, user_id?, session_id?, request_id?, traffic?} -> {request_id, ids, scores, arm,
                    model_version, ms}. Keyword search finds the candidates and the trained model reorders the
                    top K with features from the module that built its training set (features/).
  POST /rank/<arm>  the same, from one named arm (baseline or candidate): for the Try it console's side by side
                    view, and for a team that splits traffic in its own A/B system.
                    With "explain": true the answer also carries, for each result, the signals the model was
                    given (`signals`), and how much the model leans on each one (`importance`).
  POST /outcome     {request_id, event: click | add-to-cart | purchase, product_id?, value?}
  POST /flush       write buffered log records now (and publish the logs dataset with {"publish": true})
  GET  /healthz     what is loaded, uptime, requests served, and when it stops by itself. No secrets.
  GET  /ping, POST /invocations   SageMaker's contract. /invocations takes {"op": "rank" | "outcome" | "flush" |
                    "status", "key": ..., ...the op's own fields}, because SageMaker exposes one route.

Every route but /ping and /healthz needs the endpoint key: `Authorization: Bearer <key>` locally, `"key"` in the
body through SageMaker. The process only ever holds the key's SHA-256.

Traffic split. With a split set in the control document (`staging/<project>/<env>/control.json`), a request goes
to the candidate when a hash of its session id (else user id, else request id) falls under the split, so one
shopper always sees one arm, and raising the split only ever moves shoppers from baseline to candidate. The arm
is logged on every request. The control document is re-read every few seconds, so a ramp or a rollback needs no
redeploy.

Shadow mode. With `shadow` set in the control document, every request is answered with the baseline's ranking,
exactly as before, while the candidate ranks the same request silently. Both rankings are logged on the request.
No shopper sees the candidate. It comes before any split: it shows how different the candidate is on the site's
real queries, and what it costs in response time, at no risk.

Logging. Every request is logged with the feature rows it served (logs.py). Records are buffered, written as
write-once chunks, and published as a version of the project's staging-logs dataset every few minutes. In the
cloud that needs the serving role to write to the workspace bucket.

Stopping by itself. After `idle_minutes` with no /rank or /outcome the runtime flushes its log, leaves a marker in
storage and stops: a local process exits, a SageMaker container asks SageMaker to delete its own endpoint.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time

from ..model.entities import new_id
from ..storage.backend import AlreadyExists, NotFound
from . import logs as L

CONTROL_TTL_S = 3.0
LOG_TOP = 10  # feature rows are logged for this many of the results returned
ARMS = ("baseline", "candidate")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def control_key(pid: str, env: str) -> str:
    return f"staging/{pid}/{env}/control.json"


def stopped_key(pid: str, env: str, stage: str) -> str:
    return f"staging/{pid}/{env}/stopped/{stage}.json"


def bucket(salt: str, unit: str) -> float:
    """Where a shopper falls in [0, 1): the same shopper and salt always give the same place."""
    return int(hashlib.sha256(f"{salt}:{unit}".encode()).hexdigest()[:12], 16) / float(16 ** 12)


def assign(control: dict, arms, unit: str) -> str:
    """The arm a request is served from. No split: the arm the endpoint serves. A split: candidate under it."""
    split = control.get("split")
    if split is None or not {"baseline", "candidate"} <= set(arms):
        serve = control.get("serve")
        return serve if serve in arms else next(iter(arms))
    return "candidate" if bucket(str(control.get("salt") or ""), unit) < float(split) else "baseline"


class Arm:
    def __init__(self, name: str, spec: dict, engine):
        self.name, self.pipeline, self.engine = name, spec["pipeline"], engine
        self.names = list(getattr(getattr(engine, "feat", None), "names", None) or [])
        self.kind = spec.get("kind") or "captain"
        model = getattr(engine, "model", None)
        self.importance = model.importance() if model is not None and hasattr(model, "importance") else {}

    def rank(self, query: str, k: int):
        if hasattr(self.engine, "search_detailed"):
            return self.engine.search_detailed(query, k)
        return self.engine.search_scored(query, k), {}


def load_arm(store, bundle_key: str, name: str, spec: dict, catalogs: dict) -> Arm:
    """One arm of a bundle as a ranker: the engine a pipeline version names, over the bundle's own files."""
    import io

    import pyarrow.parquet as pq

    from ..engines import make_engine

    prods = None
    if spec.get("catalog"):
        if spec["catalog"] not in catalogs:
            rows = pq.read_table(io.BytesIO(store.get(spec["catalog"]))).to_pylist()
            for r in rows:
                r["id"] = str(r["id"])
            catalogs[spec["catalog"]] = rows
        prods = catalogs[spec["catalog"]]
    files = spec.get("artifact") or {}

    def artifact(path: str):
        f = files.get(str(path).lstrip("/"))
        return store.get(f"{bundle_key}/{f}") if f else None

    return Arm(name, spec, make_engine(spec["engine"], spec.get("spec") or {}, prods, artifact=artifact))


class Runtime:
    def __init__(self, ctx, bundle_key: str, *, key_sha256: str | None = None, flush_s: float = 15.0,
                 publish_minutes: float = 5.0, idle_minutes: float | None = None, on_stop=None, clock=time.time):
        self.ctx, self.store, self.bundle_key = ctx, ctx.store, bundle_key.rstrip("/")
        self.bundle = json.loads(self.store.get(f"{self.bundle_key}/bundle.json"))
        self.pid, self.env, self.stage = self.bundle["project"], self.bundle["env"], self.bundle["id"]
        catalogs: dict = {}
        self.arms = {n: load_arm(self.store, self.bundle_key, n, s, catalogs) for n, s in self.bundle["arms"].items()}
        self.key_sha256 = key_sha256
        self.flush_s, self.publish_s = float(flush_s), float(publish_minutes) * 60.0
        self.clock, self.on_stop = clock, on_stop
        self.started = self.last_used = self.last_flush = self.last_publish = clock()
        self.started_at = _now()
        self.requests = self.outcomes = 0
        self.by_traffic = {k: 0 for k in L.TRAFFIC}  # how many requests of each kind: only "real" is a site calling
        self.last_real_at: str | None = None
        self.running = 0  # requests being answered right now
        self._lock = threading.Lock()
        self._req: list[dict] = []
        self._out: list[dict] = []
        self._unpublished = False
        self._seen: dict[str, str] = {}  # request id -> arm, for answering /outcome; bounded
        self._control = dict(self.bundle.get("control") or {})
        self._control_at = 0.0
        self._idle_default = float(idle_minutes if idle_minutes is not None else self.bundle.get("idle_minutes") or 240)
        self.stopped: str | None = None
        self.control()

    # ---- control -------------------------------------------------------------------------------

    def control(self, force: bool = False) -> dict:
        now = self.clock()
        if force or now - self._control_at >= CONTROL_TTL_S:
            try:
                doc = json.loads(self.store.get(control_key(self.pid, self.env)))
                if doc.get("stage") in (None, self.stage):
                    self._control = doc
            except NotFound:
                pass
            except Exception:  # noqa: BLE001  a storage hiccup keeps the last known control
                pass
            self._control_at = now
        return self._control

    @property
    def idle_minutes(self) -> float:
        v = self._control.get("idle_minutes")
        return float(v) if v else self._idle_default

    def authorized(self, key: str | None) -> bool:
        if not self.key_sha256:
            return True
        return bool(key) and hashlib.sha256(str(key).encode()).hexdigest() == self.key_sha256

    # ---- the work ------------------------------------------------------------------------------

    def rank(self, body: dict, arm: str | None = None) -> dict:
        query = str(body.get("query") or "")
        if not query.strip():
            raise ValueError("a rank request needs a query")
        k = max(1, min(200, int(body.get("k") or 10)))
        rid = str(body.get("request_id") or new_id())
        session, user = body.get("session_id"), body.get("user_id")
        traffic = body.get("traffic") if body.get("traffic") in L.TRAFFIC else "unlabelled"  # never real by default
        ctl = self.control()
        if arm is not None and arm not in self.arms:
            raise ValueError(f"this endpoint has no {arm} arm. Loaded: {', '.join(self.arms)}")
        shadowing = arm is None and bool(ctl.get("shadow")) and {"baseline", "candidate"} <= set(self.arms)
        name = arm or ("baseline" if shadowing else assign(ctl, self.arms, str(session or user or rid)))
        a = self.arms[name]
        t0 = time.perf_counter()
        self.running += 1
        try:
            ranked, feats = a.rank(query, k)
        finally:
            self.running -= 1
        ms = round((time.perf_counter() - t0) * 1000, 3)
        ids = [str(p) for p, _ in ranked]
        shadow = None
        if shadowing:  # the candidate ranks the same request, and nobody sees it
            c = self.arms["candidate"]
            t1 = time.perf_counter()
            try:
                c_ranked, _ = c.rank(query, k)
                shadow = {"arm": "candidate", "model_version": c.pipeline, "ids": [str(p) for p, _ in c_ranked],
                          "ms": round((time.perf_counter() - t1) * 1000, 3), "id": ctl.get("shadow")}
            except Exception as e:  # noqa: BLE001  a candidate that breaks must never break the answer the site gets
                shadow = {"arm": "candidate", "model_version": c.pipeline, "ids": [], "ms": None, "id": ctl.get("shadow"),
                          "error": str(e)[:200]}
        rec = {"request_id": rid, "ts": _now(), "query": query, "k": k, "user_id": user, "session_id": session,
               "arm": name, "model_version": a.pipeline, "ids": ids,
               "scores": [None if s is None else round(float(s), 6) for _, s in ranked],
               "features": {p: feats[p] for p in ids[:LOG_TOP] if p in feats}, "ms": ms, "traffic": traffic,
               "experiment": ctl.get("experiment") if arm is None and ctl.get("split") is not None else None,
               "forced": arm is not None}
        if shadow:
            rec["shadow"] = shadow
        with self._lock:
            self._req.append(rec)
            self.requests += 1
            self.by_traffic[traffic] += 1
            if traffic == "real":
                self.last_real_at = rec["ts"]
            self.last_used = self.clock()
            self._seen[rid] = name
            if len(self._seen) > 50_000:
                for old in list(self._seen)[:10_000]:
                    del self._seen[old]
        out = {"request_id": rid, "ids": ids, "scores": rec["scores"], "arm": name, "model_version": a.pipeline, "ms": ms}
        if body.get("explain"):
            out["signals"] = {p: dict(zip(a.names, feats[p])) for p in ids if p in feats}
            out["importance"] = a.importance
        return out

    def outcome(self, body: dict) -> dict:
        rid, ev = str(body.get("request_id") or ""), body.get("event")
        if not rid:
            raise ValueError("an outcome needs the request_id that /rank returned")
        if ev not in L.EVENTS:
            raise ValueError(f"event is one of {', '.join(L.EVENTS)}")
        rec = {"request_id": rid, "event": ev, "product_id": None if body.get("product_id") is None else str(body["product_id"]),
               "value": float(body.get("value") or 0.0), "ts": _now()}
        with self._lock:
            self._out.append(rec)
            self.outcomes += 1
            self.last_used = self.clock()
        return {"ok": True, "request_id": rid, "known": rid in self._seen}

    def flush(self, publish: bool = False) -> dict:
        with self._lock:
            req, out = self._req, self._out
            self._req, self._out = [], []
            self.last_flush = self.clock()
        base = L.prefix(self.pid, self.env, self.stage)
        cid = new_id()
        try:
            if req:
                meta = {"_meta": {"names": {n: a.names for n, a in self.arms.items() if a.names}}}
                self.store.put_new(f"{base}requests-{cid}.jsonl", "\n".join(json.dumps(x) for x in [meta, *req]).encode())
            if out:
                self.store.put_new(f"{base}outcomes-{cid}.jsonl", "\n".join(json.dumps(x) for x in out).encode())
        except Exception:
            with self._lock:  # nothing is lost to a failed write: the records go back in the buffer
                self._req, self._out = req + self._req, out + self._out
            raise
        if req or out:
            self._unpublished = True
        ref = None
        note = None
        if publish and self._unpublished and self.bundle.get("serving_store"):
            # This endpoint's storage holds the bundle and the log only, not the workspace. Captain publishes.
            note = "not published from the endpoint: its storage holds only the serving bundle and the log"
        elif publish and self._unpublished:
            # A serving role that may only write the request log can't publish a dataset version. That is fine: the
            # chunks are written, and Captain publishes the version from them on its side.
            self.last_publish = self.clock()
            try:
                m = L.publish(self.ctx, self.pid, self.env, self.stage)
                self._unpublished = False
                ref = f"{m.name}@v{m.version}" if m else None
            except Exception as e:  # noqa: BLE001
                note = f"not published from the endpoint: {str(e)[:160]}"
        return {"requests": len(req), "outcomes": len(out), "published": ref, **({"publish_note": note} if note else {})}

    def status(self) -> dict:
        now = self.clock()
        ctl = self._control
        idle = now - self.last_used
        return {"ok": self.stopped is None, "project": self.pid, "env": self.env, "stage": self.stage,
                "arms": {n: a.pipeline for n, a in self.arms.items()}, "serve": ctl.get("serve"), "split": ctl.get("split"),
                "experiment": ctl.get("experiment"), "started_at": self.started_at, "uptime_s": round(now - self.started),
                "requests": self.requests, "outcomes": self.outcomes, "running": max(0, self.running), "traffic": dict(self.by_traffic),
                "last_real_at": self.last_real_at, "shadow": ctl.get("shadow") or None, "idle_s": round(idle),
                "idle_minutes": self.idle_minutes,
                "stops_in_s": None if self.bundle.get("scale_to_zero") else max(0, round(self.idle_minutes * 60 - idle)),
                "stopped": self.stopped}

    # ---- housekeeping --------------------------------------------------------------------------

    def tick(self) -> None:
        """Called about once a second: write the log when it is due, and stop when nobody has used the endpoint."""
        now = self.clock()
        if self.stopped:
            return
        due = now - self.last_flush >= self.flush_s or len(self._req) >= 500
        publish = self._unpublished and now - self.last_publish >= self.publish_s
        if (due and (self._req or self._out)) or publish:
            try:
                self.flush(publish=now - self.last_publish >= self.publish_s)
            except Exception as e:  # noqa: BLE001
                print(f"staging: could not write the log: {e}", flush=True)
        self.control()
        # On a host that sleeps when idle, the host takes the copies away. Stopping here would leave one answering 503.
        if not self.bundle.get("scale_to_zero") and now - self.last_used >= self.idle_minutes * 60:
            self.stop(f"no requests for {self.idle_minutes:g} minutes")

    def stop(self, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = reason
        try:
            self.flush(publish=True)
        except Exception as e:  # noqa: BLE001
            print(f"staging: could not write the log while stopping: {e}", flush=True)
        try:
            self.store.put_new(stopped_key(self.pid, self.env, self.stage),
                               json.dumps({"at": _now(), "reason": reason, "requests": self.requests}).encode())
        except AlreadyExists:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"staging: could not record the stop: {e}", flush=True)
        if self.on_stop:
            self.on_stop(reason)


def make_app(rt: Runtime):
    from fastapi import FastAPI, Header, HTTPException

    api = FastAPI(title=f"Captain staging: {rt.pid}")

    def check(authorization: str = "", key: str | None = None) -> None:
        tok = key or (authorization[7:] if authorization.lower().startswith("bearer ") else None)
        if not rt.authorized(tok):
            raise HTTPException(401, "missing or wrong endpoint key")

    def run(fn, *a):
        if rt.stopped:
            raise HTTPException(503, f"this endpoint stopped: {rt.stopped}")
        try:
            return fn(*a)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @api.get("/ping")
    def ping():
        return {"ok": True}

    @api.get("/healthz")
    def healthz():
        return rt.status()

    @api.post("/rank")
    def rank(body: dict, authorization: str = Header(default="")):
        check(authorization)
        return run(rt.rank, body)

    @api.post("/rank/{arm}")
    def rank_arm(arm: str, body: dict, authorization: str = Header(default="")):
        check(authorization)
        return run(rt.rank, body, arm)

    @api.post("/outcome")
    def outcome(body: dict, authorization: str = Header(default="")):
        check(authorization)
        return run(rt.outcome, body)

    @api.post("/flush")
    def flush(body: dict | None = None, authorization: str = Header(default="")):
        check(authorization)
        return run(rt.flush, bool((body or {}).get("publish")))

    @api.post("/reload")
    def reload(authorization: str = Header(default="")):
        check(authorization)
        return rt.control(force=True)

    @api.post("/invocations")
    def invocations(body: dict):
        body = dict(body)
        check(key=body.pop("key", None) or "")
        op = body.pop("op", "rank")
        if op == "rank":
            return run(rt.rank, body, body.pop("arm", None))
        if op == "outcome":
            return run(rt.outcome, body)
        if op == "flush":
            return run(rt.flush, bool(body.get("publish")))
        if op == "status":
            return rt.status()
        if op == "reload":
            return rt.control(force=True)
        raise HTTPException(400, f"unknown op {op!r}: rank, outcome, flush, status or reload")

    return api


def _self_delete_endpoint(reason: str) -> None:
    """Inside SageMaker: ask SageMaker to delete this endpoint, so an idle one stops billing. Needs
    sagemaker:DeleteEndpoint on the execution role; without it the endpoint stays up and Captain stops it the next
    time the app or `cst projects staging` looks."""
    name = os.environ.get("CST_STAGING_ENDPOINT")
    if not name:
        return
    try:
        import boto3

        boto3.client("sagemaker", region_name=os.environ.get("CST_STAGING_REGION") or None).delete_endpoint(EndpointName=name)
        print(f"staging: asked SageMaker to delete {name}: {reason}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"staging: could not delete {name} from inside ({e}). Stop it from Captain.", flush=True)


def serve(ctx, bundle_key: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the runtime until it is stopped or has been idle too long. This is `cst projects staging-serve`."""
    import uvicorn

    cloud = bool(os.environ.get("CST_STAGING_ENDPOINT"))
    done = threading.Event()

    def on_stop(reason: str) -> None:
        if cloud:
            _self_delete_endpoint(reason)
        done.set()

    try:
        rt = Runtime(ctx, bundle_key, key_sha256=os.environ.get("CST_STAGING_KEY_SHA256") or None,
                     flush_s=float(os.environ.get("CST_STAGING_FLUSH_S") or 15),
                     publish_minutes=float(os.environ.get("CST_STAGING_PUBLISH_MINUTES") or 5), on_stop=on_stop)
    except MemoryError:
        # Stop with a sentence, not a stack trace: the catalog and its indexes did not fit in memory.
        print("MemoryError: the catalog and its indexes did not fit in this machine's memory while loading.", flush=True)
        raise SystemExit(3) from None
    server = uvicorn.Server(uvicorn.Config(make_app(rt), host=host, port=port, log_level="warning"))

    def loop() -> None:
        while not done.is_set() and not server.should_exit:
            time.sleep(1.0)
            rt.tick()
        if not cloud:  # a cloud container keeps answering 503 until SageMaker takes it down
            server.should_exit = True

    threading.Thread(target=loop, daemon=True).start()
    print(f"staging: serving {', '.join(f'{n}={a.pipeline}' for n, a in rt.arms.items())} on {host}:{port}", flush=True)
    try:
        server.run()
    finally:
        if not rt.stopped:
            rt.stopped = "stopped"
            try:
                rt.flush(publish=True)
            except Exception:  # noqa: BLE001
                pass
