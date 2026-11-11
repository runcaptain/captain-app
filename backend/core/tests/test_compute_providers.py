"""Every compute kind passes the same contract against a fake of its SDK: plan, submit, the five states, logs, cancel.

The fakes return the response shapes of the real SDKs (boto3 ECS/Batch/CloudWatch, google-cloud-run, aiplatform_v1,
logging_v2, azure-mgmt-appcontainers, azure-ai-ml, azure-monitor-query, modal). Live runs are separate: see
docs/api.md "Compute providers" for which kinds have run on a real account.
"""
import datetime as dt
import os
import sys
import time
import types

import pytest

from captain_search import compute as C
from captain_search import compute_providers as P
from captain_search import jobs as J
from captain_search.workspace import init_workspace

T0 = dt.datetime(2026, 11, 6, 20, 0, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(seconds=95)


class World:
    """One fake job's truth, which every fake SDK below reports in its own words."""

    def __init__(self):
        self.state = "queued"
        self.lines = ["loading workspace", "running 2 arms", "done"]
        self.calls = []


def NS(**kw):
    return types.SimpleNamespace(**kw)


# ---- AWS ----------------------------------------------------------------------------------


class _Exc:
    class ResourceNotFoundException(Exception):
        pass

    class ClientException(Exception):
        pass


class FakeECS:
    exceptions = _Exc

    def __init__(self, w):
        self.w = w

    def run_task(self, **kw):
        self.w.calls.append(("run_task", kw))
        return {"tasks": [{"taskArn": "arn:aws:ecs:us-west-1:1:task/captain-search/abc123",
                           "taskDefinitionArn": "arn:aws:ecs:us-west-1:1:task-definition/captain-search-runner:3"}], "failures": []}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": {"family": "captain-search-runner", "revision": 3, "containerDefinitions": [
            {"name": "runner", "image": "img", "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": "/captain-search/runner", "awslogs-stream-prefix": "cst"}}}]}}

    def describe_tasks(self, cluster, tasks):
        s = self.w.state
        base = {"taskArn": tasks[0], "createdAt": T0, "desiredStatus": "RUNNING"}
        if s == "queued":
            return {"tasks": [{**base, "lastStatus": "PENDING"}]}
        if s == "running":
            return {"tasks": [{**base, "lastStatus": "RUNNING", "startedAt": T0}]}
        stop = {**base, "lastStatus": "STOPPED", "desiredStatus": "STOPPED", "startedAt": T0, "stoppedAt": T1}
        if s == "succeeded":
            return {"tasks": [{**stop, "stopCode": "EssentialContainerExited", "containers": [{"name": "runner", "exitCode": 0}]}]}
        if s == "failed":
            return {"tasks": [{**stop, "stopCode": "EssentialContainerExited", "containers": [{"name": "runner", "exitCode": 2}]}]}
        return {"tasks": [{**stop, "stopCode": "UserInitiated", "stoppedReason": "cancelled from Captain by t",
                           "containers": [{"name": "runner", "exitCode": 143}]}]}

    def stop_task(self, **kw):
        self.w.calls.append(("stop_task", kw))
        self.w.state = "stopped"


class FakeBatch:
    def __init__(self, w):
        self.w = w

    def submit_job(self, **kw):
        self.w.calls.append(("submit_job", kw))
        return {"jobId": "j-1", "jobArn": "arn:aws:batch:us-west-1:1:job/j-1"}

    def describe_jobs(self, jobs):
        s = self.w.state
        st = {"queued": "RUNNABLE", "running": "RUNNING", "succeeded": "SUCCEEDED", "failed": "FAILED", "stopped": "FAILED"}[s]
        ct = {"logStreamName": "cst/default/x", "exitCode": {"succeeded": 0, "failed": 1}.get(s)}
        j = {"jobId": jobs[0], "status": st, "container": ct, "startedAt": 1793995200000}
        if s == "stopped":
            j["statusReason"] = "Captain: cancelled by t"
        return {"jobs": [j]}

    def terminate_job(self, **kw):
        self.w.calls.append(("terminate_job", kw))
        self.w.state = "stopped"


class FakeLogs:
    exceptions = _Exc

    def __init__(self, w):
        self.w = w

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None):
        n = len(self.w.lines)
        if nextToken == f"f/{n}":
            return {"events": [], "nextForwardToken": nextToken}
        start = int(nextToken.split("/")[1]) if nextToken else 0
        ev = [{"timestamp": 1793995200000 + i, "message": m} for i, m in enumerate(self.w.lines[start:])]
        return {"events": ev, "nextForwardToken": f"f/{n}"}


class FakeSession:
    def __init__(self, w):
        self.w = w

    def client(self, name):
        return {"ecs": FakeECS, "batch": FakeBatch, "logs": FakeLogs}[name](self.w)


# ---- Google -------------------------------------------------------------------------------


def _gcp_modules(w):
    class Execs:
        def __init__(self, credentials=None):
            pass

        def get_execution(self, name):
            s = w.state
            done = s in ("succeeded", "failed", "stopped")
            return NS(start_time=T0 if s != "queued" else None, completion_time=T1 if done else None,
                      running_count=1 if s == "running" else 0, failed_count=1 if s == "failed" else 0,
                      cancelled_count=1 if s == "stopped" else 0,
                      conditions=[NS(type_="Completed", message="Task cst-0 failed with exit code 2" if s == "failed" else "")])

        def cancel_execution(self, name):
            w.calls.append(("cancel_execution", name))
            w.state = "stopped"

    class Jobs:
        def __init__(self, credentials=None):
            pass

        def run_job(self, request):
            w.calls.append(("run_job", request.kw))
            return NS(metadata=NS(name="projects/p1/locations/us-central1/jobs/cst/executions/cst-x1"), operation=NS(name="op1"))

    run_v2 = types.ModuleType("google.cloud.run_v2")
    run_v2.JobsClient, run_v2.ExecutionsClient = Jobs, Execs
    run_v2.RunJobRequest = lambda **kw: NS(kw=kw)

    class JobSvc:
        def __init__(self, credentials=None, client_options=None):
            pass

        def create_custom_job(self, parent, custom_job):
            w.calls.append(("create_custom_job", {"parent": parent, "custom_job": custom_job}))
            return NS(name=f"{parent}/customJobs/777")

        def get_custom_job(self, name):
            st = {"queued": "JOB_STATE_PENDING", "running": "JOB_STATE_RUNNING", "succeeded": "JOB_STATE_SUCCEEDED",
                  "failed": "JOB_STATE_FAILED", "stopped": "JOB_STATE_CANCELLED"}[w.state]
            return NS(state=NS(name=st), error=NS(message="exit 2" if w.state == "failed" else ""), start_time=T0, end_time=None)

        def cancel_custom_job(self, name):
            w.calls.append(("cancel_custom_job", name))
            w.state = "stopped"

        def list_custom_jobs(self, request=None, *, parent=None):  # the real one takes no page_size keyword
            w.calls.append(("list_custom_jobs", request))
            return iter([])

    aip = types.ModuleType("google.cloud.aiplatform_v1")
    aip.JobServiceClient = JobSvc

    class LogClient:
        def __init__(self, project=None, credentials=None):
            pass

        def list_entries(self, filter_, order_by, max_results):
            w.calls.append(("list_entries", filter_))
            return [NS(payload=m, timestamp=T0 + dt.timedelta(seconds=i)) for i, m in enumerate(w.lines)]

    lg = types.ModuleType("google.cloud.logging_v2")
    lg.Client, lg.ASCENDING = LogClient, "timestamp asc"
    return {"google.cloud.run_v2": run_v2, "google.cloud.aiplatform_v1": aip, "google.cloud.logging_v2": lg}


# ---- Azure --------------------------------------------------------------------------------


def _azure_modules(w):
    class Poller:
        def __init__(self, r=None):
            self.r = r

        def result(self):
            return self.r

    class ACAJobs:
        def begin_start(self, rg, job, template):
            w.calls.append(("begin_start", {"rg": rg, "job": job, "template": template}))
            return Poller(NS(name="cst-job-abc12", id="/subs/x/jobs/cst-job/executions/cst-job-abc12"))

        def begin_stop_execution(self, rg, job, ex):
            w.calls.append(("begin_stop_execution", ex))
            w.state = "stopped"
            return Poller()

    class ACA:
        def __init__(self, cred, sub):
            self.jobs = ACAJobs()

        def job_execution(self, rg, job, ex):
            st = {"queued": "Processing", "running": "Running", "succeeded": "Succeeded", "failed": "Failed", "stopped": "Stopped"}[w.state]
            return NS(status=st, start_time=T0, end_time=None)

    aca = types.ModuleType("azure.mgmt.appcontainers")
    aca.ContainerAppsAPIClient = ACA

    class LQ:
        def __init__(self, cred):
            pass

        def query_workspace(self, wid, q, timespan):
            w.calls.append(("query_workspace", q))
            return NS(tables=[NS(rows=[(T0 + dt.timedelta(seconds=i), m) for i, m in enumerate(w.lines)])])

    mq = types.ModuleType("azure.monitor.query")
    mq.LogsQueryClient = LQ

    class AMLJobs:
        def create_or_update(self, job):
            w.calls.append(("create_or_update", job))
            return NS(name="aml-1", studio_url="https://ml.azure.com/runs/aml-1")

        def get(self, name):
            st = {"queued": "Queued", "running": "Running", "succeeded": "Completed", "failed": "Failed", "stopped": "Canceled"}[w.state]
            return NS(status=st, creation_context=NS(created_at=T0))

        def begin_cancel(self, name):
            w.calls.append(("begin_cancel", name))
            w.state = "stopped"
            return Poller()

        def download(self, name, download_path, all):
            d = os.path.join(download_path, "artifacts", "user_logs")
            os.makedirs(d)
            open(os.path.join(d, "std_log.txt"), "w").write("\n".join(w.lines) + "\n")

    class MLClient:
        def __init__(self, cred, sub, rg, ws):
            self.jobs = AMLJobs()

    ml = types.ModuleType("azure.ai.ml")
    ml.MLClient, ml.command = MLClient, (lambda **kw: kw)
    ents = types.ModuleType("azure.ai.ml.entities")
    ents.Environment = lambda image: NS(image=image)
    return {"azure.mgmt.appcontainers": aca, "azure.monitor.query": mq, "azure.ai.ml": ml, "azure.ai.ml.entities": ents}


# ---- Modal --------------------------------------------------------------------------------


def _modal_module(w):
    class InputCancellation(Exception):
        pass

    class Call:
        object_id = "fc-01"

        def get(self, timeout=None):
            if w.state in ("queued", "running"):
                raise TimeoutError()
            if w.state == "stopped":
                raise InputCancellation("cancelled")
            return 0 if w.state == "succeeded" else 3

        def cancel(self):
            w.calls.append(("cancel", None))
            w.state = "stopped"

    class Fn:
        def spawn(self, env, arg):
            w.calls.append(("spawn", {"env": env, "arg": arg}))
            return Call()

    m = types.ModuleType("modal")
    m.Function = NS(from_name=lambda app, fn: Fn())
    m.FunctionCall = NS(from_id=lambda i: Call())
    return {"modal": m}


# ---- fixtures -----------------------------------------------------------------------------

TARGETS = {
    "aws-ecs": ("aws", "us-west-1", {"cluster": "captain-search", "task_definition": "captain-search-runner",
                                     "subnets": "subnet-a,subnet-b", "security_groups": "sg-1", "assign_public_ip": "ENABLED"}),
    "aws-batch": ("aws", "us-west-1", {"job_queue": "q", "job_definition": "d"}),
    "gcp-cloud-run": ("gcp", "us-central1", {"job": "cst"}),
    "gcp-vertex": ("gcp", "us-central1", {}),
    "azure-container-apps": ("azure", None, {"resource_group": "rg", "job": "cst-job", "log_analytics_workspace_id": "w-1"}),
    "azure-ml": ("azure", None, {"resource_group": "rg", "workspace_name": "aml", "compute": "cpu"}),
    "modal": (None, None, {}),
}


@pytest.fixture
def world(monkeypatch):
    from captain_search import cloud

    w = World()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: FakeSession(w))
    monkeypatch.setattr(cloud, "gcp_credentials", lambda profile: ("creds", "p1"))
    monkeypatch.setattr(cloud, "azure_credential", lambda profile: ("cred", None, "sub-1"))
    mods = {**_gcp_modules(w), **_azure_modules(w), **_modal_module(w)}
    for parent in ("google", "google.cloud", "azure", "azure.mgmt", "azure.monitor", "azure.ai"):
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
        parent, _, leaf = name.rpartition(".")
        if parent:
            monkeypatch.setattr(sys.modules[parent], leaf, mod, raising=False)
    return w


@pytest.fixture
def cloud_ctx(tmp_path):
    c = init_workspace("ws", str(tmp_path / "bucket"))
    c.workspace.storage_uri = "s3://captain-test/cst"  # the plans read this; the store itself stays on disk
    return c


def _target(ctx, kind):
    prov, region, cfg = TARGETS[kind]
    return C.add(ctx, kind.replace("-", "")[:12], kind, "p" if prov else None, region, cfg)


@pytest.mark.parametrize("kind", list(TARGETS))
def test_every_kind_runs_the_same_job_contract(kind, world, cloud_ctx):
    t = _target(cloud_ctx, kind)
    rec = C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1", backtest="bt1", kind="backtest")
    assert rec["handle"] and rec["state"] == "queued" and rec["target_kind"] == kind
    assert J.load(cloud_ctx, rec["id"])["handle"] == rec["handle"]

    # The provider received cst arguments only (the image's entrypoint is cst) and the workspace environment.
    name, sent = world.calls[0]
    flat = repr(sent)
    assert "backtest" in flat and "bt1" in flat and "CST_STORAGE" in flat and rec["id"] in flat
    assert "sh -c" not in flat

    expect = {"queued": "running" if kind == "modal" else "queued"}
    for s in ("queued", "running", "succeeded"):
        world.state = s
        r = J.show(cloud_ctx, rec["id"])
        assert r["state"] == expect.get(s, s), (kind, s, r)
        assert r["reason"]
    # finished jobs are not asked again
    world.state = "failed"
    assert J.show(cloud_ctx, rec["id"])["state"] == "succeeded"

    logs = J.logs(cloud_ctx, rec["id"])
    if kind == "modal":
        assert logs["lines"] == [] and "modal app logs" in logs["note"]
    else:
        assert [ln["message"] for ln in logs["lines"]] == world.lines
        assert logs["next"]


@pytest.mark.parametrize("kind", list(TARGETS))
def test_every_kind_reports_failure_and_cancel(kind, world, cloud_ctx):
    t = _target(cloud_ctx, kind)
    a = C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1", backtest="bt1")
    world.state = "failed"
    r = J.show(cloud_ctx, a["id"])
    assert r["state"] == "failed" and r["reason"] and r["finished_at"]

    b = C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1", backtest="bt1")
    world.state = "running"
    r = J.cancel(cloud_ctx, b["id"])
    assert world.state == "stopped" and r["cancelled_by"] == "tester@captain.dev"
    assert J.show(cloud_ctx, b["id"])["state"] == "stopped"
    assert "nothing to cancel" in J.cancel(cloud_ctx, b["id"])["note"]
    ids = [x["id"] for x in J.listing(cloud_ctx)]
    assert ids == [b["id"], a["id"]]  # newest first


def test_ecs_plan_and_log_location(world, cloud_ctx):
    t = _target(cloud_ctx, "aws-ecs")
    plan = C.plan_backtest(cloud_ctx, t, "bt1")[0]["args"]
    net = plan["networkConfiguration"]["awsvpcConfiguration"]
    assert net == {"subnets": ["subnet-a", "subnet-b"], "securityGroups": ["sg-1"], "assignPublicIp": "ENABLED"}
    assert {"key": "captain:purpose", "value": "cst-compute"} in plan["tags"]
    assert plan["overrides"]["containerOverrides"][0]["command"] == ["-w", "ws", "backtest", "run", "bt1"]
    rec = C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1")
    assert rec["handle"]["log_stream"] == "cst/runner/abc123"
    world.state = "succeeded"
    r = J.show(cloud_ctx, rec["id"])
    assert r["run_seconds"] == 95.0 and r["exit_code"] == 0
    # the cursor returns only lines written after it
    first = J.logs(cloud_ctx, rec["id"])
    world.lines.append("late line")
    assert [x["message"] for x in J.logs(cloud_ctx, rec["id"], first["next"])["lines"]] == ["late line"]


def test_submit_failure_is_recorded(world, cloud_ctx, monkeypatch):
    t = _target(cloud_ctx, "aws-ecs")
    monkeypatch.setattr(FakeECS, "run_task", lambda self, **kw: {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]})
    with pytest.raises(P.ProviderError, match="RESOURCE:MEMORY"):
        C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1")
    (rec,) = J.listing(cloud_ctx)
    assert rec["state"] == "failed" and "RESOURCE:MEMORY" in rec["reason"]


def test_cloud_kinds_need_a_cloud_workspace(world, tmp_path):
    c = init_workspace("disk", str(tmp_path / "b2"))
    t = C.add(c, "ecs", "aws-ecs", "p", "us-west-1", TARGETS["aws-ecs"][2])
    with pytest.raises(ValueError, match="cloud workspace"):
        C.submit_job(c, t, ["backtest", "run", "x"], label="x")
    assert J.listing(c) == []


def test_local_provider_runs_cst_in_a_child_process(tmp_path):
    c = init_workspace("loc", str(tmp_path / "b3"))
    rec = C.submit_job(c, C.get(c, "local"), ["compute", "kinds"], label="kinds")
    for _ in range(120):
        r = J.show(c, rec["id"])
        if r["state"] != "running":
            break
        time.sleep(0.25)
    assert r["state"] == "succeeded", r
    out = "\n".join(x["message"] for x in J.logs(c, rec["id"])["lines"])
    assert "aws-ecs" in out and "gcp-vertex" in out


def test_plugins_load_from_entry_points(monkeypatch, cloud_ctx):
    class Slurm(P.ComputeProvider):
        kind, provider, label = "slurm", "none", "Our Slurm cluster"
        fields = [{"name": "partition", "label": "Partition", "required": True}]

        def plan(self, job):
            return [{"service": "slurm", "call": "sbatch", "args": {"argv": job.argv}}]

        def submit(self, job):
            return {"slurm_id": 42}

        def status(self, h):
            return P.status("running", "squeue says R")

        def logs(self, h, since=None):
            return {"lines": [], "next": None}

        def cancel(self, h):
            return P.status("stopped", "scancel")

        def test(self):
            return {"ok": True, "detail": "sinfo answered"}

    class Broken:
        kind = "broken"

    eps = [NS(name="slurm", value="acme:Slurm", load=lambda: Slurm), NS(name="broken", value="acme:Broken", load=lambda: Broken),
           NS(name="aws-ecs", value="evil:ECS", load=lambda: Slurm)]
    monkeypatch.setattr(P, "entry_points", lambda group: eps if group == "captain_search.compute" else [])
    kinds = {k["kind"]: k for k in C.kinds()}
    assert kinds["slurm"]["label"] == "Our Slurm cluster" and "broken" not in kinds
    assert kinds["aws-ecs"]["label"] == "AWS ECS (Fargate)"  # a plugin cannot replace a built-in kind
    errs = P.plugin_errors()
    assert "missing" in errs["broken"] and "built-in" in errs["aws-ecs"]
    t = C.add(cloud_ctx, "hpc", "slurm", None, None, {"partition": "gpu"})
    rec = C.submit_job(cloud_ctx, t, ["backtest", "run", "x"], label="x")
    assert rec["handle"] == {"slurm_id": 42}
    assert J.show(cloud_ctx, rec["id"])["reason"] == "squeue says R"
    assert C.test(cloud_ctx, "hpc")["detail"] == "sinfo answered"


def test_job_routes(world, cloud_ctx, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server

    monkeypatch.setattr(server, "ctx", lambda: cloud_ctx)
    t = _target(cloud_ctx, "aws-ecs")
    rec = C.submit_job(cloud_ctx, t, ["backtest", "run", "bt1"], label="bt1")
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert [j["id"] for j in c.get("/api/jobs", headers=h).json()] == [rec["id"]]
    world.state = "running"
    assert c.get("/api/jobs?refresh=true", headers=h).json()[0]["state"] == "running"
    assert c.get(f"/api/jobs/{rec['id']}", headers=h).json()["state"] == "running"
    lg = c.get(f"/api/jobs/{rec['id']}/logs", headers=h).json()
    assert [x["message"] for x in lg["lines"]] == world.lines
    assert c.get(f"/api/jobs/{rec['id']}/logs", headers=h, params={"since": lg["next"]}).json()["lines"] == []
    assert c.post(f"/api/jobs/{rec['id']}/cancel", headers=h).json()["cancelled_by"] == "tester@captain.dev"
    assert c.get("/api/jobs/job-nope", headers=h).status_code == 404
    assert c.get("/api/jobs", headers={}).status_code == 401


def test_runner_attaches_to_the_workspace_from_env(tmp_path, monkeypatch):
    from captain_search import workspace as W

    init_workspace("shared", str(tmp_path / "bkt"))
    os.remove(W.config_path())  # a fresh container: no local config at all
    monkeypatch.setenv("CST_STORAGE", str(tmp_path / "bkt"))
    monkeypatch.setenv("CST_WORKSPACE", "shared")
    assert W.open_context("shared").workspace.name == "shared"
    monkeypatch.setenv("CST_STORAGE", str(tmp_path / "empty"))
    monkeypatch.setenv("CST_WORKSPACE", "other")
    with pytest.raises(Exception):
        W.open_context("other")  # attaches only: an empty bucket is not made into a workspace


def test_vertex_target_test_asks_for_one_job_with_a_request(world, cloud_ctx):
    """Live, 2026-11-10: `cst compute test` on Vertex failed with "JobServiceClient.list_custom_jobs() got an
    unexpected keyword argument 'page_size'". The page size has to travel inside the request."""
    r = P.get(_target(cloud_ctx, "gcp-vertex"), cloud_ctx).test()
    assert r["ok"], r
    assert world.calls[-1] == ("list_custom_jobs", {"parent": "projects/p1/locations/us-central1", "page_size": 1})


def test_gcp_calls_bind_to_the_real_sdk_signatures():
    """The fakes above accept whatever the provider sends, which is how a wrong keyword reached a real account.
    Where the Google SDK is installed, bind each call the provider makes to the real method's signature."""
    import inspect

    run_v2 = pytest.importorskip("google.cloud.run_v2")
    aip = pytest.importorskip("google.cloud.aiplatform_v1")

    def binds(fn, **kw):
        inspect.signature(fn).bind(None, **kw)

    binds(run_v2.JobsClient.run_job, request=None)
    binds(run_v2.JobsClient.get_job, name="n")
    binds(run_v2.ExecutionsClient.get_execution, name="n")
    binds(run_v2.ExecutionsClient.cancel_execution, name="n")
    binds(aip.JobServiceClient.create_custom_job, parent="p", custom_job={})
    binds(aip.JobServiceClient.get_custom_job, name="n")
    binds(aip.JobServiceClient.cancel_custom_job, name="n")
    binds(aip.JobServiceClient.list_custom_jobs, request={})
    with pytest.raises(TypeError):
        binds(aip.JobServiceClient.list_custom_jobs, parent="p", page_size=1)
