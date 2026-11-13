"""The staging runtime: one process that loads a serving bundle and answers `POST /rank`.

The same code runs as a local process on a loopback port (`--target local`) and inside the runner image on the
team's own cloud (SageMaker). It reads the bundle from the workspace's storage (bundle.py wrote it), so a local
staging endpoint and a cloud one serve exactly the same files.

  POST /rank        {query, k?, user_id?, session_id?, request_id?, traffic?} -> {request_id, ids, scores, arm,
                    model_version, ms}. Keyword search finds the candidates and the trained model reorders the
                    top K with features from the module that built its training set (features/).
  POST /rank/<arm>  the same, from one named model (baseline or candidate): for the Try it console's side by side
                    view, and so a team's own testing tool can send each group of shoppers to its model.
                    With "explain": true the answer also carries, for each result, the signals the model was
                    given (`signals`), and how much the model leans on each one (`importance`).
  POST /timing      {request_id, ms}: how long the request took as the site saw it, network included.
  POST /flush       write buffered log records now (and publish the logs dataset with {"publish": true})
  GET  /healthz     what is loaded, uptime, requests served, and when it stops by itself. No secrets.
  GET  /ping, POST /invocations   SageMaker's contract. /invocations takes {"op": "rank" | "timing" | "flush" |
                    "status", "key": ..., ...the op's own fields}, because SageMaker exposes one route.

Every route but /ping and /healthz needs the endpoint key: `Authorization: Bearer <key>` locally, `"key"` in the
body through SageMaker. The process only ever holds the key's SHA-256.

No split, and no shopper outcomes. Captain does not assign shoppers to models and does not collect what they
clicked or bought: that is the job of the team's own testing tool, which sends each group to its model's address.
A `split` left in an old control document is ignored. `POST /outcome` answers 410 with a sentence.

Shadow mode. With `shadow` set in the control document, every request is answered with the baseline's ranking,
exactly as before, while the candidate ranks the same request silently. Both rankings are logged on the request.
No shopper sees the candidate. It shows how different the candidate is on the site's real queries, and what it
costs in response time, at no risk.

Logging. Every request is logged with the feature rows it served (logs.py). Records are buffered, written as
write-once chunks, and published as a version of the project's staging-logs dataset every few minutes. In the
cloud that needs the serving role to write to the workspace bucket.

Stopping by itself. After `idle_minutes` with no /rank the runtime flushes its log, leaves a marker in
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


def _memory_mb() -> float | None:
    """The most memory this copy has used, in MB."""
    try:
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except Exception:  # noqa: BLE001
        return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def control_key(pid: str, env: str) -> str:
    return f"staging/{pid}/{env}/control.json"


def stopped_key(pid: str, env: str, stage: str) -> str:
    return f"staging/{pid}/{env}/stopped/{stage}.json"


def copies_prefix(pid: str, env: str, stage: str) -> str:
    """Where each running copy of one deployment says when it was last used: one small file a copy."""
    return f"staging/{pid}/{env}/copies/{stage}/"


def assign(control: dict, arms, unit: str | None = None) -> str:
    """The model a request is served from: the one the deployment serves. There is no split: a `split` left in an
    old control document is ignored."""
    serve = control.get("serve")
    return serve if serve in arms else next(iter(arms))


OUTCOMES_GONE = ("Captain no longer collects what shoppers clicked or bought. Keep that in your own testing tool. To report how long a request took at your site, POST /timing with request_id and ms.")


MAX_REQUEST_KB = 256  # until the control document says otherwise (staging/protect.py)


class Refused(ValueError):
    """A request refused for what it sent. `status` is the HTTP status the site gets with the sentence."""
    status = 400
    headers: dict | None = None


class TooLarge(Refused):
    status = 413


class TooMany(Refused):
    status = 429
    headers = {"Retry-After": "1"}


class TooSlow(Exception):
    """The model took longer than its time budget and there was no other model to answer in its place."""
    status = 504


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
        t_load = time.perf_counter()
        self.arms = {n: load_arm(self.store, self.bundle_key, n, s, catalogs) for n, s in self.bundle["arms"].items()}
        self.load_s = round(time.perf_counter() - t_load, 3)  # how long this copy took to be ready to answer
        self.copy = new_id()[-8:].lower()  # this running copy: one process, or one container
        self.key_sha256 = key_sha256
        self.flush_s, self.publish_s = float(flush_s), float(publish_minutes) * 60.0
        self.clock, self.on_stop = clock, on_stop
        self.started = self.last_used = self.last_flush = self.last_publish = clock()
        self._beat_at, self._beat_used, self._beat_tag = 0.0, None, None
        self.started_at = _now()
        self.requests = 0
        self.by_traffic = {k: 0 for k in L.TRAFFIC}  # how many requests of each kind: only "real" is a site calling
        self.last_real_at: str | None = None
        self.running = 0  # requests being answered right now
        self._lock = threading.Lock()
        self._req: list[dict] = []
        self._lines: list[dict] = []    # log lines, each with the request it belongs to when there is one
        self._timings: list[dict] = []  # how long a request took as the site saw it, when the site reports it
        self.errors = self.refused = 0  # requests that failed in here, and requests refused for what they sent
        self.fallbacks = 0  # requests answered with the compared model's results because this one was slow or failed
        self._tokens, self._tokens_at = None, 0.0  # the rate limit's bucket
        self._key_reload_at = 0.0
        self._pool = None  # threads for requests run against a time budget
        self._unpublished = False
        self._control = dict(self.bundle.get("control") or {})
        self._control_at = 0.0
        self._idle_default = float(idle_minutes if idle_minutes is not None else self.bundle.get("idle_minutes") or 240)
        self.stopped: str | None = None
        self.control()
        self.say(f"Ready in {self.load_s:g} s: " + ", ".join(f"{n} is {a.pipeline}" for n, a in self.arms.items()) + ".")

    def say(self, text: str, request_id: str | None = None, level: str = "info") -> None:
        """One log line, with the time to the millisecond. Lines of requests handled at the same time interleave."""
        with self._lock:
            self._lines.append({"ts": _now(), "text": text, "level": level, "copy": self.copy,
                                **({"request_id": request_id} if request_id else {})})
            if len(self._lines) > 20_000:  # a storage outage must not grow this without bound
                del self._lines[:5_000]

    # ---- control -------------------------------------------------------------------------------

    def control(self, force: bool = False) -> dict:
        now = self.clock()
        if force or now - self._control_at >= CONTROL_TTL_S:
            try:
                doc = json.loads(self.store.get(control_key(self.pid, self.env)))
                if doc.get("stage") in (None, self.stage):
                    old = self._control
                    self._control = doc
                    if self._control_at and any(old.get(k) != doc.get(k) for k in ("serve", "shadow", "idle_minutes")):
                        self.say("Traffic changed: " + ("shadow mode, shoppers get the baseline" if doc.get("shadow")
                                                        else f"every request gets the {doc.get('serve') or 'candidate'}")
                                 + f". Stops after {float(doc.get('idle_minutes') or self._idle_default):g} idle minutes.")
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

    def _key_ok(self, h: str) -> bool:
        """The key this copy started with, or the ones the control document names: the current key, and the one
        before it until its grace period ends."""
        import hmac

        keys = self._control.get("keys") or {}
        if not keys.get("current"):
            return hmac.compare_digest(h, self.key_sha256)
        # The key was changed since this copy started: the key it started with no longer counts by itself.
        if hmac.compare_digest(h, str(keys["current"])):
            return True
        prev, until = keys.get("previous"), keys.get("previous_until")
        return bool(prev) and bool(until) and hmac.compare_digest(h, str(prev)) and _now() < str(until)

    def authorized(self, key: str | None) -> bool:
        if not self.key_sha256:
            return True
        if not key:
            return False
        h = hashlib.sha256(str(key).encode()).hexdigest()
        if self._key_ok(h):
            return True
        # A key this copy doesn't know may be one that was just changed: read the control document again before
        # refusing, at most once a second, so a new key works at once on every copy.
        now = self.clock()
        if now - self._key_reload_at >= 1.0:
            self._key_reload_at = now
            self.control(force=True)
            return self._key_ok(h)
        return False

    def _admit(self, body: dict, ctl: dict) -> None:
        """Refuse a request that is too big, or one too many this second, with a sentence a person can act on."""
        lim = ctl.get("limits") or {}
        max_kb = lim.get("max_request_kb", MAX_REQUEST_KB)
        size = len(json.dumps(body, default=str).encode())
        if max_kb and size > float(max_kb) * 1024:
            raise TooLarge(f"This request is {size / 1024:,.0f} KB. This deployment takes requests up to {float(max_kb):g} KB. "
                           f"Send a smaller one, or raise the limit: cst deployments protect {self.pid} --max-request-kb <n>.")
        rate = lim.get("rate_per_s")
        if rate:
            rate = float(rate)
            with self._lock:
                now = self.clock()
                full = max(1.0, rate)  # a second's worth may arrive at once
                self._tokens = full if self._tokens is None else min(full, self._tokens + (now - self._tokens_at) * rate)
                self._tokens_at = now
                ok = self._tokens >= 1.0
                if ok:
                    self._tokens -= 1.0
            if not ok:
                raise TooMany(f"Too many requests. This deployment takes {rate:g} a second on each copy. Send it again in a second, "
                              f"or raise the limit: cst deployments protect {self.pid} --rate <n>.")

    def _within(self, a, query: str, k: int, budget_ms: float):
        """Rank, but wait no longer than the budget. A call that runs over is left to finish by itself."""
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as _Timeout

        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="staging-rank")
        fut = self._pool.submit(a.rank, query, k)
        try:
            return fut.result(timeout=budget_ms / 1000.0)
        except _Timeout:
            fut.cancel()
            raise TooSlow(f"took longer than the {budget_ms:g} ms budget") from None

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
        self._admit(body, ctl)
        if arm is not None and arm not in self.arms:
            raise ValueError(f"this endpoint has no {arm} arm. Loaded: {', '.join(self.arms)}")
        shadowing = arm is None and bool(ctl.get("shadow")) and {"baseline", "candidate"} <= set(self.arms)
        name = arm or ("baseline" if shadowing else assign(ctl, self.arms, str(session or user or rid)))
        a = self.arms[name]
        t0 = time.perf_counter()
        self.running += 1
        bud = ctl.get("budget") or {}
        budget = float(bud["ms"]) if bud.get("ms") else None
        # The compared model answers in this one's place when this one is slow or fails. Only for the model a site
        # is being served, never for a request that asked for one model by name.
        other = self.arms.get("baseline") if name == "candidate" and arm is None and bud.get("fallback", True) else None
        fell = None
        try:
            try:
                ranked, feats = self._within(a, query, k, budget) if budget and name == "candidate" and arm is None else a.rank(query, k)
            except Exception as e:  # noqa: BLE001
                slow = isinstance(e, TooSlow)
                if other is None:
                    self.errors += 1
                    self.say((f"Ranking {query[:80]!r} with the {name} took longer than the {budget:g} ms budget, and there is no other model to answer in its place."
                              if slow else f"Ranking {query[:80]!r} with the {name} failed: {str(e)[:200]}"), rid, "error")
                    if slow:
                        raise TooSlow(f"This request took longer than the {budget:g} ms this deployment allows, and there is no other "
                                      "model loaded to answer in its place.") from None
                    raise
                ranked, feats = other.rank(query, k)
                fell = {"from": a.pipeline, "why": "slow" if slow else "failed", "budget_ms": budget,
                        **({} if slow else {"error": str(e)[:200]})}
                self.fallbacks += 1
                self.say((f"The {name} took longer than {budget:g} ms on {query[:80]!r}, so the baseline answered." if slow
                          else f"The {name} failed on {query[:80]!r} ({str(e)[:160]}), so the baseline answered."), rid, "warn")
                a = other
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
               "forced": arm is not None, "copy": self.copy}  # which running copy answered, for a host with several
        if shadow:
            rec["shadow"] = shadow
        if fell:
            rec["fallback"] = fell
        with self._lock:
            self._req.append(rec)
            self.requests += 1
            self.by_traffic[traffic] += 1
            if traffic == "real":
                self.last_real_at = rec["ts"]
            self.last_used = self.clock()
        self.say(f"Ranked {query[:80]!r} with the {name} in {ms:g} ms: {len(ids)} results." , rid)
        if shadow:
            self.say(f"The candidate ranked it silently in {shadow['ms']:g} ms." if shadow.get("ms") is not None
                     else f"The candidate failed on it silently: {shadow.get('error')}", rid, "info" if shadow.get("ms") is not None else "error")
        out = {"request_id": rid, "ids": ids, "scores": rec["scores"], "arm": name, "model_version": a.pipeline, "ms": ms}
        if fell:
            out["fallback"] = {"why": fell["why"], "answered_by": a.pipeline}
        if body.get("explain"):
            out["signals"] = {p: dict(zip(a.names, feats[p])) for p in ids if p in feats}
            out["importance"] = a.importance
        return out

    def timing(self, body: dict) -> dict:
        """How long a request took as the site saw it, network included: {request_id, ms}. The only thing a site
        reports back. What shoppers clicked or bought is not collected here."""
        rid = str(body.get("request_id") or "")
        if not rid:
            raise ValueError("a timing needs the request_id that /rank returned")
        seen_ms = body.get("ms") if body.get("ms") is not None else body.get("client_ms")
        if seen_ms is None:
            raise ValueError("a timing needs ms: how long the request took as your site saw it")
        try:
            seen_ms = float(seen_ms)
        except (TypeError, ValueError):
            raise ValueError("the time the site saw is a number of milliseconds") from None
        if not 0 <= seen_ms < 600_000:
            raise ValueError("the time the site saw is a number of milliseconds")
        with self._lock:
            self._timings.append({"request_id": rid, "ms": round(seen_ms, 3), "ts": _now()})
            self.last_used = self.clock()
        return {"ok": True, "request_id": rid}

    def flush(self, publish: bool = False) -> dict:
        with self._lock:
            req, lines, timings = self._req, self._lines, self._timings
            self._req, self._lines, self._timings = [], [], []
            self.last_flush = self.clock()
        base = L.prefix(self.pid, self.env, self.stage)
        cid = new_id()
        try:
            if req:
                meta = {"_meta": {"names": {n: a.names for n, a in self.arms.items() if a.names}}}
                self.store.put_new(f"{base}requests-{cid}.jsonl", "\n".join(json.dumps(x) for x in [meta, *req]).encode())
            if timings:
                self.store.put_new(f"{base}timings-{cid}.jsonl", "\n".join(json.dumps(x) for x in timings).encode())
            if lines:
                self.store.put_new(f"{base}lines-{cid}.jsonl", "\n".join(json.dumps(x) for x in lines).encode())
        except AlreadyExists:
            pass  # a retry after a half-written flush: what was written stays, the rest went in the first try
        except Exception:
            with self._lock:  # nothing is lost to a failed write: the records go back in the buffer
                self._req = req + self._req
                self._lines, self._timings = lines + self._lines, timings + self._timings
            raise
        if req:
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
        return {"requests": len(req), "published": ref, **({"publish_note": note} if note else {})}

    def status(self) -> dict:
        now = self.clock()
        ctl = self._control
        idle = now - self.last_used
        return {"ok": self.stopped is None, "project": self.pid, "env": self.env, "stage": self.stage,
                "arms": {n: a.pipeline for n, a in self.arms.items()}, "serve": ctl.get("serve"),
                "started_at": self.started_at, "uptime_s": round(now - self.started),
                "requests": self.requests, "running": max(0, self.running), "traffic": dict(self.by_traffic),
                "errors": self.errors, "refused": self.refused, "fallbacks": self.fallbacks, "copy": self.copy, "ready_s": self.load_s, "memory_mb": _memory_mb(),
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
        due = now - self.last_flush >= self.flush_s or len(self._req) >= 500 or len(self._lines) >= 2000
        publish = self._unpublished and now - self.last_publish >= self.publish_s
        if (due and (self._req or self._lines or self._timings)) or publish:
            try:
                self.flush(publish=now - self.last_publish >= self.publish_s)
            except Exception as e:  # noqa: BLE001
                print(f"staging: could not write the log: {e}", flush=True)
        self.control()
        # On a host that sleeps when idle, the host takes the copies away. Stopping here would leave one answering 503.
        if self.bundle.get("scale_to_zero"):
            return
        self._beat(now)
        if now - self.last_used >= self.idle_minutes * 60:
            # Stopping deletes the whole endpoint, and with more than one copy the host hands a request to whichever
            # it likes: this copy can sit unused while another is busy. So ask the others before stopping.
            other = self._others_last_used()
            if other is not None and other > self.last_used:
                self.last_used = other
                return
            self.stop(f"no requests for {self.idle_minutes:g} minutes")

    def _beat(self, now: float) -> None:
        """Say when this copy was last used, where the other copies of this deployment can read it. Written when it
        has changed, at most once a flush interval. A failure is not worth a line: the next tick tries again."""
        if self._beat_used == self.last_used or now - self._beat_at < self.flush_s:
            return
        key = copies_prefix(self.pid, self.env, self.stage) + f"{self.copy}.json"
        try:
            self._beat_tag = self.store.put_if(key, json.dumps({"copy": self.copy, "last_used": self.last_used, "at": _now()}).encode(), self._beat_tag)
            self._beat_at, self._beat_used = now, self.last_used
        except Exception:  # noqa: BLE001
            self._beat_at = now

    def _others_last_used(self) -> float | None:
        """The latest time any other copy of this deployment said it was used, or None when there is no other copy
        or it can't be read. Asked only when this copy is about to stop."""
        latest = None
        try:
            mine = f"{self.copy}.json"
            for key in self.store.list(copies_prefix(self.pid, self.env, self.stage)):
                if key.endswith("/" + mine):
                    continue
                try:
                    used = float(json.loads(self.store.get(key)).get("last_used") or 0)
                except Exception:  # noqa: BLE001
                    continue
                latest = used if latest is None else max(latest, used)
        except Exception:  # noqa: BLE001
            return None
        return latest

    def stop(self, reason: str) -> None:
        if self.stopped:
            return
        self.say(f"Stopping: {reason}.")
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

    api = FastAPI(title=f"Captain deployed model: {rt.pid}")

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
            rt.refused += 1
            rt.say(f"Refused a request: {str(e)[:200]}", level="warn")
            raise HTTPException(getattr(e, "status", 400), str(e), headers=getattr(e, "headers", None)) from None
        except TooSlow as e:
            raise HTTPException(504, str(e)) from None

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

    @api.post("/timing")
    def timing(body: dict, authorization: str = Header(default="")):
        check(authorization)
        return run(rt.timing, body)

    @api.post("/outcome")
    def outcome(body: dict, authorization: str = Header(default="")):
        check(authorization)
        if (body or {}).get("event") == "timing":  # the code sites pasted before: still how they report their time
            return run(rt.timing, body)
        raise HTTPException(410, OUTCOMES_GONE)

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
        if op == "timing" or (op == "outcome" and body.get("event") == "timing"):
            return run(rt.timing, body)
        if op == "outcome":
            raise HTTPException(410, OUTCOMES_GONE)
        if op == "flush":
            return run(rt.flush, bool(body.get("publish")))
        if op == "status":
            return rt.status()
        if op == "reload":
            return rt.control(force=True)
        raise HTTPException(400, f"unknown op {op!r}: rank, timing, flush, status or reload")

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
