"""Cloud Run service: host a staged model on Google Cloud, with no copy up while nobody calls.

One deployment is one Cloud Run service named `<prefix>-<project>-<env>`, labelled app=captain, running the serving
image from the project's Artifact Registry as the serving service account the setup made. That account can read
and write the serving bucket and nothing else. Unstage deletes the service.

The address is the service's own https URL. Captain makes it public (the `allUsers` invoker binding on that one
service) so a site can call it, and every route but /healthz and /ping still needs the endpoint key. Where an
organization policy forbids public services, the service stays private: Captain calls it with the login's own
identity, and says that a site would need Google credentials too.

Everything goes through Google's REST APIs with the login's credentials, so no client library beyond google-auth
is needed for serving.
"""
from __future__ import annotations

import time

from . import ProviderError
from .serving import HttpServing, clean_name

RUN = "https://run.googleapis.com/v2"
LABELS = {"app": "captain", "captain-purpose": "serving"}


class GoogleError(ProviderError):
    def __init__(self, status: int, message: str, reason: str = ""):
        super().__init__(message)
        self.status, self.reason = status, reason


def denied(e: Exception) -> bool:
    return isinstance(e, GoogleError) and e.status == 403


def api_off(e: Exception) -> str | None:
    """The API's name when Google refused because it is switched off in the project."""
    import re

    m = re.search(r"([a-z0-9.-]+\.googleapis\.com)", str(e))
    if isinstance(e, GoogleError) and e.status == 403 and ("has not been used" in str(e) or "is disabled" in str(e) or e.reason == "SERVICE_DISABLED"):
        return m.group(1) if m else "an API"
    return None


class Rest:
    """Google's REST APIs with one login. `call` returns the JSON, or raises GoogleError in Google's words."""

    def __init__(self, creds, project: str):
        self.creds, self.project = creds, project
        self._s = None

    def session(self):
        if self._s is None:
            from google.auth.transport.requests import AuthorizedSession  # type: ignore

            self._s = AuthorizedSession(self.creds)
        return self._s

    def call(self, method: str, url: str, body: dict | None = None, params: dict | None = None, data: bytes | None = None,
             headers: dict | None = None, ok_missing: bool = False):
        kw: dict = {"params": params, "timeout": 120}
        if data is not None:
            kw.update(data=data, headers=headers or {})
        elif body is not None:
            kw["json"] = body
        r = self.session().request(method, url, **kw)
        if r.status_code == 404 and ok_missing:
            return None
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
            except Exception:  # noqa: BLE001
                err = {}
            reason = next((d.get("reason", "") for d in err.get("details") or [] if d.get("reason")), "")
            raise GoogleError(r.status_code, str(err.get("message") or r.text[:300]), reason)
        return r.json() if r.content else {}

    def wait(self, op: dict, base: str, deadline_s: float = 900, poll_s: float = 3.0) -> dict:
        """Wait for a long-running operation. `base` is the API root its name hangs off."""
        t0 = time.time()
        while not op.get("done"):
            if time.time() - t0 > deadline_s:
                raise ProviderError(f"Google was still working on {op.get('name')} after {int(deadline_s // 60)} minutes")
            time.sleep(poll_s)
            op = self.call("GET", f"{base}/{op['name']}")
        if op.get("error"):
            raise GoogleError(int(op["error"].get("code") or 500), str(op["error"].get("message") or op["error"]))
        return op.get("response") or {}


class CloudRunServing(HttpServing):
    """Mixed into CloudRunProvider (gcp.py): the staging half."""

    scale_to_zero = True
    serving_storage_own = True  # the serving bucket, whatever the workspace is kept on
    serving_live_tested: str | None = None
    POLL_S = 3.0

    def _rest(self) -> Rest:
        creds, project = self._creds()
        return Rest(creds, self.config.get("project") or project)

    def _names(self, st: dict) -> dict:
        prefix = (self.config.get("name_prefix") or "captain").strip("-")
        return {"service": clean_name(f"{prefix}-{st['project']}-{st['env']}", 49)}

    def _parent(self, rest: Rest) -> str:
        return f"projects/{rest.project}/locations/{self.region}"

    def _size(self) -> dict:
        c = self.config

        def num(k, d):
            try:
                return float(c.get(k) or d)
            except (TypeError, ValueError):
                return float(d)

        return {"cpu": num("cpu", 1), "memory_mb": int(num("memory_mb", 2048)), "min": int(num("min_copies", 0)), "max": max(1, int(num("max_copies", 1)))}

    def _labels(self, st: dict) -> dict:
        return {**LABELS, "project": clean_name(st["project"], 60), "env": clean_name(st["env"], 60), "captain-stage": clean_name(st["id"], 60)}

    def _body(self, st: dict) -> dict:
        size = self._size()
        env = dict(st.get("container_env") or {})
        return {"labels": self._labels(st), "ingress": "INGRESS_TRAFFIC_ALL",
                "template": {"serviceAccount": self.config.get("service_account") or None, "labels": self._labels(st),
                             "scaling": {"minInstanceCount": size["min"], "maxInstanceCount": size["max"]},
                             "maxInstanceRequestConcurrency": 32, "timeout": "120s",
                             "containers": [{"image": st.get("image") or self.config["image"], "args": ["serve"],
                                             "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                                             "ports": [{"containerPort": 8080}],
                                             "resources": {"limits": {"cpu": f"{size['cpu']:g}", "memory": f"{size['memory_mb']}Mi"},
                                                           "cpuIdle": True, "startupCpuBoost": True},
                                             # the catalog and its index load before the port opens
                                             "startupProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 3, "failureThreshold": 100, "timeoutSeconds": 2}}]}}

    def stage_plan(self, st: dict) -> list[dict]:
        n = self._names(st)
        update = any(r["type"] == "cloud-run-service" and r["name"] == n["service"] for r in st.get("existing") or [])
        name = f"projects/{{project}}/locations/{self.region}/services/{n['service']}"
        return [{"service": "run", "region": self.region, "call": "services.patch" if update else "services.create",
                 "args": {"name": name, "service": self._body(st)}},
                {"service": "run", "region": self.region, "call": "services.setIamPolicy",
                 "args": {"resource": name, "policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}}}]

    def _get(self, rest: Rest, service: str) -> dict | None:
        return rest.call("GET", f"{RUN}/{self._parent(rest)}/services/{service}", ok_missing=True)

    def _failed(self, rest: Rest, service: str):
        def ask() -> str | None:
            s = self._get(rest, service) or {}
            t = s.get("terminalCondition") or {}
            if t.get("state") == "CONDITION_FAILED":
                return f"Cloud Run could not start the service: {t.get('message') or t.get('reason')}"
            return None
        return ask

    def stage(self, st: dict, progress=None) -> dict:
        say = progress or (lambda _t: None)
        if st.get("image"):
            raise ProviderError("A container image of your own can't be staged on Cloud Run from Captain yet. Use an endpoint you already run.")
        rest = self._rest()
        n = self._names(st)
        res = {"type": "cloud-run-service", "name": n["service"], "region": self.region, "project": rest.project}
        made: list[dict] = []
        public = True
        try:
            cur = self._get(rest, n["service"])
            if cur is not None and (cur.get("labels") or {}).get("app") != "captain":
                raise ProviderError(f"A Cloud Run service named {n['service']} already exists here and Captain did not make it. "
                                    "Give this target another name prefix.")
            base = f"{RUN}/{self._parent(rest)}/services"
            if cur is None:
                say("Creating the Cloud Run service")
                op = rest.call("POST", base, self._body(st), params={"serviceId": n["service"]})
                made.append(res)
            else:
                say("Moving the Cloud Run service to the new version")
                op = rest.call("PATCH", f"{base}/{n['service']}", self._body(st))
            svc = rest.wait(op, RUN, deadline_s=900, poll_s=self.POLL_S)
            url = svc.get("uri") or (self._get(rest, n["service"]) or {}).get("uri")
            if not url:
                raise ProviderError("Cloud Run created the service but gave it no address")
            say("Opening the address to your site")
            try:
                rest.call("POST", f"{base}/{n['service']}:setIamPolicy", {"policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}})
            except GoogleError as e:
                # An organization that forbids public services: keep it private, and say what that means.
                public = False
                say(f"Your organization doesn't allow a public address ({str(e)[:120]}). The service stays private: "
                    "Captain can call it, and a site would need Google credentials.")
            say("Waiting for the endpoint to answer")
            ep = {"name": n["service"], "url": url, "region": self.region, "project": rest.project, **({} if public else {"private": True})}
            self.wait_ready(url, st["id"], rec={"endpoint": ep}, failed=self._failed(rest, n["service"]))
        except BaseException as e:
            say("Removing what was created, because the endpoint did not start")
            left = self.unstage(made)
            if not made and any(r["type"] == "cloud-run-service" for r in st.get("existing") or []):
                left = left + list(st.get("existing") or [])  # a move that failed: the service is still there
            err = e if isinstance(e, ProviderError) or not isinstance(e, Exception) else ProviderError(f"{type(e).__name__}: {str(e)[:400]}")
            err.left_behind = left
            if err is e:
                raise
            raise err from None
        return {"endpoint": ep, "resources": [res]}

    def _front_door(self, rec: dict) -> dict:
        if not (rec.get("endpoint") or {}).get("private"):
            return {}
        import google.auth.transport.requests  # type: ignore

        creds, _ = self._creds()
        tok = None
        try:
            from google.oauth2 import id_token  # type: ignore

            tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), rec["endpoint"]["url"])
        except Exception:  # noqa: BLE001  a person's own login has no service identity: use the login's own token
            creds.refresh(google.auth.transport.requests.Request())
            tok = getattr(creds, "id_token", None)
        if not tok:
            raise ProviderError("This service is private, and this login can't make an identity token for it. Sign in with a service account.")
        return {"X-Serverless-Authorization": f"Bearer {tok}"}

    def unstage(self, resources: list[dict]) -> list[dict]:
        rest = self._rest()
        left = []
        for r in resources:
            if r["type"] != "cloud-run-service":
                continue
            url = f"{RUN}/projects/{r.get('project') or rest.project}/locations/{r.get('region') or self.region}/services/{r['name']}"
            try:
                cur = rest.call("GET", url, ok_missing=True)
                if cur is None:
                    continue
                if (cur.get("labels") or {}).get("app") != "captain":
                    left.append({**r, "error": "it does not carry Captain's label, so Captain left it alone"})
                    continue
                rest.wait(rest.call("DELETE", url), RUN, deadline_s=300, poll_s=self.POLL_S)
                if rest.call("GET", url, ok_missing=True) is not None:
                    left.append({**r, "error": "Cloud Run is still deleting it"})
            except Exception as e:  # noqa: BLE001
                left.append({**r, "error": str(e)[:300]})
        return left

    def staged_status(self, rec: dict) -> dict:
        rest = self._rest()
        s = self._get(rest, (rec.get("endpoint") or {}).get("name"))
        if s is None:
            return {"state": "gone", "reason": "the Cloud Run service no longer exists"}
        t = s.get("terminalCondition") or {}
        if t.get("state") == "CONDITION_FAILED":
            return {"state": "failed", "reason": t.get("message") or t.get("reason") or "the service failed"}
        if s.get("reconciling") or t.get("state") not in (None, "CONDITION_SUCCEEDED"):
            return {"state": "starting", "reason": t.get("reason") or "Cloud Run is rolling it out"}
        # Cloud Run doesn't say how many copies are up. Asking the endpoint is safe here: it bills only while it
        # answers, so a status check costs a few milliseconds, not a copy kept awake.
        return {"state": "running", "copies": None, "ask": True}

    def scale(self, rec: dict) -> dict:
        s = dict(self._size())
        s.update({k: v for k, v in ((rec.get("scale") or {}).items()) if k in ("min", "max") and v is not None})
        return {"min_copies": s["min"], "max_copies": s["max"], "idle_seconds": None, "can_set": ["min_copies", "max_copies"],
                "text": ("No copy is up while nobody calls. Cloud Run keeps an idle one for up to 15 minutes and decides when. " if s["min"] == 0
                         else f"Keeps {s['min']} up at all times. ") + f"At most {s['max']} at once."}

    def set_scale(self, rec: dict, min_copies: int | None = None, max_copies: int | None = None, idle_seconds: int | None = None) -> dict:
        if idle_seconds is not None:
            raise ValueError("Cloud Run decides how long an idle copy stays up (up to 15 minutes). It can't be set.")
        cur = dict(rec.get("scale") or {})
        cur.update({k: int(v) for k, v in (("min", min_copies), ("max", max_copies)) if v is not None})
        s = {**self._size(), **cur}
        if s["min"] > s["max"]:
            raise ValueError("The fewest copies can't be more than the most.")
        rest = self._rest()
        name = (rec.get("endpoint") or {}).get("name")
        svc = self._get(rest, name)
        if svc is None:
            raise ProviderError("the Cloud Run service no longer exists")
        tmpl = {k: v for k, v in svc["template"].items() if k != "revision"}
        tmpl["scaling"] = {"minInstanceCount": s["min"], "maxInstanceCount": s["max"]}
        rest.wait(rest.call("PATCH", f"{RUN}/{self._parent(rest)}/services/{name}", {"labels": svc.get("labels"), "ingress": svc.get("ingress"), "template": tmpl}),
                  RUN, deadline_s=600, poll_s=self.POLL_S)
        return {**self.scale({**rec, "scale": cur}), "scale": cur}

    def staged_logs(self, rec: dict, since: str | None = None) -> dict:
        name = (rec.get("endpoint") or {}).get("name")
        return self._logs(f'resource.type="cloud_run_revision" AND resource.labels.service_name="{name}"', since)

    def address(self, rec: dict) -> dict:
        a = super().address(rec)
        if (rec.get("endpoint") or {}).get("private"):
            a.update(auth="X-Serverless-Authorization: Bearer <a Google identity token>, and Authorization: Bearer <the endpoint key>",
                     how="The service is private: your organization doesn't allow a public address. A caller needs the Cloud Run "
                         "Invoker role on it, and sends its Google identity token beside the endpoint key.")
        return a

    def tagged(self, project: str, env: str) -> list[dict]:
        rest = self._rest()
        out, token = [], None
        while True:
            r = rest.call("GET", f"{RUN}/{self._parent(rest)}/services", params={"pageSize": 100, **({"pageToken": token} if token else {})})
            for s in r.get("services") or []:
                lb = s.get("labels") or {}
                if lb.get("app") == "captain" and lb.get("project") == clean_name(project, 60) and lb.get("env") == clean_name(env, 60):
                    out.append({"type": "cloud-run-service", "name": s["name"].rsplit("/", 1)[-1], "region": self.region, "project": rest.project})
            token = r.get("nextPageToken")
            if not token:
                return out
