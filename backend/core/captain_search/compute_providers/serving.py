"""What every place that hosts a staged model does, in one shape.

A compute provider that can host a staging endpoint implements these on top of the job methods. staging/ calls
them through compute.py and never branches on the cloud:

  stage_plan(st)            the calls `stage` would make, for a dry run. No network.
  stage(st, progress)       create (or move) the endpoint and wait until it answers. Returns
                            {endpoint: {name?, url?, region?}, resources: [{type, name, ...}]}. On failure it removes
                            what it made, and what it could not remove rides on the error as `left_behind`.
  staged_status(rec)        {state: running | starting | failed | gone, reason?, copies?, ask?}. `copies` is how
                            many copies are up right now, for a host that sleeps when idle (None when the host
                            doesn't say). `ask` is True when asking the endpoint itself costs next to nothing.
  invoke(rec, payload)      one call to the endpoint: {"op": rank | outcome | flush | status | reload, "key", ...}.
  unstage(resources)        delete these. Returns the ones that are still there.
  tagged(project, env)      every resource here that carries Captain's labels for a project's staging.
  stage_preflight(st, uri)  what the endpoint's identity may do, asked without changing anything.
  staged_logs(rec, since)   the host's own log lines for the endpoint (startup failures land here, before the
                            runtime can write its own log). {lines: [{ts, message}], next, note?}
  scale(rec)                {min_copies, max_copies, idle_seconds, can_set: [...], text}
  set_scale(rec, ...)       change them. Returns scale(rec).
  address(rec)              {url, auth, how}: where a site sends requests and what it must send with them.

`st` is {project, env, id, bundle, key_sha256, container_env, image?, idle_minutes, existing}.

Storage. The endpoint reads its bundle and writes its request log in one place, and Captain reads and writes the
same place. Each host says which: SageMaker, Cloud Run and Container Apps use a bucket (the workspace's own, or a
serving bucket the setup makes) that the endpoint's own identity may read and write under `staging/` and nowhere
else. Modal has no identities to grant a bucket to, so it uses a Modal volume the setup makes.
"""
from __future__ import annotations

import time

from . import ProviderError


class ServingProvider:
    """Mix into a ComputeProvider that hosts staging endpoints. Defaults are the honest "this host can't"."""

    # True when the host stops every copy by itself while nobody calls, and starts one on the next request. Such an
    # endpoint costs nothing asleep, so Captain never removes it for being idle and never wakes it to ask how it is.
    scale_to_zero = False
    # True when the host keeps the bundle and the log in storage of its own (a Modal volume, a serving bucket the
    # setup made), whatever the workspace is kept on. Then only the bundle and the log ever go there.
    serving_storage_own = False
    # The last date a model was staged here on a real account, start to finish, or None.
    serving_live_tested: str | None = None

    def stage_preflight(self, st: dict, storage_uri: str) -> dict:
        return {"checked": False, "can_delete": None, "can_write": None, "warnings": [], "note": "Not checked on this kind of target."}

    def staged_logs(self, rec: dict, since: str | None = None) -> dict:
        return {"lines": [], "next": since, "note": "This host's own log can't be read from here. The endpoint's request log is under Logs."}

    def scale(self, rec: dict) -> dict:
        return {"min_copies": None, "max_copies": None, "idle_seconds": None, "can_set": [], "text": "Copies can't be changed on this host from here."}

    def set_scale(self, rec: dict, **_kw) -> dict:
        raise ValueError(self.scale(rec)["text"])

    def address(self, rec: dict) -> dict:
        ep = rec.get("endpoint") or {}
        return {"url": ep.get("url"), "auth": None, "how": None}

    @classmethod
    def describe(cls) -> dict:
        d = super().describe()  # type: ignore[misc]
        d["serving"] = {"scale_to_zero": cls.scale_to_zero, "live_tested": cls.serving_live_tested}
        return d


ROUTES = {"outcome": "/outcome", "flush": "/flush", "reload": "/reload"}
_clients: dict = {}


def _client(base: str):
    """One connection kept open per endpoint address. Opening a fresh one for every call costs three round trips
    to the cloud's front door before the request is even sent (measured on Modal: 272 ms a call, against under 100)."""
    import httpx

    c = _clients.get(base)
    if c is None or c.is_closed:
        if len(_clients) > 16:
            _clients.pop(next(iter(_clients))).close()
        c = _clients[base] = httpx.Client(timeout=HttpServing.INVOKE_TIMEOUT_S)
    return c


class HttpServing(ServingProvider):
    """A host whose endpoint has an address of its own (Modal, Cloud Run, Container Apps): the runtime's own routes,
    with the endpoint key as a bearer token. The cloud's front door may want a header of its own as well."""

    scale_to_zero = True
    # A copy that was asleep has to start, load the catalog and build its index before it answers.
    INVOKE_TIMEOUT_S = 180.0
    READY_WAIT_S = 600
    READY_POLL_S = 2.0

    def _front_door(self, rec: dict) -> dict:
        """Headers the cloud's own front door needs, beside the endpoint key. None by default."""
        return {}

    def stage_preflight(self, st: dict, storage_uri: str) -> dict:
        # Setup made the endpoint's identity and its storage together, with the one grant between them, and the
        # host stops idle copies itself. There is no role of the team's own to ask about.
        return {"checked": True, "can_delete": None, "can_write": True, "warnings": [], "note": None}

    def invoke(self, rec: dict, payload: dict) -> dict:
        import httpx

        ep = rec.get("endpoint") or {}
        base = str(ep.get("url") or "").rstrip("/")
        if not base:
            raise ProviderError("this endpoint has no address recorded. Stage it again.")
        body = dict(payload)
        op, key, arm = body.pop("op", "rank"), body.pop("key", None), body.pop("arm", None)
        front = self._front_door(rec)
        try:
            if op == "status":
                r = _client(base).get(base + "/healthz", headers=front, timeout=self.INVOKE_TIMEOUT_S)
            else:
                path = "/rank" + (f"/{arm}" if arm else "") if op == "rank" else ROUTES.get(op)
                if path is None:
                    raise ProviderError(f"unknown op {op!r}: rank, outcome, flush, status or reload")
                r = _client(base).post(base + path, json=body, headers={**front, "Authorization": f"Bearer {key}"}, timeout=self.INVOKE_TIMEOUT_S)
        except httpx.HTTPError as e:
            raise ProviderError(f"The staging endpoint did not answer: {type(e).__name__} {str(e)[:200]}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            raise ProviderError(f"The staging endpoint refused the request ({r.status_code}): {detail}")
        return r.json()

    def wait_ready(self, url: str, stage_id: str, rec: dict | None = None, deadline_s: float | None = None, failed=None) -> float:
        """Wait until the endpoint answers /healthz with this stage loaded. Returns the seconds it took. `failed`
        is asked between tries: it returns a sentence when the host says the deploy is lost."""
        import httpx

        t0 = time.time()
        last = "no answer yet"
        front = self._front_door(rec or {"endpoint": {"url": url}})
        while time.time() - t0 < (deadline_s or self.READY_WAIT_S):
            try:
                r = httpx.get(url.rstrip("/") + "/healthz", headers=front, timeout=60.0)
                if r.status_code == 200 and r.json().get("stage") == stage_id:
                    return time.time() - t0
                last = f"HTTP {r.status_code}" + (f", serving {r.json().get('stage')}" if r.status_code == 200 else f": {r.text[:160]}")
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__} {str(e)[:120]}"
            why = failed() if failed else None
            if why:
                raise ProviderError(why)
            time.sleep(self.READY_POLL_S)
        raise ProviderError(f"the endpoint did not answer within {int((deadline_s or self.READY_WAIT_S) // 60)} minutes ({last})")

    def address(self, rec: dict) -> dict:
        ep = rec.get("endpoint") or {}
        return {"url": ep.get("url"), "auth": "Authorization: Bearer <the endpoint key>",
                "how": "POST <url>/rank with the endpoint key. The address is public, and every route but /healthz and /ping needs the key."}


def clean_name(text: str, limit: int = 63, lower: bool = True) -> str:
    """A name most clouds take: letters, digits and dashes, starting with a letter, no dash at either end."""
    import re

    s = re.sub(r"[^A-Za-z0-9-]", "-", text.lower() if lower else text)
    s = re.sub(r"-+", "-", s).strip("-")[:limit].strip("-")
    return s if s and s[0].isalpha() else ("c-" + s)[:limit].strip("-")
