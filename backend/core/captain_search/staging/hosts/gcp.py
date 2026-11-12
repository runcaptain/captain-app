"""Google Cloud as a host: a Cloud Run service with no copy up while nobody calls.

Setup makes, in the person's own project, after showing each one and a confirm:

  service account   `<prefix>-run`: the endpoint runs as it. It may read and add objects in the serving bucket,
                    and nothing else anywhere.
  bucket            `<prefix>-serving-<project>`: the serving bundle and the request log, and the build folder. The
                    workspace itself is never copied there, wherever it is kept.
  repository        `<prefix>-serving` in Artifact Registry: holds the serving image.
  build account     `<prefix>-bld`: the image build runs as it. It may read the build folder, write the
                    build log beside it, and push to that one repository.
  the image         built by Cloud Build inside the project from the installed Captain. No Docker on this computer.
  compute target    `gcp-serving` in Captain.

No project-wide permission is granted to anything: every grant is on the one bucket or the one repository. APIs the
project has switched off (Cloud Run, Cloud Build, Artifact Registry) are listed in the plan and switched on by
setup. Removing the setup leaves them on unless asked (`apis=off`), because something else may use them by then.
Google itself grants its own service agents a project role when an API is first switched on (Cloud Run's gets
`roles/run.serviceAgent`), and leaves that behind when the API is switched off again. Captain says so and does
not touch the project's own permissions.

A login that lacks a permission gets one sentence naming the step, and the roles to ask an admin for.
"""
from __future__ import annotations

import io
import tarfile
import time
import zipfile

from ... import price_table as PT
from ...workspace import Context
from .base import Host, MissingPermission, now

TARGET = "gcp-serving"
LABELS = {"app": "captain", "captain-purpose": "serving"}
APIS = {"run.googleapis.com": "Cloud Run runs the endpoint.", "artifactregistry.googleapis.com": "Artifact Registry holds the serving image.",
        "cloudbuild.googleapis.com": "Cloud Build builds the serving image inside your project.", "iam.googleapis.com": "To make the two service accounts.",
        "storage.googleapis.com": "Cloud Storage holds the serving bundle and the request log."}
ROLES = [("roles/run.admin", "create, change and delete the Cloud Run service"),
         ("roles/iam.serviceAccountAdmin", "create and delete the two service accounts"),
         ("roles/iam.serviceAccountUser", "run the service and the build as those accounts"),
         ("roles/artifactregistry.admin", "create and delete the image repository"),
         ("roles/storage.admin", "create, fill and delete the serving bucket"),
         ("roles/cloudbuild.builds.editor", "start the image build"),
         ("roles/serviceusage.serviceUsageAdmin", "switch on the APIs that are off (only if any are)"),
         ("roles/logging.viewer", "read the service's own log")]
BUILD_MINUTES = 3
IAM, GCS, AR, CB, SU = ("https://iam.googleapis.com/v1", "https://storage.googleapis.com/storage/v1", "https://artifactregistry.googleapis.com/v1",
                        "https://cloudbuild.googleapis.com/v1", "https://serviceusage.googleapis.com/v1")


# A service account's name is at most 30 characters, and the two here end in -run and -bld.
def _clean(prefix: str, limit: int = 26) -> str:
    from ...compute_providers.serving import clean_name

    return clean_name(prefix or "captain", limit)


def login_policy(project: str, apis_off: list[str]) -> dict:
    roles = [r for r in ROLES if r[0] != "roles/serviceusage.serviceUsageAdmin" or apis_off]
    return {"project": project, "roles": [{"role": r, "why": w} for r, w in roles],
            "commands": [f"gcloud projects add-iam-policy-binding {project} --member=user:<the login> --role={r}" for r, _ in roles]}


class GcpHost(Host):
    cloud, label, kind, target, login = "gcp", "Google Cloud", "gcp-cloud-run", TARGET, "gcp"
    scale_to_zero = True
    proven: str | None = None
    storage = ("A serving bucket setup makes. Only the serving bundle and the request log go there. The endpoint's own "
               "service account may read and add objects in that bucket and nothing else.")
    address = ("https://<service>-<project number>.<region>.run.app. Public, with the endpoint key as a bearer token on "
               "every route but /healthz and /ping. Private where your organization forbids public services.")
    options = [{"name": "profile", "label": "Cloud login", "required": True}, {"name": "region", "label": "Region", "default": "us-central1"},
               {"name": "name_prefix", "label": "Name prefix", "default": "captain"},
               {"name": "cpu", "label": "vCPU for one copy", "default": "1"}, {"name": "memory_mb", "label": "Memory for one copy, in MB", "default": "2048"},
               {"name": "max_copies", "label": "Most copies at once", "default": "1"},
               {"name": "min_copies", "label": "Copies kept up while idle (0 for none)", "default": "0"}]
    order = {"compute-target": 0, "artifact-repository": 2, "gcs-bucket": 3, "service-account": 4, "api": 9}
    POLL_S = 5.0
    SETTLE_S = 8.0  # a new service account is not usable in a grant for a few seconds

    # ---- names and the plan -------------------------------------------------------------------

    def _opts(self, o: dict) -> dict:
        if not o.get("profile"):
            raise ValueError("Pick the Google Cloud login to set this up with (profile).")
        return {"profile": o["profile"], "region": (o.get("region") or "us-central1").strip(), "name_prefix": _clean(o.get("name_prefix") or "captain"),
                "cpu": float(o.get("cpu") or 1), "memory_mb": int(float(o.get("memory_mb") or 2048)),
                "max_copies": max(1, int(float(o.get("max_copies") or 1))), "min_copies": max(0, int(float(o.get("min_copies") or 0)))}

    def _rest(self, profile: str):
        from ... import cloud
        from ...compute_providers.gcp_serving import Rest

        creds, project = cloud.gcp_credentials(profile)
        if not project:
            raise ValueError("This Google Cloud login names no project. Add one: cst cloud add <login> --provider gcp --auth adc -f project=<project id>")
        return Rest(creds, project)

    def names(self, ctx: Context, o: dict, project: str) -> dict:
        p = o["name_prefix"]
        bucket = f"{p}-serving-{project}"[:63].strip("-")
        return {"account": f"{p}-run", "build_account": f"{p}-bld", "bucket": bucket, "repo": f"{p}-serving", "target": TARGET, "services": p,
                "account_email": f"{p}-run@{project}.iam.gserviceaccount.com", "build_email": f"{p}-bld@{project}.iam.gserviceaccount.com",
                "storage": f"gs://{bucket}/{ctx.workspace.name}"}

    def hourly(self, cpu: float, memory_mb: int) -> float:
        return 3600 * (cpu * PT.CLOUD_RUN["vcpu_second"] + memory_mb / 1024 * PT.CLOUD_RUN["gib_second"])

    def _apis_off(self, rest) -> list[str] | None:
        off = []
        try:
            for api in APIS:
                s = rest.call("GET", f"{SU}/projects/{rest.project}/services/{api}")
                if s.get("state") != "ENABLED":
                    off.append(api)
        except Exception:  # noqa: BLE001  a login that may not ask: setup finds out as it goes
            return None
        return off

    def plan(self, ctx: Context, _rest=None, **options) -> dict:
        o = self._opts(options)
        rest = _rest or self._rest(o["profile"])
        n = self.names(ctx, o, rest.project)
        off = self._apis_off(rest)
        hourly = self.hourly(o["cpu"], o["memory_mb"])
        kept = 3600 * o["min_copies"] * (o["cpu"] * PT.CLOUD_RUN["idle_vcpu_second"] + o["memory_mb"] / 1024 * PT.CLOUD_RUN["idle_gib_second"])
        rows = [{"resource": "API switched on", "name": a, "why": APIS[a]} for a in off or []]
        rows += [
            {"resource": "Service account", "name": n["account"], "why": "The endpoint runs as this. It may read and add objects in the serving bucket. Nothing else."},
            {"resource": "Cloud Storage bucket", "name": n["bucket"], "why": "Holds the serving bundle (the model and its catalog), the request log and the build folder. "
                                                                              "Your workspace is not copied there."},
            {"resource": "Artifact Registry repository", "name": n["repo"], "why": "Holds Captain's serving image in your project."},
            {"resource": "Service account", "name": n["build_account"], "why": "The image build runs as this. It may read the build folder, write its log beside it, "
                                                                                "and push to that one repository."},
            {"resource": "Cloud Build build", "name": f"{n['repo']} image", "why": f"Builds the serving image inside your project. It runs once, about {BUILD_MINUTES} minutes."},
            {"resource": "Captain compute target", "name": n["target"],
             "why": f"Cloud Run service, {o['cpu']:g} vCPU and {o['memory_mb'] / 1024:g} GB a copy, at most {o['max_copies']}, in {o['region']}. Staging uses it."}]
        later = [{"resource": "Cloud Run service", "name": f"{n['services']}-{{project}}-staging", "why": "The endpoint. Made when you stage a model, deleted when you unstage it."}]
        build = BUILD_MINUTES * PT.CLOUD_RUN["build_minute"]
        return {"cloud": "gcp", "profile": o["profile"], "region": o["region"], "project": rest.project, "names": n, "storage": n["storage"], "options": o,
                "apis_off": off, "creates": rows, "each_deploy": later,
                "cost": {"setup_usd": round(build, 2), "hourly_awake_usd": round(hourly, 4),
                         "text": f"Setting up costs about ${build:.2f} once: the image build runs about {BUILD_MINUTES} minutes in Cloud Build "
                                 "(free inside Google's monthly allowance). The image is under 1 GB, about $0.10 a month to keep. "
                                 f"A copy costs about ${hourly:.3f} an hour while it is answering and nothing between requests"
                                 + (f", plus about ${kept:.3f} an hour for the {o['min_copies']} kept up" if kept else "")
                                 + f". Prices as of {PT.SERVERLESS_PRICED_AS_OF}."},
                "login_policy": login_policy(rest.project, off or []),
                "login_text": "Grant these roles on the project to the login that sets this up. Owner or Editor has them already.",
                "text": f"Set up serving in your Google Cloud project {rest.project[:4]}... in {o['region']}: "
                        + ", ".join(f"{r['resource']} {r['name']}" for r in rows) + "."}

    # ---- setup --------------------------------------------------------------------------------

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **options) -> dict:
        from ... import __version__
        from ... import compute as C
        from ...compute_providers.gcp_serving import GoogleError, api_off, denied
        from ...storage.backend import NotFound
        from .. import serving_setup as SS

        o = self._opts(options)
        rest = self._rest(o["profile"])
        p = self.plan(ctx, _rest=rest, **options)
        if not confirm:
            raise ValueError("Confirm first. " + p["text"] + " " + p["cost"]["text"])
        n, project, region = p["names"], rest.project, o["region"]
        say = progress or (lambda _t: None)
        rec, note = self.begin(ctx, profile=o["profile"], region=region, project=project, names=n, storage=n["storage"])

        def step(what: str, action: str, fn):
            try:
                return fn()
            except GoogleError as e:
                off = api_off(e)
                if off:
                    rec["status"] = "needs an API switched on"
                    self.write(ctx, rec)
                    raise ValueError(f"Google Cloud can't {what}: the API {off} is switched off in this project. Switch it on "
                                     f"(gcloud services enable {off} --project {project}), then run setup again.") from None
                if denied(e):
                    rec["status"] = "needs permission"
                    self.write(ctx, rec)
                    raise MissingPermission(what, action, p["login_policy"], cloud="Google Cloud") from None
                raise

        def exists(url: str) -> bool:
            return rest.call("GET", url, ok_missing=True) is not None

        for api in p["apis_off"] or []:
            say(f"Switching on {api}")
            step(f"switch on {api}", "serviceusage.services.enable",
                 lambda api=api: rest.wait(rest.call("POST", f"{SU}/projects/{project}/services/{api}:enable", {}), SU, deadline_s=300, poll_s=self.POLL_S))
            note("api", api, project=project)

        def account(name: str, email: str, why: str) -> None:
            say(f"Creating the service account {name}")
            if not step(f"look up the service account {name}", "iam.serviceAccounts.get", lambda: exists(f"{IAM}/projects/{project}/serviceAccounts/{email}")):
                step(f"create the service account {name}", "iam.serviceAccounts.create", lambda: rest.call(
                    "POST", f"{IAM}/projects/{project}/serviceAccounts", {"accountId": name, "serviceAccount": {"displayName": f"Captain serving: {why}"[:100]}}))
                note("service-account", email, project=project)

        account(n["account"], n["account_email"], "what a staging endpoint may do")
        account(n["build_account"], n["build_email"], "what the image build may do")

        say(f"Creating the bucket {n['bucket']} for the serving bundle and the request log")
        if not step(f"look up the bucket {n['bucket']}", "storage.buckets.get", lambda: exists(f"{GCS}/b/{n['bucket']}")):
            step(f"create the bucket {n['bucket']}", "storage.buckets.create", lambda: rest.call(
                "POST", f"{GCS}/b", {"name": n["bucket"], "location": region, "labels": LABELS,
                                     "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}},
                params={"project": project}))
            note("gcs-bucket", n["bucket"], project=project)
        elif not any(x["type"] == "gcs-bucket" and x["name"] == n["bucket"] for x in rec["created"]):
            rec["status"] = "name taken"
            self.write(ctx, rec)
            raise ValueError(f"A bucket named {n['bucket']} already exists and Captain did not make it. Pick another name prefix.")

        def grant(get, put, roles: list[str], email: str) -> None:
            """Add these roles for one account on one resource, leaving every other grant as it is. A service
            account made a moment ago is not always known yet: wait and try again."""
            for i in range(8):
                try:
                    pol = get() or {}
                    b = pol.setdefault("bindings", [])
                    for role in roles:
                        cur = next((x for x in b if x["role"] == role and not x.get("condition")), None)
                        if cur is None:
                            b.append({"role": role, "members": [f"serviceAccount:{email}"]})
                        elif f"serviceAccount:{email}" not in cur["members"]:
                            cur["members"].append(f"serviceAccount:{email}")
                    put(pol)
                    return
                except GoogleError as e:
                    if "does not exist" in str(e) and i < 7:
                        time.sleep(self.SETTLE_S)
                        continue
                    raise

        say("Letting the two accounts into the bucket, and nowhere else")
        bucket_iam = f"{GCS}/b/{n['bucket']}/iam"
        for email in (n["account_email"], n["build_email"]):
            step("set who may use the bucket", "storage.buckets.setIamPolicy", lambda email=email: grant(
                lambda: rest.call("GET", bucket_iam), lambda pol: rest.call("PUT", bucket_iam, pol),
                ["roles/storage.objectViewer", "roles/storage.objectCreator"], email))

        say(f"Creating the image repository {n['repo']}")
        repo = f"{AR}/projects/{project}/locations/{region}/repositories/{n['repo']}"
        if not step(f"look up the image repository {n['repo']}", "artifactregistry.repositories.get", lambda: exists(repo)):
            step(f"create the image repository {n['repo']}", "artifactregistry.repositories.create", lambda: rest.wait(rest.call(
                "POST", f"{AR}/projects/{project}/locations/{region}/repositories", {"format": "DOCKER", "labels": LABELS, "description": "Captain's serving image"},
                params={"repositoryId": n["repo"]}), AR, deadline_s=300, poll_s=self.POLL_S))
            note("artifact-repository", n["repo"], project=project, region=region)
        step("let the build push to the repository", "artifactregistry.repositories.setIamPolicy", lambda: grant(
            lambda: rest.call("GET", f"{repo}:getIamPolicy"), lambda pol: rest.call("POST", f"{repo}:setIamPolicy", {"policy": pol}),
            ["roles/artifactregistry.writer"], n["build_email"]))

        folder, digest = SS.build_folder("gcp")
        tag = f"{__version__}-{digest[:10]}"
        image = f"{region}-docker.pkg.dev/{project}/{n['repo']}/serving:{tag}"
        have = step("look up the serving image", "artifactregistry.tags.get", lambda: exists(f"{repo}/packages/serving/tags/{tag}"))
        t0 = time.time()
        if have:
            say("The serving image for this version of Captain is already in your project")
        else:
            key = f"serving/build/{tag}.tgz"
            say("Uploading the build folder")
            step("upload the build folder", "storage.objects.create", lambda: rest.call(
                "POST", f"https://storage.googleapis.com/upload/storage/v1/b/{n['bucket']}/o", params={"uploadType": "media", "name": key},
                data=_tgz(folder), headers={"Content-Type": "application/gzip"}))
            say(f"Building the serving image in your project (about {BUILD_MINUTES} minutes)")
            body = {"source": {"storageSource": {"bucket": n["bucket"], "object": key}},
                    "steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "-t", image, "."]}], "images": [image],
                    "serviceAccount": f"projects/{project}/serviceAccounts/{n['build_email']}",
                    "logsBucket": f"gs://{n['bucket']}/serving/build-logs", "options": {"logging": "GCS_ONLY"}, "timeout": "1800s", "tags": ["captain-serving"]}

            def start():
                for i in range(8):  # the build account's grants take a moment to reach Cloud Build
                    try:
                        return rest.call("POST", f"{CB}/projects/{project}/locations/{region}/builds", body)
                    except GoogleError as e:
                        if i < 7 and e.status in (400, 403) and ("does not have" in str(e) or "permission" in str(e).lower()) and not api_off(e):
                            time.sleep(self.SETTLE_S)
                            continue
                        raise

            op = step("start the image build", "cloudbuild.builds.create", start)
            bid = ((op.get("metadata") or {}).get("build") or {}).get("id")
            rec["build"] = bid
            while True:
                b = step("watch the image build", "cloudbuild.builds.get", lambda: rest.call("GET", f"{CB}/projects/{project}/locations/{region}/builds/{bid}"))
                if b.get("status") == "SUCCESS":
                    break
                if b.get("status") not in ("QUEUED", "WORKING", "PENDING", "STATUS_UNKNOWN"):
                    rec["status"] = "image build failed"
                    self.write(ctx, rec)
                    why = b.get("statusDetail") or (b.get("failureInfo") or {}).get("detail") or b.get("status")
                    raise ValueError(f"The image build did not finish: {str(why)[:300]}. Its log is in gs://{n['bucket']}/serving/build-logs/.")
                if time.time() - t0 > 1800:
                    raise ValueError("The image build was still running after 30 minutes. Look at it in Cloud Build, then run setup again.")
                time.sleep(self.POLL_S)
        rec["image"] = image
        rec["build_seconds"] = None if have else round(time.time() - t0)
        cfg = {"image": image, "service_account": n["account_email"], "serving_storage": n["storage"], "name_prefix": n["services"], "project": project,
               "cpu": str(o["cpu"]), "memory_mb": str(o["memory_mb"]), "min_copies": str(o["min_copies"]), "max_copies": str(o["max_copies"])}
        say("Adding the compute target")
        try:
            C.get(ctx, n["target"])
            C.add(ctx, n["target"], self.kind, o["profile"], region, cfg, replace=True)
        except NotFound:
            C.add(ctx, n["target"], self.kind, o["profile"], region, cfg)
        note("compute-target", n["target"])
        rec.update(status="ready", target=n["target"], ready_at=now())
        self.write(ctx, rec)
        return {**rec, "text": f"Serving is set up in your Google Cloud project. Staging can use the target {n['target']}."}

    # ---- removal ------------------------------------------------------------------------------

    def teardown(self, ctx: Context, progress=None, apis: str = "keep", **_options) -> dict:
        self._apis = apis
        out = super().teardown(ctx, progress)
        kept = [x["name"] for x in (self.read(ctx) or {}).get("apis_left_on") or []]
        if kept and out.get("status") == "removed":
            it = "it" if len(kept) == 1 else "them"
            out["text"] += (f" Captain switched on {', '.join(kept)} and left {it} on, in case something else uses {it} now. Google also keeps "
                            "the project role it gave its own service agent when an API was first switched on.")
        return out

    def remove_one(self, ctx: Context, rec: dict, item: dict) -> None:
        rest = self._rest(rec["profile"])
        project, region, t, name = rec["project"], rec["region"], item["type"], item["name"]
        if t == "api":
            if getattr(self, "_apis", "keep") == "off":
                rest.wait(rest.call("POST", f"{SU}/projects/{project}/services/{name}:disable", {}), SU, deadline_s=300, poll_s=self.POLL_S)
            else:
                rec.setdefault("apis_left_on", []).append({"name": name})
        elif t == "artifact-repository":
            op = rest.call("DELETE", f"{AR}/projects/{project}/locations/{item.get('region') or region}/repositories/{name}", ok_missing=True)
            if op:
                rest.wait(op, AR, deadline_s=300, poll_s=self.POLL_S)
        elif t == "service-account":
            rest.call("DELETE", f"{IAM}/projects/{project}/serviceAccounts/{name}", ok_missing=True)
        elif t == "gcs-bucket":
            # The bucket is Captain's own: the serving bundles, the request log (already published into the
            # workspace when each endpoint stopped) and the build folder. Those go. Anything else stops it.
            ws = rec["storage"].split("://", 1)[1].partition("/")[2].strip("/")
            mine = tuple(f"{ws}/{p}" if ws else p for p in ("staging/", "workspace.yaml")) + ("serving/",)
            token, other = None, []
            while True:
                got = rest.call("GET", f"{GCS}/b/{name}/o", params={"maxResults": 1000, **({"pageToken": token} if token else {})}, ok_missing=True) or {}
                for obj in got.get("items") or []:
                    if obj["name"].startswith(mine):
                        rest.call("DELETE", f"{GCS}/b/{name}/o/{_quote(obj['name'])}", ok_missing=True)
                    else:
                        other.append(obj["name"])
                token = got.get("nextPageToken")
                if not token:
                    break
            if other:
                raise ValueError(f"it still holds {len(other)} object(s) Captain did not put there, such as {other[0]}. Captain doesn't delete data it can't account for.")
            rest.call("DELETE", f"{GCS}/b/{name}", ok_missing=True)

    def leftovers(self, ctx: Context) -> list[dict]:
        """What in the project still carries Captain's serving label or this setup's names, asked of Google."""
        from ...compute_providers.gcp_serving import RUN

        rec = self.read(ctx)
        if not rec:
            return []
        rest = self._rest(rec["profile"])
        project, region, n = rec["project"], rec["region"], rec.get("names") or {}
        out = []

        def ask(kind: str, fn) -> None:
            try:
                out.extend({"type": kind, "name": x} for x in fn())
            except Exception as e:  # noqa: BLE001
                if "SERVICE_DISABLED" in str(e) or "has not been used" in str(e) or "is disabled" in str(e):
                    return  # an API that is off holds nothing
                out.append({"type": kind, "name": "?", "error": f"could not list: {str(e)[:160]}"})

        def mine(lb) -> bool:
            return (lb or {}).get("app") == "captain" and (lb or {}).get("captain-purpose") == "serving"

        ask("cloud-run-service", lambda: [s["name"].rsplit("/", 1)[-1] for s in (rest.call("GET", f"{RUN}/projects/{project}/locations/{region}/services").get("services") or [])
                                          if mine(s.get("labels"))])
        ask("artifact-repository", lambda: [r["name"].rsplit("/", 1)[-1] for r in (rest.call("GET", f"{AR}/projects/{project}/locations/{region}/repositories").get("repositories") or [])
                                            if mine(r.get("labels"))])
        ask("gcs-bucket", lambda: [b["name"] for b in (rest.call("GET", f"{GCS}/b", params={"project": project, "prefix": n.get("services") or "captain"}).get("items") or [])
                                   if mine(b.get("labels"))])
        ask("service-account", lambda: [a["email"] for a in (rest.call("GET", f"{IAM}/projects/{project}/serviceAccounts", params={"pageSize": 100}).get("accounts") or [])
                                        if a["email"] in (n.get("account_email"), n.get("build_email"))])
        return out


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


def _tgz(folder_zip: bytes) -> bytes:
    """The build folder as the gzipped tar Cloud Build reads. serve.sh stays executable."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(folder_zip)) as z, tarfile.open(fileobj=buf, mode="w:gz") as t:
        for info in z.infolist():
            data = z.read(info.filename)
            ti = tarfile.TarInfo(info.filename)
            ti.size = len(data)
            ti.mode = 0o755 if info.filename == "serve.sh" else 0o644
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()
