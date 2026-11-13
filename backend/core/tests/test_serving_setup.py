"""Setting up serving in the person's own AWS account, against a fake AWS: what is created, how it is named and
tagged, that the role is least privilege, that a missing permission says which one and hands over the policy, that
the image needs no local Docker, and that teardown leaves nothing behind."""
import io
import json
import zipfile

import pytest
import yaml
from typer.testing import CliRunner

from captain_search import compute as C
from captain_search import settings as ST
from captain_search.cli.main import app
from captain_search.staging import serving_setup as SS
from captain_search.workspace import init_workspace

ACC = "000000000000"


class Denied(Exception):
    def __init__(self, action):
        super().__init__(action)
        self.response = {"Error": {"Code": "AccessDenied", "Message": f"User is not authorized to perform: {action}"}}


class Missing(Exception):
    def __init__(self, what="x"):
        super().__init__(what)
        self.response = {"Error": {"Code": "NoSuchEntity", "Message": f"{what} cannot be found"}}


class Fake:
    """One object standing in for every AWS client. `deny` holds the actions the login lacks."""

    def __init__(self):
        self.roles, self.policies, self.repos, self.images, self.projects, self.buckets, self.objects = {}, {}, {}, {}, {}, {}, {}
        self.builds, self.deny, self.calls, self.build_result = [], set(), [], "SUCCEEDED"

    def client(self, name):
        return self

    def _do(self, action, **kw):
        self.calls.append((action, kw))
        if action in self.deny:
            raise Denied(action)

    def get_caller_identity(self):
        return {"Account": ACC}

    # iam
    def get_role(self, RoleName):
        self._do("iam:GetRole")
        if RoleName not in self.roles:
            raise Missing(RoleName)
        return {"Role": self.roles[RoleName]}

    def create_role(self, **kw):
        self._do("iam:CreateRole", **kw)
        self.roles[kw["RoleName"]] = kw

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self._do("iam:PutRolePolicy")
        self.policies[RoleName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        self.policies.pop(RoleName, None)

    def delete_role(self, RoleName):
        self._do("iam:DeleteRole")
        if RoleName not in self.roles:
            raise Missing(RoleName)
        del self.roles[RoleName]

    # ecr
    def describe_repositories(self, repositoryNames):
        self._do("ecr:DescribeRepositories")
        if repositoryNames[0] not in self.repos:
            raise Missing("repository")
        return {"repositories": [{}]}

    def create_repository(self, repositoryName, tags):
        self._do("ecr:CreateRepository")
        self.repos[repositoryName] = tags

    def describe_images(self, repositoryName, imageIds):
        if imageIds[0]["imageTag"] not in self.images.get(repositoryName, set()):
            raise Missing("image")
        return {"imageDetails": [{}]}

    def delete_repository(self, repositoryName, force):
        self._do("ecr:DeleteRepository")
        self.repos.pop(repositoryName)
        self.images.pop(repositoryName, None)

    # s3
    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise Missing("bucket")

    def create_bucket(self, Bucket, **kw):
        self._do("s3:CreateBucket", **kw)
        self.buckets[Bucket] = kw

    def put_bucket_tagging(self, Bucket, Tagging):
        self.buckets[Bucket]["tags"] = Tagging["TagSet"]

    def put_object(self, Bucket, Key, Body):
        self._do("s3:PutObject")
        self.objects[f"{Bucket}/{Key}"] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(f"{Bucket}/{Key}", None)

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        return {"Contents": [{"Key": k.split("/", 1)[1]} for k in list(self.objects) if k.startswith(f"{Bucket}/{Prefix}")]}

    def delete_bucket(self, Bucket):
        del self.buckets[Bucket]

    # codebuild
    def create_project(self, **kw):
        self._do("codebuild:CreateProject")
        self.projects[kw["name"]] = kw

    def start_build(self, projectName):
        self._do("codebuild:StartBuild")
        self.builds.append(projectName)
        return {"build": {"id": f"{projectName}:1"}}

    def batch_get_builds(self, ids):
        spec = self.projects[ids[0].split(":")[0]]
        if self.build_result == "SUCCEEDED":  # the build pushes the image its buildspec names
            image = yaml.safe_load(spec["source"]["buildspec"])["phases"]["post_build"]["commands"][0].split()[-1]
            repo, tag = image.split("/", 1)[1].split(":")
            self.images.setdefault(repo, set()).add(tag)
            return {"builds": [{"buildStatus": "SUCCEEDED"}]}
        return {"builds": [{"buildStatus": "FAILED", "phases": [{"phaseType": "BUILD", "phaseStatus": "FAILED", "contexts": [{"message": "pip could not resolve a package"}]}]}]}

    def delete_log_group(self, logGroupName):
        self._do("logs:DeleteLogGroup")
        self.calls.append(("deleted-log-group", logGroupName))

    def delete_project(self, name):
        self._do("codebuild:DeleteProject")
        del self.projects[name]


@pytest.fixture
def aws(monkeypatch):
    from captain_search import cloud

    f = Fake()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: f)
    monkeypatch.setattr(SS, "docker_answers", lambda timeout=5.0: False)  # no Docker on this computer
    return f


@pytest.fixture
def cloud_ws(tmp_path):
    c = init_workspace("shop", str(tmp_path / "bucket"))
    c.workspace.storage_uri = "s3://team-bucket/captain/shop"
    return c


def test_the_plan_says_what_will_be_created_and_what_it_costs_and_changes_nothing(cloud_ws, aws):
    p = SS.plan(cloud_ws, "p", "us-west-1")
    assert p["image_source"] == "codebuild"  # Docker isn't answering, so the image is built in the account
    assert [(r["resource"], r["name"]) for r in p["creates"]] == [
        ("IAM role", "captain-serving"), ("ECR repository", "captain-serving"), ("IAM role", "captain-serving-build"),
        ("CodeBuild project", "captain-serving-build"), ("Captain compute target", "aws-serving")]
    assert p["cost"]["setup_usd"] == 0.01 and "Building the image takes about a minute and a half in AWS CodeBuild" in p["cost"]["text"] and "bills by the hour only while it is up" in p["cost"]["text"]
    assert not aws.roles and not aws.repos and SS.read(cloud_ws) is None and SS.status(cloud_ws)["status"] == "none"
    with pytest.raises(ValueError, match="Confirm first"):
        SS.setup(cloud_ws, "p", "us-west-1")
    assert not aws.roles
    # the login's own policy, for an admin: only Captain's own names
    text = json.dumps(p["login_policy"])
    assert f"arn:aws:iam::{ACC}:role/captain-serving" in text and "codebuild:StartBuild" in text and "iam:PassRole" in text
    assert '"Resource": "*"' in text  # SageMaker endpoints only: everything else is named
    # Two statements may name every resource: SageMaker's endpoints, and the scaling service, whose targets and alarms
    # get names AWS makes up. Everything else names exactly what setup makes.
    assert all(s["Resource"] != "*" for s in p["login_policy"]["Statement"] if s["Sid"] not in ("StagingEndpoints", "MoreThanOneCopy", "ReadItsNumbers"))
    reads = next(s for s in p["login_policy"]["Statement"] if s["Sid"] == "ReadItsNumbers")
    assert reads["Action"] == ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]  # read-only, and nothing else
    scaling = next(s for s in p["login_policy"]["Statement"] if s["Sid"] == "ScalingServiceRole")
    assert scaling["Action"] == "iam:CreateServiceLinkedRole" and "sagemaker.application-autoscaling.amazonaws.com" in scaling["Resource"]
    assert scaling["Condition"] == {"StringLike": {"iam:AWSServiceName": "sagemaker.application-autoscaling.amazonaws.com"}}


def test_setup_creates_a_least_privilege_role_an_image_built_in_the_account_and_a_target(cloud_ws, aws):
    said = []
    r = SS.setup(cloud_ws, "p", "us-west-1", name_prefix="captain-search-test", instance_type="ml.t2.medium", confirm=True, progress=said.append, poll_s=0)
    assert r["status"] == "ready" and r["target"] == "aws-serving" and r["image_source"] == "codebuild"
    assert [(x["type"], x["name"]) for x in r["created"]][:3] == [("iam-role", "captain-search-test-serving"), ("ecr-repository", "captain-search-test-serving"),
                                                                  ("iam-role", "captain-search-test-serving-build")]
    assert any("Building the serving image in your account" in s for s in said)
    # every AWS resource is tagged for Captain
    for role in aws.roles.values():
        assert {t["Key"]: t["Value"] for t in role["Tags"]}["app"] == "captain"
    assert {t["Key"]: t["Value"] for t in aws.repos["captain-search-test-serving"]}["app"] == "captain"
    assert {t["key"]: t["value"] for t in aws.projects["captain-search-test-serving-build"]["tags"]}["app"] == "captain"
    # the endpoint's role: read the bundle, write the log, delete its own endpoint, pull the image. Nothing wider.
    pol = {s["Sid"]: s for s in aws.policies["captain-search-test-serving"]["Statement"]}
    assert pol["ReadTheBundle"]["Resource"] == ["arn:aws:s3:::team-bucket/captain/shop/workspace.yaml", "arn:aws:s3:::team-bucket/captain/shop/staging/*"]
    assert pol["WriteTheRequestLog"] == {"Sid": "WriteTheRequestLog", "Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::team-bucket/captain/shop/staging/*"}
    assert pol["StopItself"]["Action"] == "sagemaker:DeleteEndpoint" and pol["StopItself"]["Resource"].endswith(":endpoint/captain-search-test-*")
    assert pol["ListItsFolder"]["Condition"] == {"StringLike": {"s3:prefix": ["captain/shop", "captain/shop/*"]}}
    assert not any("s3:DeleteObject" in json.dumps(s) or s["Resource"] == "arn:aws:s3:::team-bucket/*" for s in pol.values())
    assert aws.roles["captain-search-test-serving"]["AssumeRolePolicyDocument"].count("sagemaker.amazonaws.com") == 1
    # the build: a folder made from the installed package, no checkout and no local Docker
    proj = aws.projects["captain-search-test-serving-build"]
    assert proj["environment"]["privilegedMode"] and proj["source"]["type"] == "S3"
    folder = zipfile.ZipFile(io.BytesIO(aws.objects["team-bucket/" + proj["source"]["location"].split("/", 1)[1]]))
    names = set(folder.namelist())
    assert {"Dockerfile", "serve.sh", "requirements.txt", "captain_search/__init__.py", "captain_search/staging/runtime.py"} <= names
    assert not any(n.endswith(".pyc") for n in names) and "fastapi" in folder.read("requirements.txt").decode()
    assert "projects staging-serve" in folder.read("serve.sh").decode()
    spec = yaml.safe_load(proj["source"]["buildspec"])
    assert spec["phases"]["build"]["commands"] == [f"docker build --platform linux/amd64 -t {r['image']} ."]
    assert r["image"].startswith(f"{ACC}.dkr.ecr.us-west-1.amazonaws.com/captain-search-test-serving:")
    # the target staging uses
    t = C.get(cloud_ws, "aws-serving")
    assert t["kind"] == "aws-sagemaker" and t["config"]["image"] == r["image"] and t["config"]["name_prefix"] == "captain-search-test"
    assert t["config"]["execution_role_arn"] == f"arn:aws:iam::{ACC}:role/captain-search-test-serving" and not t["config"].get("serving_storage")
    assert "Serving is set up" in SS.status(cloud_ws)["text"]
    # running it again builds nothing new and keeps the list
    n = len(aws.builds)
    again = SS.setup(cloud_ws, "p", "us-west-1", name_prefix="captain-search-test", instance_type="ml.t2.medium", confirm=True, poll_s=0)
    assert len(aws.builds) == n and again["build_seconds"] is None and len(again["created"]) == len(r["created"])


@pytest.mark.parametrize("action,step", [("iam:CreateRole", "create the role captain-serving"), ("ecr:CreateRepository", "create the image repository captain-serving"),
                                         ("codebuild:CreateProject", "create the image build captain-serving-build"), ("codebuild:StartBuild", "start the image build")])
def test_a_missing_permission_names_the_step_and_hands_over_the_policy(cloud_ws, aws, action, step):
    aws.deny = {action}
    with pytest.raises(SS.MissingPermission) as e:
        SS.setup(cloud_ws, "p", "us-west-1", confirm=True, poll_s=0)
    assert str(e.value) == f"Your AWS login can't {step}: it lacks {action}. Ask an admin to grant the policy below, then try again."
    assert e.value.policy == SS.plan(cloud_ws, "p", "us-west-1")["login_policy"] and action.split(":")[1] in json.dumps(e.value.policy)
    rec = SS.read(cloud_ws)
    assert rec["status"] == "needs permission"
    # what was made before the refusal is listed, so it can be removed or the setup finished later
    aws.deny = set()
    assert SS.setup(cloud_ws, "p", "us-west-1", confirm=True, poll_s=0)["status"] == "ready"
    SS.teardown(cloud_ws)
    assert not aws.roles and not aws.repos and not aws.projects


def test_a_failed_image_build_says_why(cloud_ws, aws):
    aws.build_result = "FAILED"
    with pytest.raises(ValueError, match=r"The image build did not finish \(BUILD\): pip could not resolve a package"):
        SS.setup(cloud_ws, "p", "us-west-1", confirm=True, poll_s=0)
    assert SS.read(cloud_ws)["status"] == "image build failed"


def test_a_workspace_on_this_computer_gets_a_bucket_for_the_bundle_only(tmp_path, aws):
    c = init_workspace("shop", str(tmp_path / "local"))
    p = SS.plan(c, "p", "us-west-1")
    row = next(r for r in p["creates"] if r["resource"] == "S3 bucket")
    assert row["name"] == f"captain-serving-{ACC}-us-west-1" and "stays on this computer" in row["why"] and "Only the serving bundle" in row["why"]
    r = SS.setup(c, "p", "us-west-1", confirm=True, poll_s=0)
    assert f"captain-serving-{ACC}-us-west-1" in aws.buckets and aws.buckets[f"captain-serving-{ACC}-us-west-1"]["CreateBucketConfiguration"] == {"LocationConstraint": "us-west-1"}
    assert C.get(c, "aws-serving")["config"]["serving_storage"] == f"s3://captain-serving-{ACC}-us-west-1/shop"
    assert aws.policies["captain-serving"]["Statement"][0]["Resource"][1] == f"arn:aws:s3:::captain-serving-{ACC}-us-west-1/shop/staging/*"
    assert SS.plan(c, "p", "us-west-1", bucket="our-own-bucket")["storage"] == "s3://our-own-bucket/shop"  # or one the person picks
    assert ("s3-bucket", f"captain-serving-{ACC}-us-west-1") in [(x["type"], x["name"]) for x in r["created"]]
    # Removing the setup empties the serving folder (bundle, catalog, log, the workspace file) and nothing beside it.
    b = f"captain-serving-{ACC}-us-west-1"
    for k in ("shop/staging/demo/bundles/x/bundle.json", "shop/staging/demo/staging/logs/x/requests-1.jsonl", "shop/workspace.yaml"):
        aws.objects[f"{b}/{k}"] = b"x"
    aws.objects[f"{b}/someone-elses/file.txt"] = b"theirs"
    out = SS.teardown(c)
    assert not [k for k in aws.objects if k.startswith(f"{b}/shop/")], "the serving folder is empty"
    assert aws.objects[f"{b}/someone-elses/file.txt"] == b"theirs" and b in aws.buckets  # not Captain's: kept, and so is the bucket
    assert out["emptied"] >= 3 and "it still holds 1 object" in out["text"]


def test_the_public_image_path_exists_and_stays_off_until_one_is_published(cloud_ws, aws):
    with pytest.raises(ValueError, match="no published Captain serving image yet"):
        SS.plan(cloud_ws, "p", "us-west-1", source="public")
    ST.set_serving_image(cloud_ws, "public.ecr.aws/example/captain-serving:1")
    r = SS.setup(cloud_ws, "p", "us-west-1", source="public", confirm=True, poll_s=0)
    spec = yaml.safe_load(aws.projects["captain-serving-build"]["source"]["buildspec"])
    assert spec["phases"]["build"]["commands"] == [f"docker pull public.ecr.aws/example/captain-serving:1 && docker tag public.ecr.aws/example/captain-serving:1 {r['image']}"]


def test_docker_is_used_when_it_answers_and_a_hung_one_counts_as_no(cloud_ws, aws, monkeypatch):
    import subprocess

    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("docker", 5)))
    assert SS.docker_answers() is False  # a Docker that hangs is not a Docker
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"27.0", b""))
    assert SS.docker_answers() is True


def test_teardown_removes_everything_and_refuses_while_a_model_is_staged(cloud_ws, aws):
    from captain_search import project_state as PS
    from captain_search import projects as PR

    SS.setup(cloud_ws, "p", "us-west-1", confirm=True, poll_s=0)
    PR.create(cloud_ws, "shop")
    PS._update(cloud_ws, "shop", lambda d: d.__setitem__("staging", {"staging": {"status": "running", "target": "aws-serving"}}))
    with pytest.raises(ValueError, match="has a model deployed here. Stop it first"):
        SS.teardown(cloud_ws)
    PS._update(cloud_ws, "shop", lambda d: d.__setitem__("staging", {"staging": {"status": "stopped", "target": "aws-serving"}}))
    said = []
    r = SS.teardown(cloud_ws, progress=said.append)
    assert r["status"] == "removed" and r["created"] == [] and r["text"] == "Serving setup is removed. Nothing of Captain's is left in the account."
    assert not aws.roles and not aws.policies and not aws.repos and not aws.projects and not aws.objects
    assert ("deleted-log-group", "/aws/codebuild/captain-serving-build") in aws.calls  # the build's log goes too
    assert "serve" not in C.get(cloud_ws, "aws-serving")["purposes"]  # the target can no longer be staged on
    assert SS.teardown(cloud_ws)["status"] == "removed"  # twice is fine
    # what can't be removed is named
    SS.setup(cloud_ws, "p", "us-west-1", confirm=True, poll_s=0)
    aws.deny = {"iam:DeleteRole"}
    left = SS.teardown(cloud_ws)
    assert left["status"] == "partly removed" and {x["name"] for x in left["created"]} == {"captain-serving", "captain-serving-build"}
    assert "not authorized to perform: iam:DeleteRole" in left["text"]


def test_cli_shows_the_plan_and_the_policy(cloud_ws, aws):
    run = CliRunner()
    dry = run.invoke(app, ["--dry-run", "compute", "setup", "aws-serving", "--profile", "p", "--region", "us-west-1"])
    assert dry.exit_code == 0 and "Would create:" in dry.output and "captain-serving-build" in dry.output and not aws.roles, dry.output
    pol = run.invoke(app, ["compute", "setup", "aws-serving", "--profile", "p", "--region", "us-west-1", "--policy"])
    assert pol.exit_code == 0 and json.loads(pol.output)["Statement"][0]["Sid"] == "ServingRoles"
    aws.deny = {"ecr:CreateRepository"}
    out = run.invoke(app, ["compute", "setup", "aws-serving", "--profile", "p", "--region", "us-west-1", "--yes"])
    assert out.exit_code == 1 and "it lacks ecr:CreateRepository" in out.output and '"Sid": "ServingImage"' in out.output


def test_the_serving_bucket_is_a_name_or_a_full_address():
    from captain_search.staging import serving_setup as SS

    assert SS._serving_uri(None, "000000000000", "us-west-1", "shop") == "s3://captain-serving-000000000000-us-west-1/shop"
    assert SS._serving_uri("my-bucket", None, "us-west-1", "shop") == "s3://my-bucket/shop"
    assert SS._serving_uri("s3://my-bucket", None, "us-west-1", "shop") == "s3://my-bucket/shop"
    assert SS._serving_uri("s3://my-bucket/a/folder/", None, "us-west-1", "shop") == "s3://my-bucket/a/folder"
    assert SS._where(SS._serving_uri("s3://my-bucket/a/folder", None, "us-west-1", "shop")) == ("my-bucket", "a/folder")
    import pytest

    with pytest.raises(ValueError, match="a bucket name, or an address like s3://"):
        SS._serving_uri("gs://other", None, "us-west-1", "shop")
