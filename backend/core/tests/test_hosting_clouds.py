"""Hosting on Google Cloud Run and Azure Container Apps, against stand-ins for each cloud's API: what setup makes
and grants, what a deploy makes and removes, what a login without a permission is told, and that a failure cleans
up after itself. The stand-in endpoint runs the real runtime over the real bundle, so a query goes the whole way."""
import re

import pytest
from fastapi.testclient import TestClient

from captain_search import cloud
from captain_search import compute as C
from captain_search import staging as SG
from captain_search.compute_providers import ProviderError, serving as SV
from captain_search.compute_providers import azure_serving as AZ
from captain_search.compute_providers import gcp_serving as GC
from captain_search.compute_providers.azure import ContainerAppsProvider
from captain_search.compute_providers.gcp import CloudRunProvider
from captain_search.staging import hosts as H
from captain_search.staging import runtime as RT
from captain_search.staging.hosts.azure import AzureHost
from captain_search.staging.hosts.gcp import GcpHost
from captain_search.storage import backend as SB
from captain_search.storage.backend import LocalStore
from captain_search.storage.registry import Registry
from captain_search.workspace import Context

from test_staging import keychain, ws  # noqa: F401  (fixtures)

PROJECT = "demo-project"
SUB = "00000000-0000-0000-0000-000000000000"


class Endpoints:
    """The endpoints the stand-in clouds started: each one the real runtime, reached by its address."""

    def __init__(self, ctx, tmp):
        self.ctx, self.tmp, self.clients, self.calls = ctx, tmp, {}, []

    def store(self):
        return LocalStore(str(self.tmp / "serving" / "t"))

    def start(self, url: str, env: dict) -> None:
        store = self.store()
        rt = RT.Runtime(Context(self.ctx.workspace, store, Registry(store)), env["CST_STAGING_BUNDLE"], key_sha256=env["CST_STAGING_KEY_SHA256"])
        self.clients[url] = TestClient(RT.make_app(rt))

    def client(self, base):
        w = self

        class Counting:
            def get(self, url, **kw):
                w.calls.append(url)
                return w.clients[base].get(url, headers=kw.get("headers"))

            def post(self, url, **kw):
                w.calls.append(url)
                return w.clients[base].post(url, json=kw.get("json"), headers=kw.get("headers"))

        return Counting()

    def wire(self, monkeypatch):
        import httpx

        monkeypatch.setattr(SV, "_client", self.client)
        monkeypatch.setattr(httpx, "get", lambda url, **kw: self.client(url.rsplit("/healthz", 1)[0]).get(url, **kw))
        real = SB.open_store
        monkeypatch.setattr(SB, "open_store", lambda uri, profile=None: self.store() if uri.split("://")[0] in ("gs", "az") else real(uri, profile))


# ---- Google Cloud --------------------------------------------------------------------------------------------


class FakeGoogle(GC.Rest):
    def __init__(self, eps: Endpoints):
        super().__init__(None, PROJECT)
        self.eps = eps
        self.apis = {"run.googleapis.com": False, "cloudbuild.googleapis.com": False, "artifactregistry.googleapis.com": True,
                     "iam.googleapis.com": True, "storage.googleapis.com": True}
        self.accounts, self.buckets, self.repos, self.services, self.builds = {}, {}, {}, {}, {}
        self.deny: set[str] = set()     # words in a call that this login may not make
        self.build_status = "SUCCESS"
        self.no_public = False          # an organization that forbids public services
        self.service_fails = False
        self.log: list[tuple[str, str]] = []

    def call(self, method, url, body=None, params=None, data=None, headers=None, ok_missing=False):
        self.log.append((method, url))
        for word in self.deny:
            if word in f"{method} {url}":
                raise GC.GoogleError(403, f"Permission denied on {url.rsplit('/', 1)[-1]}")
        got = self._do(method, url, body or {}, params or {}, data)
        if got is None:
            if ok_missing:
                return None
            raise GC.GoogleError(404, "not found")
        return got

    def _do(self, m, url, body, params, data):
        if "serviceusage" in url:
            api = re.search(r"services/([a-z.]+)", url).group(1)
            if url.endswith(":enable"):
                self.apis[api] = True
                return {"done": True}
            if url.endswith(":disable"):
                self.apis[api] = False
                return {"done": True}
            return {"state": "ENABLED" if self.apis.get(api) else "DISABLED"}
        if "iam.googleapis.com" in url:
            if m == "POST":
                email = f"{body['accountId']}@{PROJECT}.iam.gserviceaccount.com"
                self.accounts[email] = body
                return {"email": email}
            if url.endswith("/serviceAccounts"):
                return {"accounts": [{"email": e} for e in self.accounts]}
            email = url.rsplit("/", 1)[-1]
            if m == "DELETE":
                return {} if self.accounts.pop(email, None) is not None else None
            return {"email": email} if email in self.accounts else None
        if "upload/storage" in url:
            self.buckets[re.search(r"/b/([^/]+)/o", url).group(1)]["objects"][params["name"]] = data
            return {}
        if "storage.googleapis.com" in url:
            mb = re.search(r"/b/([^/?]+)(/iam|/o/?(.*))?$", url)
            if not mb:  # /b : create or list
                if m == "POST":
                    self.buckets[body["name"]] = {"labels": body.get("labels"), "iam": {"bindings": []}, "objects": {}, "body": body}
                    return {}
                return {"items": [{"name": n, "labels": b["labels"]} for n, b in self.buckets.items()]}
            b = self.buckets.get(mb.group(1))
            if b is None:
                return None
            if mb.group(2) == "/iam":
                if m == "PUT":
                    b["iam"] = body
                return b["iam"]
            if mb.group(2) and mb.group(2).startswith("/o"):
                if m == "DELETE":
                    from urllib.parse import unquote

                    b["objects"].pop(unquote(mb.group(3)), None)
                    return {}
                return {"items": [{"name": n} for n in b["objects"]]}
            if m == "DELETE":
                if b["objects"]:
                    raise GC.GoogleError(409, "The bucket you tried to delete is not empty.")
                del self.buckets[mb.group(1)]
                return {}
            return {"name": mb.group(1)}
        if "artifactregistry" in url:
            if m == "POST" and url.endswith("/repositories"):
                self.repos[params["repositoryId"]] = {"labels": body.get("labels"), "iam": {"bindings": []}, "tags": set()}
                return {"done": True}
            if url.endswith("/repositories"):
                return {"repositories": [{"name": f"x/{n}", "labels": r["labels"]} for n, r in self.repos.items()]}
            name = re.search(r"repositories/([^/:]+)", url).group(1)
            r = self.repos.get(name)
            if r is None:
                return None
            if url.endswith(":getIamPolicy"):
                return r["iam"]
            if url.endswith(":setIamPolicy"):
                r["iam"] = body["policy"]
                return r["iam"]
            if "/tags/" in url:
                return {} if url.rsplit("/", 1)[-1] in r["tags"] else None
            if m == "DELETE":
                del self.repos[name]
                return {"done": True}
            return {"name": name}
        if "cloudbuild" in url:
            if m == "POST":
                self.builds["b1"] = body
                if self.build_status == "SUCCESS":
                    repo = re.search(r"/([^/]+)/serving:", body["images"][0]).group(1)
                    self.repos[repo]["tags"].add(body["images"][0].rsplit(":", 1)[-1])
                return {"metadata": {"build": {"id": "b1"}}}
            return {"status": self.build_status, "statusDetail": "step 0 exited with status 1"}
        if "run.googleapis.com" in url:
            if url.endswith(":setIamPolicy"):
                if self.no_public:
                    raise GC.GoogleError(400, "One or more users named in the policy do not belong to a permitted customer.")
                self.services[re.search(r"services/([^/:]+)", url).group(1)]["public"] = True
                return {}
            if url.endswith("/services") and m == "GET":
                return {"services": [{"name": f"x/{n}", "labels": s["labels"]} for n, s in self.services.items()]}
            if m == "POST":
                name = params["serviceId"]
                uri = f"https://{name}-1234.us-central1.run.app"
                self.services[name] = {**body, "uri": uri, "terminalCondition": {"state": "CONDITION_FAILED" if self.service_fails else "CONDITION_SUCCEEDED",
                                                                                 "message": "The container failed to start and listen on the port."}}
                if not self.service_fails:
                    self.eps.start(uri, {e["name"]: e["value"] for e in body["template"]["containers"][0]["env"]})
                return {"done": True, "response": self.services[name]}
            name = re.search(r"services/([^/:]+)", url).group(1)
            s = self.services.get(name)
            if s is None:
                return None
            if m == "PATCH":
                s.update(body)
                env = {e["name"]: e["value"] for e in s["template"]["containers"][0]["env"]}
                self.eps.start(s["uri"], env)
                return {"done": True, "response": s}
            if m == "DELETE":
                del self.services[name]
                return {"done": True}
            return s
        raise AssertionError(f"{m} {url}")


@pytest.fixture
def google(ws, monkeypatch, tmp_path):  # noqa: F811
    eps = Endpoints(ws, tmp_path)
    g = FakeGoogle(eps)
    eps.wire(monkeypatch)
    monkeypatch.setattr(GcpHost, "_rest", lambda self, profile: g)
    monkeypatch.setattr(CloudRunProvider, "_rest", lambda self: g)
    monkeypatch.setattr(GcpHost, "POLL_S", 0)
    monkeypatch.setattr(GcpHost, "SETTLE_S", 0)
    monkeypatch.setattr(CloudRunProvider, "POLL_S", 0)
    monkeypatch.setattr(CloudRunProvider, "READY_POLL_S", 0)
    monkeypatch.setattr(cloud, "get_profile", lambda name: {"name": name, "provider": "gcp", "auth": "adc", "fields": {"project": PROJECT}})
    return g


def _gcp(ws, **kw):  # noqa: F811
    return H.get("gcp").setup(ws, confirm=True, profile="g", region="us-central1", name_prefix="captain-search-test-ab12cd", **kw)


def test_google_plan_lists_apis_to_switch_on_and_what_it_makes(ws, google):  # noqa: F811
    p = H.get("gcp").plan(ws, profile="g", name_prefix="captain-search-test-ab12cd")
    kinds = [r["resource"] for r in p["creates"]]
    assert kinds[:2] == ["API switched on", "API switched on"] and {r["name"] for r in p["creates"][:2]} == {"run.googleapis.com", "cloudbuild.googleapis.com"}
    assert kinds[2:] == ["Service account", "Cloud Storage bucket", "Artifact Registry repository", "Service account", "Cloud Build build", "Captain compute target"]
    assert all(len(p["names"][k]) <= 30 for k in ("account", "build_account"))  # Google's limit, with the whole prefix kept
    assert p["names"]["account"] == "captain-search-test-ab12cd-run" and p["storage"] == f"gs://captain-search-test-ab12cd-serving-{PROJECT}/t"
    assert "nothing between requests" in p["cost"]["text"]
    roles = [r["role"] for r in p["login_policy"]["roles"]]
    assert "roles/run.admin" in roles and "roles/serviceusage.serviceUsageAdmin" in roles and p["login_policy"]["commands"][0].startswith("gcloud projects add-iam-policy-binding")
    assert not google.accounts and not google.buckets and google.apis["run.googleapis.com"] is False


def test_google_setup_grants_only_on_the_bucket_and_the_repository_and_teardown_removes_it_all(ws, google):  # noqa: F811
    r = _gcp(ws)
    assert r["status"] == "ready" and google.apis["run.googleapis.com"] and google.apis["cloudbuild.googleapis.com"]
    run, bld = r["names"]["account_email"], r["names"]["build_email"]
    assert set(google.accounts) == {run, bld}
    bucket = google.buckets[r["names"]["bucket"]]
    assert bucket["labels"]["app"] == "captain" and bucket["body"]["iamConfiguration"]["publicAccessPrevention"] == "enforced"
    grants = {(b["role"], m) for b in bucket["iam"]["bindings"] for m in b["members"]}
    assert grants == {(role, f"serviceAccount:{e}") for role in ("roles/storage.objectViewer", "roles/storage.objectCreator") for e in (run, bld)}
    repo = google.repos[r["names"]["repo"]]
    assert repo["iam"]["bindings"] == [{"role": "roles/artifactregistry.writer", "members": [f"serviceAccount:{bld}"]}]
    assert not any("cloudresourcemanager" in u or ":setIamPolicy" in u and "/projects/demo-project:" in u for _m, u in google.log)  # the project's own permissions: never
    build = google.builds["b1"]
    assert build["serviceAccount"].endswith(bld) and build["options"] == {"logging": "GCS_ONLY"} and build["images"] == [r["image"]]
    t = C.get(ws, "gcp-serving")
    assert t["kind"] == "gcp-cloud-run" and t["config"]["service_account"] == run and t["config"]["serving_storage"].startswith("gs://") and C.can_stage(ws, t)
    assert [x["type"] for x in H.get("gcp").leftovers(ws)] == ["artifact-repository", "gcs-bucket", "service-account", "service-account"]
    out = H.get("gcp").teardown(ws)
    assert out["status"] == "removed" and not google.accounts and not google.buckets and not google.repos
    assert "left them on" in out["text"] and google.apis["run.googleapis.com"]  # APIs stay on unless asked
    assert H.get("gcp").leftovers(ws) == []


def test_google_teardown_switches_the_apis_off_when_asked(ws, google):  # noqa: F811
    _gcp(ws)
    H.get("gcp").teardown(ws, apis="off")
    assert google.apis["run.googleapis.com"] is False and google.apis["cloudbuild.googleapis.com"] is False and google.apis["iam.googleapis.com"]


def test_google_login_without_a_permission_is_told_the_step_and_the_roles(ws, google):  # noqa: F811
    google.deny = {"POST https://storage.googleapis.com/storage/v1/b"}
    with pytest.raises(H.MissingPermission) as e:
        _gcp(ws)
    assert "can't create the bucket" in str(e.value) and "storage.buckets.create" in str(e.value)
    assert "roles/storage.admin" in [r["role"] for r in e.value.policy["roles"]]
    st = H.get("gcp").status(ws)
    assert st["status"] == "needs permission" and {x["type"] for x in st["created"]} == {"api", "service-account"}  # what it made stays listed
    google.deny = set()
    assert H.get("gcp").teardown(ws)["status"] == "removed" and not google.accounts


def test_google_image_build_that_fails_says_where_its_log_is(ws, google):  # noqa: F811
    google.build_status = "FAILURE"
    with pytest.raises(ValueError, match="The image build did not finish: step 0 exited with status 1. Its log is in gs://"):
        _gcp(ws)
    assert H.get("gcp").status(ws)["status"] == "image build failed"


def test_google_stage_query_and_unstage_leave_nothing_behind(ws, google):  # noqa: F811
    _gcp(ws)
    r = SG.stage(ws, "shop", target="gcp-serving", confirm=True)
    name = "captain-search-test-ab12cd-shop"
    assert r["status"] == "running" and r["scale_to_zero"] and r["endpoint"]["url"].endswith(".run.app") and not r["endpoint"].get("private")
    svc = google.services[name]
    assert svc["public"] and svc["labels"]["app"] == "captain" and svc["labels"]["project"] == "shop"
    c = svc["template"]["containers"][0]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert c["args"] == ["serve"] and svc["template"]["scaling"] == {"minInstanceCount": 0, "maxInstanceCount": 1} and c["resources"]["cpuIdle"]
    assert svc["template"]["serviceAccount"].startswith("captain-search-test-ab12cd-run@")
    assert env["CST_STORAGE"].startswith("gs://") and SG._key(SG.state(ws, "shop")) not in str(svc)
    assert SG.query(ws, "shop", "jacket")["staged"]["results"]
    SG.forget_asked()
    SG.ask_status(ws, SG.state(ws, "shop"))  # what a health check or "Check now" asks: a read a page repeats never calls the model
    s = SG.status(ws, "shop")  # Cloud Run doesn't say how many copies are up: the model's own numbers are what it was last asked
    assert s["health"]["requests"] == 2 and s["health"]["stops_in_s"] is None
    assert C.tagged(ws, C.get(ws, "gcp-serving"), "shop", "staging") == [r["resources"][0]]
    assert SG.unstage(ws, "shop")["status"] == "stopped" and not google.services
    assert any("staging-logs" in k for k in ws.store.list("datasets/"))


def test_google_service_that_fails_to_start_is_removed(ws, google):  # noqa: F811
    _gcp(ws)
    google.service_fails = True
    with pytest.raises(ProviderError, match="Cloud Run could not start the service: The container failed to start"):
        SG.stage(ws, "shop", target="gcp-serving", confirm=True)
    assert not google.services and SG.state(ws, "shop")["resources"] == []


def test_google_organization_that_forbids_public_services_keeps_it_private(ws, google, monkeypatch):  # noqa: F811
    _gcp(ws)
    google.no_public = True
    monkeypatch.setattr(CloudRunProvider, "_front_door", lambda self, rec: {"X-Serverless-Authorization": "Bearer id-token"} if rec["endpoint"].get("private") else {})
    said = []
    r = SG.stage(ws, "shop", target="gcp-serving", confirm=True, progress=said.append)
    assert r["endpoint"]["private"] and any("doesn't allow a public address" in t for t in said)
    a = C.staged_address(ws, C.get(ws, "gcp-serving"), SG.state(ws, "shop"))
    assert "X-Serverless-Authorization" in a["auth"] and "private" in a["how"]


def test_google_never_touches_a_service_it_did_not_make(ws, google):  # noqa: F811
    _gcp(ws)
    name = "captain-search-test-ab12cd-shop"
    google.services[name] = {"labels": {"team": "someone-else"}, "uri": "https://theirs.run.app"}
    with pytest.raises(ProviderError, match="already exists here and Captain did not make it"):
        SG.stage(ws, "shop", target="gcp-serving", confirm=True)
    left = C.unstage(ws, C.get(ws, "gcp-serving"), [{"type": "cloud-run-service", "name": name}])
    assert "does not carry Captain's label" in left[0]["error"] and name in google.services


def test_google_copies_can_be_changed_and_the_idle_time_cannot(ws, google):  # noqa: F811
    _gcp(ws)
    SG.stage(ws, "shop", target="gcp-serving", confirm=True)
    t, rec = C.get(ws, "gcp-serving"), SG.state(ws, "shop")
    out = C.set_staged_scale(ws, t, rec, min_copies=1, max_copies=2)
    assert google.services["captain-search-test-ab12cd-shop"]["template"]["scaling"] == {"minInstanceCount": 1, "maxInstanceCount": 2}
    assert "Keeps 1 up" in out["text"]
    with pytest.raises(ValueError, match="Cloud Run decides how long an idle copy stays up"):
        C.set_staged_scale(ws, t, rec, idle_seconds=60)


# ---- Azure ---------------------------------------------------------------------------------------------------


class FakeAzure(AZ.Arm):
    def __init__(self, eps: Endpoints):
        self.cred, self.sub, self._tok = None, SUB, None
        self.eps = eps
        self.res: dict[str, dict] = {}   # path -> resource
        self.grants: list[dict] = []
        self.deny: set[str] = set()
        self.build_status = "Succeeded"
        self.app_fails = False
        self.copies = 1
        self.log: list[tuple[str, str]] = []

    def _token(self):
        return "x.eyJvaWQiOiAibWUtb2JqZWN0LWlkIn0.y"  # {"oid": "me-object-id"}

    def call(self, method, path, body=None, api=None, ok_missing=False, raw=False):
        self.log.append((method, path))
        for word in self.deny:
            if word in f"{method} {path}":
                raise AZ.AzureError(403, "The client does not have authorization to perform this action.", "AuthorizationFailed")
        got = self._do(method, path.split("?")[0], body or {})
        if got is None:
            if ok_missing:
                return None
            raise AZ.AzureError(404, "not found", "ResourceNotFound")
        return got

    def _do(self, m, path, body):
        if "/providers/Microsoft." in path and path.count("/") == 4:
            return {"registrationState": "Registered"}
        if "/roleAssignments/" in path:
            self.grants.append({"scope": path.split("/providers/Microsoft.Authorization")[0], **body["properties"]})
            return {}
        if path.endswith("/listBuildSourceUploadUrl"):
            return {"uploadUrl": "https://upload.invalid/src", "relativePath": "source/src.tar.gz"}
        if path.endswith("/scheduleRun"):
            self.res[path.rsplit("/", 1)[0] + "/runs/r1"] = {"properties": {"status": self.build_status, "request": body}}
            return {"properties": {"runId": "r1"}}
        if path.endswith("/resources"):
            g = path.rsplit("/resources", 1)[0]
            if "resourceGroups" not in g:
                return {"value": [{"name": p.rsplit("/", 1)[-1], "type": "x/" + p.rsplit("/", 2)[-2], "tags": r.get("tags")} for p, r in self.res.items()
                                  if (r.get("tags") or {}).get("captain-purpose") == "serving" and "/providers/" in p]}
            return {"value": [{"name": p.rsplit("/", 1)[-1], "tags": r.get("tags")} for p, r in self.res.items() if p.startswith(g + "/providers/") and p.count("/") == 8]}
        if path.endswith("/replicas"):
            return {"value": [{}] * self.copies}
        if path.endswith("/containerApps"):
            return {"value": [{"name": p.rsplit("/", 1)[-1], "tags": r.get("tags")} for p, r in self.res.items() if "/containerApps/" in p]}
        if m == "PUT":
            r = {**body, "id": path, "name": path.rsplit("/", 1)[-1], "properties": {**(body.get("properties") or {}), "provisioningState": "Succeeded"}}
            if "/userAssignedIdentities/" in path:
                r["properties"].update(principalId="identity-object-id", clientId="identity-client-id")
            if "/registries/" in path:
                r["properties"]["loginServer"] = f"{r['name']}.azurecr.io"
            if "/containerApps/" in path:
                fqdn = f"{r['name']}.happy-123.eastus.azurecontainerapps.io"
                r["properties"]["configuration"]["ingress"]["fqdn"] = fqdn
                r["properties"]["latestRevisionName"] = r["name"] + "--1"
                if self.app_fails:
                    r["properties"]["provisioningState"] = "Failed"
                else:
                    self.eps.start(f"https://{fqdn}", {e["name"]: e["value"] for e in body["properties"]["template"]["containers"][0]["env"]})
            self.res[path] = r
            return r
        if m == "PATCH":
            r = self.res.get(path)
            if r is None:
                return None
            r["properties"]["template"] = body["properties"]["template"]
            return r
        if m == "DELETE":
            if path not in self.res:
                return None
            for p in [p for p in self.res if p == path or p.startswith(path + "/")]:
                del self.res[p]
            return {}
        return self.res.get(path)


@pytest.fixture
def azure(ws, monkeypatch, tmp_path):  # noqa: F811
    import httpx

    eps = Endpoints(ws, tmp_path)
    a = FakeAzure(eps)
    eps.wire(monkeypatch)
    monkeypatch.setattr(AzureHost, "_arm", lambda self, profile: a)
    monkeypatch.setattr(ContainerAppsProvider, "_arm", lambda self: a)
    for cls in (AzureHost, ContainerAppsProvider):
        monkeypatch.setattr(cls, "POLL_S", 0)
    monkeypatch.setattr(ContainerAppsProvider, "READY_POLL_S", 0)
    a.uploads = []
    monkeypatch.setattr(httpx, "put", lambda url, **kw: (a.uploads.append((url, len(kw["content"]), kw["headers"])) or type("R", (), {"status_code": 201, "text": ""})()))
    monkeypatch.setattr(cloud, "get_profile", lambda name: {"name": name, "provider": "azure", "auth": "default", "fields": {"subscription_id": SUB}})
    return a


def _az(ws, **kw):  # noqa: F811
    return H.get("azure").setup(ws, confirm=True, profile="a", region="eastus", name_prefix="captain-search-test-ab12cd", **kw)


GROUP = f"/subscriptions/{SUB}/resourceGroups/captain-search-test-ab12cd-serving"


def test_azure_plan_puts_everything_in_one_group_and_says_what_the_registry_costs(ws, azure):  # noqa: F811
    p = H.get("azure").plan(ws, profile="a", name_prefix="captain-search-test-ab12cd")
    assert [r["resource"] for r in p["creates"]] == ["Resource group", "Storage account", "Managed identity", "Container registry (Basic)",
                                                    "Container Apps environment", "Captain compute target"]
    n = p["names"]
    assert n["group"] == "captain-search-test-ab12cd-serving" and re.fullmatch(r"[a-z0-9]{5,24}", n["storage_account"]) and n["storage"] == f"az://{n['storage_account']}/serving/t"
    assert "a day for as long as the setup exists" in p["cost"]["text"] and "nothing once it has slept" in p["cost"]["text"]
    assert [r["role"] for r in p["login_policy"]["roles"]] == ["Contributor", "Role Based Access Control Administrator"]
    assert SUB not in str(p["login_policy"]) and SUB not in p["text"]  # the subscription is never spelled out
    assert not azure.res


def test_azure_setup_grants_inside_the_group_only_and_teardown_deletes_the_group(ws, azure):  # noqa: F811
    r = _az(ws)
    assert r["status"] == "ready" and azure.res[GROUP]["tags"]["app"] == "captain"
    sa = azure.res[f"{GROUP}/providers/Microsoft.Storage/storageAccounts/{r['names']['storage_account']}"]
    assert sa["properties"]["allowBlobPublicAccess"] is False and sa["properties"]["allowSharedKeyAccess"] is False
    assert azure.res[f"{GROUP}/providers/Microsoft.ContainerRegistry/registries/{r['names']['registry']}"]["sku"] == {"name": "Basic"}
    who = {(g["principalId"], g["roleDefinitionId"].rsplit("/", 1)[-1], g["scope"].rsplit("/", 1)[-1]) for g in azure.grants}
    assert who == {("identity-object-id", "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "serving"), ("identity-object-id", "7f951dda-4ed3-4680-a7ca-43fe172d538d", r["names"]["registry"]),
                   ("me-object-id", "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "serving")}
    assert all(g["scope"].startswith(GROUP + "/") for g in azure.grants)  # never on the subscription or the group itself
    run = azure.res[[p for p in azure.res if p.endswith("/runs/r1")][0]]["properties"]["request"]
    assert run["type"] == "DockerBuildRequest" and run["isPushEnabled"] and run["platform"] == {"os": "Linux", "architecture": "amd64"}
    assert azure.uploads and azure.uploads[0][2]["x-ms-blob-type"] == "BlockBlob"
    t = C.get(ws, "azure-serving")
    assert t["config"]["identity_client_id"] == "identity-client-id" and t["config"]["serving_storage"].startswith("az://") and C.can_stage(ws, t)
    assert {"type": "resource-group", "name": "captain-search-test-ab12cd-serving"} in H.get("azure").leftovers(ws)
    out = H.get("azure").teardown(ws)
    assert out["status"] == "removed" and not azure.res and H.get("azure").leftovers(ws) == []


def test_azure_login_that_cannot_make_grants_is_told_the_step_and_the_roles(ws, azure):  # noqa: F811
    azure.deny = {"/roleAssignments/"}
    with pytest.raises(H.MissingPermission) as e:
        _az(ws)
    assert "let the endpoint's identity into the storage container" in str(e.value) and "Microsoft.Authorization/roleAssignments/write" in str(e.value)
    assert "Role Based Access Control Administrator" in str(e.value.policy)
    assert H.get("azure").status(ws)["status"] == "needs permission"
    azure.deny = set()
    assert H.get("azure").teardown(ws)["status"] == "removed" and not azure.res  # what it had made is removed


def test_azure_teardown_leaves_a_group_holding_something_it_did_not_make(ws, azure):  # noqa: F811
    _az(ws)
    azure.res[f"{GROUP}/providers/Microsoft.Sql/servers/theirs"] = {"tags": {}}
    out = H.get("azure").teardown(ws)
    assert out["status"] == "partly removed" and "holds 1 thing(s) Captain did not make, such as theirs" in out["text"] and GROUP in azure.res


def test_azure_image_build_that_fails_says_how_to_read_its_log(ws, azure):  # noqa: F811
    azure.build_status = "Failed"
    with pytest.raises(ValueError, match="The image build did not finish: it ended Failed. Its log: az acr task logs"):
        _az(ws)


def test_azure_stage_query_sleep_and_unstage_leave_nothing_behind(ws, azure):  # noqa: F811
    _az(ws)
    r = SG.stage(ws, "shop", target="azure-serving", confirm=True)
    name = r["endpoint"]["name"]
    assert len(name) <= 32 and name.startswith("captain-search-test-ab12c") and r["scale_to_zero"]  # Azure's limit: shortened, with a hash
    app = azure.res[f"{GROUP}/providers/Microsoft.App/containerApps/{name}"]
    c = app["properties"]["template"]["containers"][0]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert app["tags"]["project"] == "shop" and c["args"] == ["serve"] and c["resources"] == {"cpu": 1.0, "memory": "2Gi"}
    assert env["AZURE_CLIENT_ID"] == "identity-client-id" and env["CST_STORAGE"].startswith("az://")  # which identity to sign in as
    assert app["properties"]["template"]["scale"]["minReplicas"] == 0 and list(app["identity"]["userAssignedIdentities"]) == [C.get(ws, "azure-serving")["config"]["identity_id"]]
    assert app["properties"]["configuration"]["registries"][0]["identity"] == C.get(ws, "azure-serving")["config"]["identity_id"]
    assert SG._key(SG.state(ws, "shop")) not in str(app)
    assert SG.query(ws, "shop", "jacket")["staged"]["results"]
    azure.copies, azure.eps.calls = 0, []
    s = SG.status(ws, "shop")
    assert s["asleep"] and "Asleep" in s["text"] and azure.eps.calls == []  # not woken to be asked
    azure.copies = 1
    SG.forget_asked()
    SG.ask_status(ws, SG.state(ws, "shop"))  # what a health check or "Check now" asks: a read a page repeats never calls the model
    assert SG.status(ws, "shop")["health"]["requests"] == 2
    assert C.tagged(ws, C.get(ws, "azure-serving"), "shop", "staging") == [r["resources"][0]]
    assert SG.unstage(ws, "shop")["status"] == "stopped" and not [p for p in azure.res if "/containerApps/" in p]


def test_azure_app_that_fails_to_start_is_removed(ws, azure):  # noqa: F811
    _az(ws)
    azure.app_fails = True
    with pytest.raises(ProviderError, match="ended Failed"):
        SG.stage(ws, "shop", target="azure-serving", confirm=True)
    assert not [p for p in azure.res if "/containerApps/" in p] and SG.state(ws, "shop")["resources"] == []


def test_azure_never_touches_an_app_it_did_not_make(ws, azure):  # noqa: F811
    _az(ws)
    name = AZ.app_name("captain-search-test-ab12cd", "shop", "staging")
    path = f"{GROUP}/providers/Microsoft.App/containerApps/{name}"
    azure.res[path] = {"tags": {"team": "someone-else"}, "properties": {"provisioningState": "Succeeded"}}
    with pytest.raises(ProviderError, match="already exists in this group and Captain did not make it"):
        SG.stage(ws, "shop", target="azure-serving", confirm=True)
    left = C.unstage(ws, C.get(ws, "azure-serving"), [{"type": "container-app", "name": name}])
    assert "does not carry Captain's tag" in left[0]["error"] and path in azure.res


def test_unproven_hosts_say_so_and_get_no_cost_number(ws, google, azure):  # noqa: F811
    from captain_search import cost_estimates as CE

    cards = {c["cloud"]: c for c in H.listing(ws)}
    for name, host in (("gcp", GcpHost), ("azure", AzureHost)):
        if host.proven:
            assert host.proven in cards[name]["proven_text"]
            continue
        assert cards[name]["proven_text"] == "Not run on a real account yet. Tested against a stand-in only."
    _gcp(ws)
    e = CE.estimate(ws, "staging", "gcp-serving")
    assert e["available"] == bool(CloudRunProvider.serving_live_tested)
