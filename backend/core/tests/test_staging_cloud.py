"""Staging on SageMaker, against a fake AWS: what Captain creates, how it is tagged, and that nothing is left behind.
The fake endpoint runs the real runtime over the real bundle, so invoking it is the cloud path end to end."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from captain_search import compute as C
from captain_search import staging as SG
from captain_search.compute_providers import ProviderError
from captain_search.compute_providers.aws import SageMakerProvider
from captain_search.staging import runtime as RT

from test_staging import keychain, ws  # noqa: F401  (fixtures)

ROLE = "arn:aws:iam::000000000000:role/captain-staging"
IMAGE = "000000000000.dkr.ecr.us-west-1.amazonaws.com/captain-search-runner:test"


class Boom(Exception):
    def __init__(self, msg, op="Call"):
        super().__init__(msg)
        self.response = {"Error": {"Code": "ValidationException", "Message": msg}}
        self.operation_name = op


class FakeAWS:
    def __init__(self, ctx):
        self.ctx = ctx
        self.models, self.configs, self.endpoints = {}, {}, {}
        self.calls = []
        self.fail_on = None       # a call name that raises
        self.endpoint_fails = False  # the endpoint is created, then goes Failed
        self.runtimes = {}
        self.role_denies = set()   # actions the execution role lacks
        self.iam_refuses = False   # Captain's own sign-in may not ask IAM

    def client(self, name):
        return self


class FakeSM:
    def __init__(self, w):
        self.w = w

    def _do(self, call, kw):
        self.w.calls.append((call, kw))
        if self.w.fail_on == call:
            raise Boom(f"not authorized to perform {call}", call)

    def create_model(self, **kw):
        self._do("create_model", kw)
        self.w.models[kw["ModelName"]] = kw

    def create_endpoint_config(self, **kw):
        self._do("create_endpoint_config", kw)
        self.w.configs[kw["EndpointConfigName"]] = kw

    def create_endpoint(self, **kw):
        self._do("create_endpoint", kw)
        self.w.endpoints[kw["EndpointName"]] = {**kw, "polls": 0}

    def update_endpoint(self, **kw):
        self._do("update_endpoint", kw)
        self.w.endpoints[kw["EndpointName"]].update(EndpointConfigName=kw["EndpointConfigName"], polls=0)

    def describe_endpoint(self, EndpointName):
        e = self.w.endpoints.get(EndpointName)
        if e is None:
            raise Boom(f"Could not find endpoint \"{EndpointName}\".")
        e["polls"] += 1
        if self.w.endpoint_fails:
            return {"EndpointStatus": "Failed", "FailureReason": "The primary container did not pass the ping health check.",
                    "EndpointConfigName": e["EndpointConfigName"]}
        return {"EndpointStatus": "InService" if e["polls"] > 1 else "Creating", "EndpointConfigName": e["EndpointConfigName"]}

    def _delete(self, call, store, name):
        self._do(call, {"name": name})
        if name not in store:
            raise Boom(f"Could not find {name}")
        del store[name]

    def delete_endpoint(self, EndpointName):
        self._delete("delete_endpoint", self.w.endpoints, EndpointName)
        self.w.runtimes.pop(EndpointName, None)

    def delete_endpoint_config(self, EndpointConfigName):
        self._delete("delete_endpoint_config", self.w.configs, EndpointConfigName)

    def delete_model(self, ModelName):
        self._delete("delete_model", self.w.models, ModelName)

    # iam: what the execution role may do, asked without changing anything
    def simulate_principal_policy(self, PolicySourceArn, ActionNames, ResourceArns):
        self.w.calls.append(("simulate_principal_policy", {"action": ActionNames[0], "resource": ResourceArns[0]}))
        if self.w.iam_refuses:
            raise Boom("not authorized to perform iam:SimulatePrincipalPolicy", "SimulatePrincipalPolicy")
        return {"EvaluationResults": [{"EvalActionName": ActionNames[0],
                                       "EvalDecision": "implicitDeny" if ActionNames[0] in self.w.role_denies else "allowed"}]}

    def delete_log_group(self, logGroupName):
        self.w.calls.append(("delete_log_group", {"name": logGroupName}))

    def list_endpoints(self, **kw):
        return {"Endpoints": [{"EndpointName": n, "EndpointArn": f"endpoint/{n}"} for n in self.w.endpoints]}

    def list_endpoint_configs(self, **kw):
        return {"EndpointConfigs": [{"EndpointConfigName": n, "EndpointConfigArn": f"config/{n}"} for n in self.w.configs]}

    def list_models(self, **kw):
        return {"Models": [{"ModelName": n, "ModelArn": f"model/{n}"} for n in self.w.models]}

    def list_tags(self, ResourceArn):
        kind, name = ResourceArn.split("/", 1)
        return {"Tags": {"endpoint": self.w.endpoints, "config": self.w.configs, "model": self.w.models}[kind][name].get("Tags", [])}

    # sagemaker-runtime: the container. It loads the bundle its model's environment names and answers /invocations.
    def invoke_endpoint(self, EndpointName, ContentType, Accept, Body):
        e = self.w.endpoints[EndpointName]
        model = self.w.models[self.w.configs[e["EndpointConfigName"]]["ProductionVariants"][0]["ModelName"]]
        env = model["PrimaryContainer"]["Environment"]
        if not env:  # the team's own image: it answers {query, k} with {ids}
            return {"Body": io.BytesIO(json.dumps({"ids": ["own-1", "own-2"]}).encode())}
        key = env["CST_STAGING_BUNDLE"]
        if EndpointName not in self.w.runtimes or self.w.runtimes[EndpointName][0] != key:
            rt = RT.Runtime(self.w.ctx, key, key_sha256=env["CST_STAGING_KEY_SHA256"])
            self.w.runtimes[EndpointName] = (key, rt, TestClient(RT.make_app(rt)))
        r = self.w.runtimes[EndpointName][2].post("/invocations", json=json.loads(Body))
        if r.status_code >= 400:
            raise Boom(f"Received client error ({r.status_code}) from primary with message {r.text}", "InvokeEndpoint")
        return {"Body": io.BytesIO(r.content)}


@pytest.fixture
def aws(ws, monkeypatch):  # noqa: F811
    from captain_search import cloud

    w = FakeAWS(ws)
    sm = FakeSM(w)
    w.client = lambda name: sm
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: w)
    monkeypatch.setattr(SageMakerProvider, "POLL_S", 0)
    ws.workspace.storage_uri = "s3://captain-test/cst"  # the plans read this; the store itself stays on disk
    C.add(ws, "sm", "aws-sagemaker", "p", "us-west-1", {"image": IMAGE, "execution_role_arn": ROLE,
                                                       "instance_type": "ml.t2.medium", "name_prefix": "captain-search-test"})
    return w


def test_a_cloud_deploy_shows_its_cost_and_needs_a_confirm(ws, aws):  # noqa: F811
    p = SG.plan(ws, "shop", target="sm")
    # The hourly figure is the shared estimator's, which prices by region (us-west-1 here), not staging's own
    # flat fallback table. Both must say the same thing, so read the number back and check the sentence uses it.
    hourly = p["cost"]["hourly_usd"]
    assert p["needs_confirm"] and p["cost"]["instance_type"] == "ml.t2.medium" and 0.04 < hourly < 0.10
    text = p["cost"]["text"].lower()
    assert f"about ${hourly:.2f} an hour" in text and "keeps billing until you stop it" in text
    with pytest.raises(SG.NeedsConfirm) as e:
        SG.stage(ws, "shop", target="sm")
    assert e.value.plan["cost"] == p["cost"]
    assert [c for c, _ in aws.calls] == ["simulate_principal_policy"] * 4 and SG.state(ws, "shop") is None  # only asked, nothing created
    assert p["preflight"] == {"checked": True, "can_delete": True, "can_write": True, "warnings": [], "note": None}


def test_stage_query_and_unstage_leave_nothing_behind(ws, aws):  # noqa: F811
    said = []
    r = SG.stage(ws, "shop", target="sm", confirm=True, progress=said.append)
    assert r["status"] == "running" and r["kind"] == "aws-sagemaker"
    assert r["endpoint"] == {"name": "captain-search-test-shop-staging", "region": "us-west-1"}
    assert [x["type"] for x in r["resources"]] == ["endpoint", "model", "endpoint-config"]
    assert any("4 to 10 minutes" in s for s in said)
    # every resource carries Captain's tags, so it can be found and removed without Captain's own records
    for store in (aws.models, aws.configs, aws.endpoints):
        (item,) = store.values()
        tags = {t["Key"]: t["Value"] for t in item["Tags"]}
        assert tags["app"] == "captain" and tags["project"] == "shop" and tags["env"] == "staging"
    assert len(C.tagged(ws, C.get(ws, "sm"), "shop", "staging")) == 3
    env = next(iter(aws.models.values()))["PrimaryContainer"]["Environment"]
    assert env["CST_STAGING_BUNDLE"] == r["bundle"] and env["CST_STORAGE"] == "s3://captain-test/cst"
    # the container is told the key's hash, never the key; the record names no secret
    key = SG._key(SG.state(ws, "shop"))
    assert key not in json.dumps(aws.calls, default=str) and key not in json.dumps(SG.public(SG.state(ws, "shop")))

    q = SG.query(ws, "shop", "jacket", k=4)
    assert len(q["staged"]["results"]) == 4 and q["baseline"]["model_version"] == ws.baseline
    s = SG.status(ws, "shop")
    assert s["status"] == "running" and s["health"]["requests"] == 2
    with pytest.raises(ProviderError, match="refused"):
        C.invoke_staged(ws, C.get(ws, "sm"), SG.state(ws, "shop"), {"op": "rank", "key": "wrong", "query": "x"})

    out = SG.unstage(ws, "shop")
    assert out["status"] == "stopped" and out["resources"] == []
    assert out["spent"]["text"].startswith("Cost ") and out["spent"]["usd"] >= 0  # what it cost while it was up
    from captain_search import cost_estimates as CE
    assert CE.estimate(ws, "staging", "sm", {"instances": 1, "instance_type": "ml.t2.medium"})["low"] == r["cost"]["hourly_usd"]
    assert ("delete_log_group", {"name": "/aws/sagemaker/Endpoints/captain-search-test-shop-staging"}) in aws.calls  # its log group too
    assert not aws.models and not aws.configs and not aws.endpoints
    assert C.tagged(ws, C.get(ws, "sm"), "shop", "staging") == []


@pytest.mark.parametrize("fail_on", ["create_model", "create_endpoint_config", "create_endpoint"])
def test_a_failed_start_removes_what_it_created(ws, aws, fail_on):  # noqa: F811
    aws.fail_on = fail_on
    with pytest.raises(ProviderError, match="not authorized"):
        SG.stage(ws, "shop", target="sm", confirm=True)
    assert not aws.models and not aws.configs and not aws.endpoints
    rec = SG.state(ws, "shop")
    assert rec["status"] == "failed" and rec["resources"] == [] and "not authorized" in rec["error"]


def test_an_endpoint_that_fails_to_come_up_is_removed(ws, aws):  # noqa: F811
    aws.endpoint_fails = True
    with pytest.raises(ProviderError, match="did not pass the ping health check"):
        SG.stage(ws, "shop", target="sm", confirm=True)
    assert not aws.models and not aws.configs and not aws.endpoints
    assert SG.state(ws, "shop")["status"] == "failed"
    assert "Staging failed" in SG.status(ws, "shop")["text"]


def test_what_cannot_be_removed_is_named(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", target="sm", confirm=True)
    aws.fail_on = "delete_model"
    out = SG.unstage(ws, "shop")
    assert out["status"] == "failed" and [r["type"] for r in out["resources"]] == ["model"]
    assert "could not remove: model" in out["error"]
    aws.fail_on = None
    assert SG.unstage(ws, "shop")["resources"] == [] and not aws.models


def test_restaging_moves_the_endpoint_and_removes_the_old_model(ws, aws):  # noqa: F811
    a = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    old = {x["name"] for x in a["resources"] if x["type"] != "endpoint"}
    b = SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    assert [c for c, _ in aws.calls].count("update_endpoint") == 1 and len(aws.endpoints) == 1
    assert len(aws.models) == 1 and len(aws.configs) == 1 and not old & set(aws.models)
    assert SG.query(ws, "shop", "jacket")["staged"]["model_version"] == ws.candidate == b["model_version"]
    SG.unstage(ws, "shop")
    assert not aws.models and not aws.configs and not aws.endpoints


def test_an_endpoint_that_deleted_itself_is_noticed_and_cleaned_up(ws, aws):  # noqa: F811
    r = SG.stage(ws, "shop", target="sm", confirm=True)
    del aws.endpoints[r["endpoint"]["name"]]  # the idle container asked SageMaker to delete its own endpoint
    s = SG.status(ws, "shop")
    assert s["status"] == "stopped" and "stopped by itself" in s["staged"]["stop_reason"]
    assert not aws.models and not aws.configs and s["staged"]["resources"] == []


def test_a_team_brings_its_own_container_image(ws, aws):  # noqa: F811
    theirs = "000000000000.dkr.ecr.us-west-1.amazonaws.com/our-ranker:7"
    r = SG.stage(ws, "shop", image=theirs, target="sm", confirm=True)
    model = next(iter(aws.models.values()))["PrimaryContainer"]
    assert model["Image"] == theirs and model["Environment"] == {}
    assert r["how"] == "image" and r["model_version"] == theirs
    q = SG.query(ws, "shop", "jacket")
    assert [x["id"] for x in q["staged"]["results"]] == ["own-1", "own-2"] and q["baseline"] is None
    assert SG.status(ws, "shop")["status"] == "running"
    SG.unstage(ws, "shop")
    assert not aws.endpoints and not aws.models
    with pytest.raises(ValueError, match="cloud compute target"):
        SG.stage(ws, "shop", image=theirs, target="local")


def test_staging_needs_a_cloud_workspace_on_a_cloud_target(ws, aws):  # noqa: F811
    ws.workspace.storage_uri = "/tmp/on-disk"
    with pytest.raises(ValueError, match="kept on this computer, and a cloud endpoint can't read it from there. Set up serving"):
        SG.stage(ws, "shop", target="sm", confirm=True)
    assert not [c for c, _ in aws.calls if c != "simulate_principal_policy"]


def test_a_failed_cloud_restage_keeps_the_old_endpoint_and_removes_only_the_new_parts(ws, aws):  # noqa: F811
    a = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    aws.fail_on = "update_endpoint"
    with pytest.raises(ProviderError, match="not authorized"):
        SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    aws.fail_on = None
    rec = SG.state(ws, "shop")
    assert rec["status"] == "running" and rec["model_version"] == ws.baseline and "is still running" in rec["error"]
    assert {r["name"] for r in rec["resources"]} == {r["name"] for r in a["resources"]}
    assert len(aws.models) == 1 and len(aws.configs) == 1 and len(aws.endpoints) == 1  # the new model and config are gone
    assert SG.query(ws, "shop", "jacket")["staged"]["model_version"] == ws.baseline  # and its key still works
    SG.unstage(ws, "shop")
    assert not aws.models and not aws.configs and not aws.endpoints


def test_a_role_that_cannot_stop_its_own_endpoint_is_warned_about_before_the_confirm(ws, aws):  # noqa: F811
    aws.role_denies = {"sagemaker:DeleteEndpoint"}
    p = SG.plan(ws, "shop", target="sm")
    warn = "This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."
    assert p["preflight"]["checked"] and p["preflight"]["can_delete"] is False and p["preflight"]["can_write"] is True
    assert p["warnings"] == [warn] and p["text"].endswith(warn)
    asked = {c[1]["action"]: c[1]["resource"] for c in aws.calls}
    assert asked["sagemaker:DeleteEndpoint"].endswith(":endpoint/captain-search-test-shop-staging")
    assert asked["s3:PutObject"] == "arn:aws:s3:::captain-test/cst/staging/preflight"
    with pytest.raises(SG.NeedsConfirm, match="can't stop itself when idle") as e:
        SG.stage(ws, "shop", target="sm")
    assert e.value.plan["warnings"] == [warn] and not aws.endpoints
    # the bucket: without it there is no log, and that is said too
    aws.role_denies = {"s3:PutObject"}
    assert "can't write the workspace bucket" in SG.plan(ws, "shop", target="sm")["warnings"][0]
    # when IAM won't answer, Captain says it couldn't check, and does not guess either way
    aws.role_denies, aws.iam_refuses = set(), True
    p = SG.plan(ws, "shop", target="sm")
    assert p["preflight"]["checked"] is False and p["preflight"]["can_delete"] is None and p["warnings"] == []
    assert "couldn't check" in p["preflight"]["note"] and "keeps billing until you stop it from Captain" in p["preflight"]["note"]
    assert SG.plan(ws, "shop", target="local")["preflight"] is None  # nothing to check on this computer


def test_a_workspace_on_this_computer_sends_only_the_bundle_and_the_log_to_the_serving_bucket(ws, aws, tmp_path, monkeypatch):  # noqa: F811
    """Staged in the cloud from a workspace kept here: the endpoint reads a serving bucket that holds the bundle,
    the control document and the request log, and nothing else of the workspace."""
    from types import SimpleNamespace

    from captain_search.staging import consistency as SC
    from captain_search.staging import logs as L
    from captain_search.storage import backend as SB

    ws.workspace.storage_uri = str(tmp_path / "store")  # on this computer
    bucket = SB.LocalStore(str(tmp_path / "serving-bucket"))
    monkeypatch.setattr(SB, "open_store", lambda uri, profile=None: bucket if uri == "s3://captain-serving-test/t" else (_ for _ in ()).throw(AssertionError(uri)))
    L._stores.clear()
    C.add(ws, "sm", "aws-sagemaker", "p", "us-west-1", {"image": IMAGE, "execution_role_arn": ROLE, "instance_type": "ml.t2.medium",
                                                       "name_prefix": "captain-search-test", "serving_storage": "s3://captain-serving-test/t"}, replace=True)
    aws.ctx = SimpleNamespace(store=bucket, registry=ws.registry, workspace=ws.workspace)  # what the endpoint can see
    r = SG.stage(ws, "shop", target="sm", confirm=True)
    assert r["status"] == "running" and r["serving_storage"] == "s3://captain-serving-test/t"
    env = next(iter(aws.models.values()))["PrimaryContainer"]["Environment"]
    assert env["CST_STORAGE"] == "s3://captain-serving-test/t"  # the endpoint is pointed at the serving bucket
    asked = {c[1]["action"]: c[1]["resource"] for c in aws.calls if c[0] == "simulate_principal_policy"}
    assert asked["s3:PutObject"] == "arn:aws:s3:::captain-serving-test/t/staging/preflight"
    keys = bucket.list("")
    assert "workspace.yaml" in keys and any(k.endswith("/bundle.json") for k in keys) and any("/catalogs/" in k for k in keys)
    assert all(k == "workspace.yaml" or k.startswith("staging/") for k in keys), f"only the bundle, the control and the log: {keys}"
    assert not any(k.startswith("staging/") for k in ws.store.list("staging/shop/bundles")), "and the bundle is not also written here"
    # it serves, logs to the bucket, and Captain reads the log back from there
    assert len(SG.query(ws, "shop", "jacket", k=4)["staged"]["results"]) == 4
    assert SC.check(ws, "shop")["status"] == "pass"
    assert any("/logs/" in k for k in bucket.list("staging/")) and SG.control(ws, "shop")["serve"] == "candidate"
    assert all(k == "workspace.yaml" or k.startswith("staging/") for k in bucket.list(""))
    SG.unstage(ws, "shop")
    assert not aws.endpoints and not aws.models
    # the published log dataset lands in the workspace here, not in the bucket
    assert ws.registry.versions("dataset", "shop/staging-logs") and not bucket.list("datasets/")
