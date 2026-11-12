"""Azure as a host: a Container Apps app with no copy up while nobody calls.

Setup makes one resource group and puts everything in it, so removing the setup is deleting that group:

  resource group        `<prefix>-serving`
  storage account       with one container, `serving`: the serving bundle and the request log. The workspace
                        itself is never copied there, wherever it is kept.
  managed identity      `<prefix>-serving-id`: the endpoint runs as it. It may read and write that one container and
                        pull the serving image. Nothing else.
  container registry    Basic, the cheapest: holds the serving image.
  the image             built by the registry itself (an ACR task) from the installed Captain. No Docker here.
  environment           a Container Apps environment on the Consumption plan, with no log workspace.
  compute target        `azure-serving` in Captain.

Two grants are made, both inside the group: the identity on the container and on the registry, and the login that
runs setup on the container (in Azure, owning a storage account does not let you read its blobs: that is a grant
of its own, and Captain needs it to write the bundle and read the log).

Making grants needs Owner, or Contributor with Role Based Access Control Administrator. A login that lacks a
permission gets one sentence naming the step, and the roles to ask an admin for.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

from ... import price_table as PT
from ...workspace import Context
from .. import timings as TM
from .base import Host, MissingPermission, now

TARGET = "azure-serving"
TAGS = {"app": "captain", "captain-purpose": "serving"}
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"   # Storage Blob Data Contributor
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"           # AcrPull
PROVIDERS = {"Microsoft.App": "Container Apps runs the endpoint.", "Microsoft.ContainerRegistry": "The registry holds and builds the serving image.",
             "Microsoft.Storage": "Storage holds the serving bundle and the request log.", "Microsoft.ManagedIdentity": "The identity the endpoint runs as."}
BUILD_MINUTES = 3
API = {"group": "2021-04-01", "storage": "2023-01-01", "identity": "2023-01-31", "role": "2022-04-01", "registry": "2023-07-01", "build": "2019-06-01-preview",
       "env": "2024-03-01", "provider": "2021-04-01"}


def _clean(prefix: str, limit: int = 40) -> str:
    from ...compute_providers.serving import clean_name

    return clean_name(prefix or "captain", limit)


def login_policy(sub: str) -> dict:
    return {"subscription": sub[:4] + "...", "roles": [
        {"role": "Contributor", "why": "create the resource group and what is in it, and delete it again"},
        {"role": "Role Based Access Control Administrator", "why": "let the endpoint's identity, and you, into the one storage container and the one registry"}],
        "note": "Owner has both. They are needed on the subscription, because setup makes its own resource group.",
        "commands": [f"az role assignment create --assignee <the login> --role \"{r}\" --scope /subscriptions/<subscription id>"
                     for r in ("Contributor", "Role Based Access Control Administrator")]}


class AzureHost(Host):
    cloud, label, kind, target, login = "azure", "Azure", "azure-container-apps", TARGET, "azure"
    scale_to_zero = True
    proven: str | None = None
    storage = ("A storage container in the resource group setup makes. Only the serving bundle and the request log go "
               "there. The endpoint's own identity may read and write that container and nothing else.")
    address = ("https://<app>.<environment>.<region>.azurecontainerapps.io. Public, with the endpoint key as a bearer "
               "token on every route but /healthz and /ping.")
    options = [{"name": "profile", "label": "Cloud login", "required": True}, {"name": "region", "label": "Region", "default": "eastus"},
               {"name": "name_prefix", "label": "Name prefix", "default": "captain"},
               {"name": "cpu", "label": "vCPU for one copy", "default": "1"}, {"name": "memory_mb", "label": "Memory for one copy, in MB", "default": "2048"},
               {"name": "max_copies", "label": "Most copies at once", "default": "1"},
               {"name": "min_copies", "label": "Copies kept up while idle (0 for none)", "default": "0"}]
    order = {"compute-target": 0, "resource-group": 1}
    POLL_S = 5.0
    GRANT_WAIT_S = 420  # a new grant can take a few minutes to let its holder in

    # ---- names and the plan -------------------------------------------------------------------

    def _opts(self, o: dict) -> dict:
        if not o.get("profile"):
            raise ValueError("Pick the Azure login to set this up with (profile).")
        return {"profile": o["profile"], "region": (o.get("region") or "eastus").strip().lower().replace(" ", ""),
                "name_prefix": _clean(o.get("name_prefix") or "captain"), "cpu": float(o.get("cpu") or 1), "memory_mb": int(float(o.get("memory_mb") or 2048)),
                "max_copies": max(1, int(float(o.get("max_copies") or 1))), "min_copies": max(0, int(float(o.get("min_copies") or 0)))}

    def _arm(self, profile: str):
        from ... import cloud
        from ...compute_providers.azure_serving import Arm

        cred, conn, sub = cloud.azure_credential(profile)
        if cred is None:
            raise ValueError("This Azure login is a storage connection string, which can't set up serving. Use a service principal or `az login`.")
        if not sub:
            raise ValueError("This Azure login names no subscription. Add one: cst cloud add <login> --provider azure --auth default -f subscription_id=<id>")
        return Arm(cred, sub)

    def names(self, ctx: Context, o: dict, sub: str) -> dict:
        p = o["name_prefix"]
        group = f"{p}-serving"
        short = "captserve" + hashlib.sha256(f"{sub}/{group}".encode()).hexdigest()[:12]  # storage and registry names: letters and digits only
        return {"group": group, "storage_account": short, "container": "serving", "identity": f"{p}-serving-id", "registry": short,
                "environment": f"{p}-serving-env", "target": TARGET, "apps": p, "storage": f"az://{short}/serving/{ctx.workspace.name}"}

    def _price(self, region: str) -> dict:
        return PT.CONTAINER_APPS.get(region, PT.CONTAINER_APPS["default"])

    def hourly(self, cpu: float, memory_mb: int, region: str = "eastus") -> float:
        p = self._price(region)
        return 3600 * (cpu * p["vcpu_second"] + memory_mb / 1024 * p["gib_second"])

    def _unregistered(self, arm) -> list[str] | None:
        try:
            return [ns for ns in PROVIDERS
                    if (arm.call("GET", f"/subscriptions/{arm.sub}/providers/{ns}", api=API["provider"]) or {}).get("registrationState") != "Registered"]
        except Exception:  # noqa: BLE001
            return None

    def plan(self, ctx: Context, _arm=None, **options) -> dict:
        o = self._opts(options)
        arm = _arm or self._arm(o["profile"])
        n = self.names(ctx, o, arm.sub)
        off = self._unregistered(arm)
        hourly = self.hourly(o["cpu"], o["memory_mb"], o["region"])
        pr = self._price(o["region"])
        kept = 3600 * o["min_copies"] * (o["cpu"] * pr["idle_vcpu_second"] + o["memory_mb"] / 1024 * pr["idle_gib_second"])
        rows = [{"resource": "Resource provider registered", "name": ns, "why": PROVIDERS[ns]} for ns in off or []]
        rows += [
            {"resource": "Resource group", "name": n["group"], "why": "Holds everything below. Removing the setup deletes this group."},
            {"resource": "Storage account", "name": n["storage_account"], "why": "One container, named serving: the serving bundle (the model and its catalog) and the request log. "
                                                                                 "Your workspace is not copied there."},
            {"resource": "Managed identity", "name": n["identity"], "why": "The endpoint runs as this. It may read and write that one container and pull the serving image. Nothing else."},
            {"resource": "Container registry (Basic)", "name": n["registry"], "why": "Holds Captain's serving image, and builds it. It costs about $5 a month while the setup exists."},
            {"resource": "Container Apps environment", "name": n["environment"], "why": "Where the endpoint runs. Consumption plan: it costs nothing by itself."},
            {"resource": "Captain compute target", "name": n["target"],
             "why": f"Container Apps app, {o['cpu']:g} vCPU and {o['memory_mb'] / 1024:g} GB a copy, at most {o['max_copies']}, in {o['region']}. Staging uses it."}]
        later = [{"resource": "Container app", "name": f"{n['apps']}-{{project}}-staging", "why": "The endpoint. Made when you stage a model, deleted when you unstage it."}]
        build = BUILD_MINUTES * 60 * 2 * PT.AZURE_EXTRAS["build_vcpu_second"]
        return {"cloud": "azure", "profile": o["profile"], "region": o["region"], "subscription": arm.sub, "names": n, "storage": n["storage"], "options": o,
                "unregistered": off, "creates": rows, "each_deploy": later,
                "cost": {"setup_usd": round(build, 2), "hourly_awake_usd": round(hourly, 4), "daily_usd": PT.AZURE_EXTRAS["registry_basic_day"],
                         "text": f"Setting up costs about ${build:.2f} once: the estimate allows {BUILD_MINUTES} minutes for the registry to build the image, not timed on a real account yet. "
                                 f"The registry then costs about ${PT.AZURE_EXTRAS['registry_basic_day']:.2f} a day for as long as the setup exists. "
                                 f"A copy costs about ${hourly:.3f} an hour while it is up and nothing once it has slept"
                                 + (f", plus about ${kept:.3f} an hour for the {o['min_copies']} kept up" if kept else "")
                                 + f". Prices as of {PT.SERVERLESS_PRICED_AS_OF}."},
                "login_policy": login_policy(arm.sub),
                "login_text": "The login that sets this up needs these two roles on the subscription. Owner has both.",
                "text": f"Set up serving in your Azure subscription {arm.sub[:4]}... in {o['region']}: "
                        + ", ".join(f"{r['resource']} {r['name']}" for r in rows) + "."}

    # ---- setup --------------------------------------------------------------------------------

    def _principal(self, arm) -> str | None:
        """The login's own object ID, read from its token. Azure puts it there as `oid`."""
        try:
            body = arm._token().split(".")[1]
            return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("oid")
        except Exception:  # noqa: BLE001
            return None

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **options) -> dict:
        import httpx

        from ... import __version__
        from ... import compute as C
        from ...compute_providers.azure_serving import AzureError, denied
        from ...storage.backend import NotFound, open_store
        from .. import serving_setup as SS
        from .gcp import _tgz

        o = self._opts(options)
        arm = self._arm(o["profile"])
        p = self.plan(ctx, _arm=arm, **options)
        if not confirm:
            raise ValueError("Confirm first. " + p["text"] + " " + p["cost"]["text"])
        n, sub, region = p["names"], arm.sub, o["region"]
        say = progress or (lambda _t: None)
        rec, note = self.begin(ctx, profile=o["profile"], region=region, subscription=sub, names=n, storage=n["storage"])
        group = f"/subscriptions/{sub}/resourceGroups/{n['group']}"

        def step(what: str, action: str, fn):
            try:
                return fn()
            except AzureError as e:
                if denied(e):
                    rec["status"] = "needs permission"
                    self.write(ctx, rec)
                    raise MissingPermission(what, action, p["login_policy"], cloud="Azure") from None
                raise

        for ns in p["unregistered"] or []:
            say(f"Registering {ns} on the subscription")
            step(f"register {ns}", f"{ns}/register/action", lambda ns=ns: arm.call("POST", f"/subscriptions/{sub}/providers/{ns}/register", api=API["provider"]))
            t0 = time.time()
            while (arm.call("GET", f"/subscriptions/{sub}/providers/{ns}", api=API["provider"]) or {}).get("registrationState") != "Registered":
                if time.time() - t0 > 600:
                    raise ValueError(f"Azure was still registering {ns} after 10 minutes. Run setup again in a while.")
                time.sleep(self.POLL_S)
            note("provider-registration", ns)

        say(f"Creating the resource group {n['group']}")
        cur = step(f"look up the resource group {n['group']}", "Microsoft.Resources/subscriptions/resourceGroups/read", lambda: arm.call("GET", group, api=API["group"], ok_missing=True))
        if cur is None:
            step(f"create the resource group {n['group']}", "Microsoft.Resources/subscriptions/resourceGroups/write",
                 lambda: arm.call("PUT", group, {"location": region, "tags": TAGS}, API["group"]))
            note("resource-group", n["group"], region=region)
        elif not any(x["type"] == "resource-group" and x["name"] == n["group"] for x in rec["created"]):
            rec["status"] = "name taken"
            self.write(ctx, rec)
            raise ValueError(f"A resource group named {n['group']} already exists and Captain did not make it. Pick another name prefix.")

        def make(kind: str, what: str, action: str, path: str, body: dict, api: str, deadline_s: float = 900) -> dict:
            say(f"Creating the {what}")
            out = step(f"create the {what}", action, lambda: arm.put_wait(path, body, api, deadline_s=deadline_s, poll_s=self.POLL_S))
            note(kind, path.rsplit("/", 1)[-1], resource_group=n["group"])
            return out

        sa = f"{group}/providers/Microsoft.Storage/storageAccounts/{n['storage_account']}"
        make("storage-account", f"storage account {n['storage_account']}", "Microsoft.Storage/storageAccounts/write", sa,
             {"location": region, "tags": TAGS, "sku": {"name": "Standard_LRS"}, "kind": "StorageV2",
              "properties": {"allowBlobPublicAccess": False, "allowSharedKeyAccess": False, "minimumTlsVersion": "TLS1_2", "supportsHttpsTrafficOnly": True}}, API["storage"])
        container = f"{sa}/blobServices/default/containers/{n['container']}"
        step("create the storage container", "Microsoft.Storage/storageAccounts/blobServices/containers/write",
             lambda: arm.call("PUT", container, {"properties": {"publicAccess": "None"}}, API["storage"]))
        ident = make("managed-identity", f"identity {n['identity']}", "Microsoft.ManagedIdentity/userAssignedIdentities/write",
                     f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{n['identity']}", {"location": region, "tags": TAGS}, API["identity"])
        ident_id, ident_principal, ident_client = ident["id"], ident["properties"]["principalId"], ident["properties"]["clientId"]
        reg = f"{group}/providers/Microsoft.ContainerRegistry/registries/{n['registry']}"
        registry = make("container-registry", f"container registry {n['registry']}", "Microsoft.ContainerRegistry/registries/write", reg,
                        {"location": region, "tags": TAGS, "sku": {"name": "Basic"}, "properties": {"adminUserEnabled": False}}, API["registry"])
        server = registry["properties"]["loginServer"]

        def grant(scope: str, role: str, principal: str, kind: str | None, what: str) -> None:
            rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{role}|{principal}"))
            body = {"properties": {"roleDefinitionId": f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{role}", "principalId": principal,
                                   **({"principalType": kind} if kind else {})}}
            for i in range(10):  # an identity made a moment ago is not always known to the directory yet
                try:
                    arm.call("PUT", f"{scope}/providers/Microsoft.Authorization/roleAssignments/{rid}", body, API["role"])
                    return
                except AzureError as e:
                    if e.code == "RoleAssignmentExists" or e.status == 409:
                        return
                    if e.code == "PrincipalNotFound" and i < 9:
                        time.sleep(self.POLL_S)
                        continue
                    if denied(e):
                        rec["status"] = "needs permission"
                        self.write(ctx, rec)
                        raise MissingPermission(what, "Microsoft.Authorization/roleAssignments/write", p["login_policy"], cloud="Azure") from None
                    raise

        say("Letting the endpoint's identity into the container and the registry, and nowhere else")
        grant(container, BLOB_CONTRIBUTOR, ident_principal, "ServicePrincipal", "let the endpoint's identity into the storage container")
        grant(reg, ACR_PULL, ident_principal, "ServicePrincipal", "let the endpoint's identity pull the image")
        me = self._principal(arm)
        if me:
            say("Letting this login into the container, to write the bundle and read the log")
            grant(container, BLOB_CONTRIBUTOR, me, None, "let this login into the storage container")

        folder, digest = SS.build_folder("azure")
        tag = f"{__version__}-{digest[:10]}"
        image = f"{server}/serving:{tag}"
        t0 = time.time()
        built = rec.get("image") != image
        if not built:
            say("The serving image for this version of Captain is already in your registry")
        else:
            say("Uploading the build folder")
            up = step("upload the build folder", "Microsoft.ContainerRegistry/registries/listBuildSourceUploadUrl/action",
                      lambda: arm.call("POST", f"{reg}/listBuildSourceUploadUrl", api=API["build"]))
            r = httpx.put(up["uploadUrl"], content=_tgz(folder), headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"}, timeout=300)
            if r.status_code >= 300:
                raise ValueError(f"Azure refused the build folder ({r.status_code}): {r.text[:200]}")
            say("Building the serving image in your registry" + TM.aside("azure", "image"))
            run = step("start the image build", "Microsoft.ContainerRegistry/registries/scheduleRun/action", lambda: arm.call("POST", f"{reg}/scheduleRun", {
                "type": "DockerBuildRequest", "imageNames": [f"serving:{tag}"], "isPushEnabled": True, "noCache": False, "dockerFilePath": "Dockerfile",
                "sourceLocation": up["relativePath"], "platform": {"os": "Linux", "architecture": "amd64"}, "timeout": 1800}, API["build"]))
            run_id = (run.get("properties") or {}).get("runId")
            rec["build"] = run_id
            while True:
                b = (step("watch the image build", "Microsoft.ContainerRegistry/registries/runs/read", lambda: arm.call("GET", f"{reg}/runs/{run_id}", api=API["build"])).get("properties") or {})
                if b.get("status") == "Succeeded":
                    break
                if b.get("status") not in ("Queued", "Started", "Running"):
                    rec["status"] = "image build failed"
                    self.write(ctx, rec)
                    raise ValueError(f"The image build did not finish: it ended {b.get('status')}. Its log: az acr task logs -r {n['registry']} --run-id {run_id}")
                if time.time() - t0 > 1800:
                    raise ValueError("The image build was still running after 30 minutes. Look at it in the registry, then run setup again.")
                time.sleep(self.POLL_S)
        rec["image"] = image
        rec["build_seconds"] = round(time.time() - t0) if built else None
        env = make("container-apps-environment", f"Container Apps environment {n['environment']} (not timed on a real account yet)", "Microsoft.App/managedEnvironments/write",
                   f"{group}/providers/Microsoft.App/managedEnvironments/{n['environment']}",
                   {"location": region, "tags": TAGS, "properties": {"zoneRedundant": False, "workloadProfiles": [{"name": "Consumption", "workloadProfileType": "Consumption"}]}},
                   API["env"], deadline_s=1500)
        if me:
            say("Waiting until this login can use the container")
            t1 = time.time()
            while True:
                try:
                    open_store(n["storage"], o["profile"]).list("staging/")
                    break
                except Exception as e:  # noqa: BLE001
                    if time.time() - t1 > self.GRANT_WAIT_S:
                        raise ValueError(f"The grant on the storage container had not taken effect after {self.GRANT_WAIT_S // 60} minutes "
                                         f"({str(e)[:160]}). Run setup again in a while.") from None
                    time.sleep(self.POLL_S * 2)
        cfg = {"resource_group": n["group"], "image": image, "environment_id": env["id"], "identity_id": ident_id, "identity_client_id": ident_client,
               "registry_server": server, "serving_storage": n["storage"], "name_prefix": n["apps"], "cpu": str(o["cpu"]), "memory_mb": str(o["memory_mb"]),
               "min_copies": str(o["min_copies"]), "max_copies": str(o["max_copies"])}
        say("Adding the compute target")
        try:
            C.get(ctx, n["target"])
            C.add(ctx, n["target"], self.kind, o["profile"], region, cfg, replace=True)
        except NotFound:
            C.add(ctx, n["target"], self.kind, o["profile"], region, cfg)
        note("compute-target", n["target"])
        rec.update(status="ready", target=n["target"], ready_at=now())
        self.write(ctx, rec)
        return {**rec, "text": f"Serving is set up in your Azure subscription. Staging can use the target {n['target']}."}

    # ---- removal ------------------------------------------------------------------------------

    def remove_one(self, ctx: Context, rec: dict, item: dict) -> None:
        if item["type"] != "resource-group":
            return  # everything else is inside the group, and goes with it
        arm = self._arm(rec["profile"])
        group = f"/subscriptions/{arm.sub}/resourceGroups/{item['name']}"
        cur = arm.call("GET", group, api=API["group"], ok_missing=True)
        if cur is None:
            return
        if (cur.get("tags") or {}).get("app") != "captain":
            raise ValueError("it does not carry Captain's tag, so Captain left it alone")
        # The group is Captain's own. Anything in it that Captain did not make stops the delete.
        mine = {x["name"] for x in rec.get("created") or []}
        got = arm.call("GET", f"{group}/resources", api=API["group"]) or {}
        other = [r["name"] for r in got.get("value") or [] if r["name"] not in mine and (r.get("tags") or {}).get("app") != "captain"]
        if other:
            raise ValueError(f"it holds {len(other)} thing(s) Captain did not make, such as {other[0]}. Captain doesn't delete what it can't account for.")
        if not arm.delete_wait(group, API["group"], deadline_s=1800, poll_s=self.POLL_S * 2):
            raise ValueError("Azure is still deleting it")

    def leftovers(self, ctx: Context) -> list[dict]:
        """Does the group still exist, and what in the subscription still carries Captain's serving tags?"""
        rec = self.read(ctx)
        if not rec:
            return []
        arm = self._arm(rec["profile"])
        out = []
        g = (rec.get("names") or {}).get("group")
        if g and arm.call("GET", f"/subscriptions/{arm.sub}/resourceGroups/{g}", api=API["group"], ok_missing=True) is not None:
            out.append({"type": "resource-group", "name": g})
        try:
            from urllib.parse import quote

            flt = quote("tagName eq 'captain-purpose' and tagValue eq 'serving'")
            got = arm.call("GET", f"/subscriptions/{arm.sub}/resources?$filter={flt}&api-version={API['group']}") or {}
            out += [{"type": r["type"].rsplit("/", 1)[-1], "name": r["name"]} for r in got.get("value") or []]
        except Exception as e:  # noqa: BLE001
            out.append({"type": "tagged", "name": "?", "error": f"could not list: {str(e)[:160]}"})
        return out
