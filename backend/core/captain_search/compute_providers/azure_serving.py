"""Azure Container Apps app: host a staged model on Azure, with no copy up while nobody calls.

One deployment is one container app named `<prefix>-<project>-<env>` (shortened to Azure's 32 characters), tagged
app=captain, in the resource group and the Container Apps environment the setup made. It runs the serving image
from the group's registry as the user-assigned identity the setup made, which may read and write the serving
container in the group's storage account and pull that one image. Unstage deletes the app.

The address is the app's own https name, public, and every route but /healthz and /ping needs the endpoint key.

Learned from the first Azure job run (docs/compute-live-testing.md) and kept here: the image carries the sign-in
library, the container is told which identity to use (AZURE_CLIENT_ID), and the size is set on the app itself.

Everything goes through Azure's management REST API with the login's credential, so no management SDK is needed.
"""
from __future__ import annotations

import hashlib
import time

from . import ProviderError
from .serving import HttpServing, clean_name

ARM = "https://management.azure.com"
APP_API = "2024-03-01"
TAGS = {"app": "captain", "captain-purpose": "serving"}


class AzureError(ProviderError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def denied(e: Exception) -> bool:
    return isinstance(e, AzureError) and (e.status == 403 or e.code in ("AuthorizationFailed", "LinkedAuthorizationFailed"))


class Arm:
    """Azure's management API with one login. `call` returns the JSON, or raises AzureError in Azure's words."""

    def __init__(self, cred, subscription: str):
        self.cred, self.sub = cred, subscription
        self._tok = None

    def _token(self) -> str:
        if self._tok is None or self._tok[1] - time.time() < 120:
            t = self.cred.get_token("https://management.azure.com/.default")
            self._tok = (t.token, float(t.expires_on))
        return self._tok[0]

    def call(self, method: str, path: str, body: dict | None = None, api: str | None = None, ok_missing: bool = False, raw: bool = False):
        import httpx

        url = path if path.startswith("http") else ARM + path
        r = httpx.request(method, url, params={"api-version": api} if api else None, json=body,
                          headers={"Authorization": f"Bearer {self._token()}"}, timeout=120)
        if r.status_code == 404 and ok_missing:
            return None
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
            except Exception:  # noqa: BLE001
                err = {}
            raise AzureError(r.status_code, str(err.get("message") or r.text[:300]), str(err.get("code") or ""))
        if raw:
            return r
        return r.json() if r.content else {}

    def put_wait(self, path: str, body: dict, api: str, deadline_s: float = 900, poll_s: float = 5.0) -> dict:
        """Create or change a resource and wait until Azure says it is there."""
        self.call("PUT", path, body, api)
        return self.wait(path, api, deadline_s, poll_s)

    def wait(self, path: str, api: str, deadline_s: float = 900, poll_s: float = 5.0) -> dict:
        t0 = time.time()
        while True:
            cur = self.call("GET", path, api=api, ok_missing=True) or {}
            state = (cur.get("properties") or {}).get("provisioningState")
            if state in ("Succeeded", None) and cur:
                return cur
            if state in ("Failed", "Canceled"):
                raise ProviderError(f"Azure could not make {path.rsplit('/', 1)[-1]}: it ended {state}")
            if time.time() - t0 > deadline_s:
                raise ProviderError(f"Azure was still making {path.rsplit('/', 1)[-1]} after {int(deadline_s // 60)} minutes")
            time.sleep(poll_s)

    def delete_wait(self, path: str, api: str, deadline_s: float = 900, poll_s: float = 5.0) -> bool:
        """Delete and wait until it is gone. False when it was still there at the deadline."""
        self.call("DELETE", path, api=api, ok_missing=True)
        t0 = time.time()
        while self.call("GET", path, api=api, ok_missing=True) is not None:
            if time.time() - t0 > deadline_s:
                return False
            time.sleep(poll_s)
        return True


def app_name(prefix: str, project: str, env: str) -> str:
    """A container app's name: at most 32 characters. A long one keeps its start and gains a short hash."""
    full = clean_name(f"{prefix}-{project}-{env}", 200)
    if len(full) <= 32:
        return full
    return clean_name(full[:25], 25) + "-" + hashlib.sha256(full.encode()).hexdigest()[:6]


class ContainerAppsServing(HttpServing):
    """Mixed into ContainerAppsProvider (azure.py): the staging half."""

    scale_to_zero = True
    serving_storage_own = True  # the serving container, whatever the workspace is kept on
    serving_live_tested: str | None = None
    POLL_S = 5.0

    def _arm(self) -> Arm:
        cred, sub = self._cred()
        return Arm(cred, sub)

    def _group(self, arm: Arm) -> str:
        return f"/subscriptions/{arm.sub}/resourceGroups/{self.config['resource_group']}"

    def _app(self, arm: Arm, name: str) -> str:
        return f"{self._group(arm)}/providers/Microsoft.App/containerApps/{name}"

    def _names(self, st: dict) -> dict:
        return {"app": app_name((self.config.get("name_prefix") or "captain").strip("-"), st["project"], st["env"])}

    def _size(self) -> dict:
        c = self.config

        def num(k, d):
            try:
                return float(c.get(k) or d)
            except (TypeError, ValueError):
                return float(d)

        return {"cpu": num("cpu", 1), "memory_mb": int(num("memory_mb", 2048)), "min": int(num("min_copies", 0)), "max": max(1, int(num("max_copies", 1))),
                "idle_s": int(num("idle_seconds", 300))}

    def _tags(self, st: dict) -> dict:
        return {**TAGS, "project": st["project"][:200], "env": st["env"][:200], "captain-stage": st["id"]}

    def _body(self, st: dict, location: str) -> dict:
        c, size = self.config, self._size()
        env = {**(st.get("container_env") or {}), **({"AZURE_CLIENT_ID": c["identity_client_id"]} if c.get("identity_client_id") else {})}
        ident = c.get("identity_id")
        return {"location": location, "tags": self._tags(st),
                **({"identity": {"type": "UserAssigned", "userAssignedIdentities": {ident: {}}}} if ident else {}),
                "properties": {"environmentId": c["environment_id"],
                               "configuration": {"ingress": {"external": True, "targetPort": 8080, "transport": "auto", "allowInsecure": False},
                                                 "registries": [{"server": c["registry_server"], "identity": ident}] if c.get("registry_server") and ident else []},
                               "template": {"containers": [{"name": "runtime", "image": st.get("image") or c["image"], "args": ["serve"],
                                                            "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                                                            # the size is the app's own: one named only at start runs at the smallest
                                                            "resources": {"cpu": size["cpu"], "memory": f"{size['memory_mb'] / 1024:g}Gi"},
                                                            "probes": [{"type": "Startup", "tcpSocket": {"port": 8080}, "periodSeconds": 3, "failureThreshold": 100}]}],
                                            "scale": {"minReplicas": size["min"], "maxReplicas": size["max"],
                                                      "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": "32"}}}]}}}}

    def stage_plan(self, st: dict) -> list[dict]:
        n = self._names(st)
        return [{"service": "containerapps", "call": "containerApps.createOrUpdate", "args": {
            "resource_group": self.config.get("resource_group"), "name": n["app"], "body": self._body(st, self.region or "<the group's region>")}}]

    def _failed(self, arm: Arm, name: str):
        def ask() -> str | None:
            a = arm.call("GET", self._app(arm, name), api=APP_API, ok_missing=True) or {}
            p = a.get("properties") or {}
            if p.get("provisioningState") in ("Failed", "Canceled"):
                return f"Container Apps could not start the app: it ended {p.get('provisioningState')}"
            return None
        return ask

    def stage(self, st: dict, progress=None) -> dict:
        say = progress or (lambda _t: None)
        if st.get("image"):
            raise ProviderError("A container image of your own can't be staged on Container Apps from Captain yet. Use an endpoint you already run.")
        arm = self._arm()
        n = self._names(st)
        res = {"type": "container-app", "name": n["app"], "resource_group": self.config["resource_group"]}
        made: list[dict] = []
        try:
            cur = arm.call("GET", self._app(arm, n["app"]), api=APP_API, ok_missing=True)
            if cur is not None and (cur.get("tags") or {}).get("app") != "captain":
                raise ProviderError(f"A container app named {n['app']} already exists in this group and Captain did not make it. "
                                    "Give this target another name prefix.")
            location = self.region or (arm.call("GET", self._group(arm), api="2021-04-01") or {}).get("location")
            say("Creating the container app" if cur is None else "Moving the container app to the new version")
            arm.call("PUT", self._app(arm, n["app"]), self._body(st, location), APP_API)
            if cur is None:
                made.append(res)
            app = arm.wait(self._app(arm, n["app"]), APP_API, deadline_s=900, poll_s=self.POLL_S)
            fqdn = (((app.get("properties") or {}).get("configuration") or {}).get("ingress") or {}).get("fqdn")
            if not fqdn:
                raise ProviderError("Container Apps created the app but gave it no address")
            url = f"https://{fqdn}"
            say("Waiting for the endpoint to answer")
            self.wait_ready(url, st["id"], failed=self._failed(arm, n["app"]))
        except BaseException as e:
            say("Removing what was created, because the endpoint did not start")
            left = self.unstage(made)
            if not made and any(r["type"] == "container-app" for r in st.get("existing") or []):
                left = left + list(st.get("existing") or [])  # a move that failed: the app is still there
            err = e if isinstance(e, ProviderError) or not isinstance(e, Exception) else ProviderError(f"{type(e).__name__}: {str(e)[:400]}")
            err.left_behind = left
            if err is e:
                raise
            raise err from None
        return {"endpoint": {"name": n["app"], "url": url, "region": location, "resource_group": self.config["resource_group"]}, "resources": [res]}

    def unstage(self, resources: list[dict]) -> list[dict]:
        arm = self._arm()
        left = []
        for r in resources:
            if r["type"] != "container-app":
                continue
            path = f"/subscriptions/{arm.sub}/resourceGroups/{r.get('resource_group') or self.config['resource_group']}/providers/Microsoft.App/containerApps/{r['name']}"
            try:
                cur = arm.call("GET", path, api=APP_API, ok_missing=True)
                if cur is None:
                    continue
                if (cur.get("tags") or {}).get("app") != "captain":
                    left.append({**r, "error": "it does not carry Captain's tag, so Captain left it alone"})
                    continue
                if not arm.delete_wait(path, APP_API, deadline_s=600, poll_s=self.POLL_S):
                    left.append({**r, "error": "Azure is still deleting it"})
            except Exception as e:  # noqa: BLE001
                left.append({**r, "error": str(e)[:300]})
        return left

    def _copies(self, arm: Arm, name: str, app: dict) -> int | None:
        rev = (app.get("properties") or {}).get("latestReadyRevisionName") or (app.get("properties") or {}).get("latestRevisionName")
        if not rev:
            return None
        try:
            got = arm.call("GET", f"{self._app(arm, name)}/revisions/{rev}/replicas", api=APP_API) or {}
            return len(got.get("value") or [])
        except Exception:  # noqa: BLE001
            return None

    def staged_status(self, rec: dict) -> dict:
        arm = self._arm()
        name = (rec.get("endpoint") or {}).get("name")
        app = arm.call("GET", self._app(arm, name), api=APP_API, ok_missing=True)
        if app is None:
            return {"state": "gone", "reason": "the container app no longer exists"}
        state = (app.get("properties") or {}).get("provisioningState")
        if state in ("Failed", "Canceled"):
            return {"state": "failed", "reason": f"the container app ended {state}"}
        if state not in ("Succeeded", None):
            return {"state": "starting", "reason": state}
        return {"state": "running", "copies": self._copies(arm, name, app)}

    def scale(self, rec: dict) -> dict:
        s = dict(self._size())
        s.update({k: v for k, v in ((rec.get("scale") or {}).items()) if k in ("min", "max") and v is not None})
        return {"min_copies": s["min"], "max_copies": s["max"], "idle_seconds": 300, "can_set": ["min_copies", "max_copies"],
                "text": ("No copy is up while nobody calls: an idle one is taken away after about 5 minutes. " if s["min"] == 0
                         else f"Keeps {s['min']} up at all times. ") + f"At most {s['max']} at once."}

    def set_scale(self, rec: dict, min_copies: int | None = None, max_copies: int | None = None, idle_seconds: int | None = None) -> dict:
        if idle_seconds is not None:
            raise ValueError("On Container Apps an idle copy is taken away after about 5 minutes. Captain can't change that yet.")
        cur = dict(rec.get("scale") or {})
        cur.update({k: int(v) for k, v in (("min", min_copies), ("max", max_copies)) if v is not None})
        s = {**self._size(), **cur}
        if s["min"] > s["max"]:
            raise ValueError("The fewest copies can't be more than the most.")
        arm = self._arm()
        name = (rec.get("endpoint") or {}).get("name")
        app = arm.call("GET", self._app(arm, name), api=APP_API, ok_missing=True)
        if app is None:
            raise ProviderError("the container app no longer exists")
        tmpl = dict((app.get("properties") or {}).get("template") or {})
        tmpl["scale"] = {**(tmpl.get("scale") or {}), "minReplicas": s["min"], "maxReplicas": s["max"]}
        arm.call("PATCH", self._app(arm, name), {"properties": {"template": tmpl}}, APP_API)
        arm.wait(self._app(arm, name), APP_API, deadline_s=600, poll_s=self.POLL_S)
        return {**self.scale({**rec, "scale": cur}), "scale": cur}

    def staged_logs(self, rec: dict, since: str | None = None) -> dict:
        name = (rec.get("endpoint") or {}).get("name")
        return {"lines": [], "next": since,
                "note": "Setup makes no log workspace, to keep the group small. The app's own lines, from a terminal: "
                        f"az containerapp logs show -n {name} -g {self.config.get('resource_group')} --follow"}

    def tagged(self, project: str, env: str) -> list[dict]:
        arm = self._arm()
        got = arm.call("GET", f"{self._group(arm)}/providers/Microsoft.App/containerApps", api=APP_API, ok_missing=True) or {}
        out = []
        for a in got.get("value") or []:
            t = a.get("tags") or {}
            if t.get("app") == "captain" and t.get("project") == project and t.get("env") == env:
                out.append({"type": "container-app", "name": a["name"], "resource_group": self.config["resource_group"]})
        return out
