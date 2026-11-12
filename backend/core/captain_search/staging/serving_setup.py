"""Set up serving in the person's own AWS account, and take it away again.

A SageMaker staging target needs three things that used to be made by hand: a role the endpoint runs as, a place
in the account's container registry holding the serving image, and a compute target in Captain that names both.
`plan` says exactly what would be created and what it costs. `setup` creates it with the connected login after a
confirm. `teardown` removes exactly what `setup` made. Everything is named `<prefix>-serving...`, tagged
app=captain, and written to `serving/setup.yaml` in the workspace, so it is listed and removable.

The role is least privilege:
  read    the workspace file and the serving bundles (`<prefix>/workspace.yaml`, `<prefix>/staging/*`)
  write   the request log and the stop marker (`<prefix>/staging/*`), nothing else in the bucket
  delete  its own endpoint, so an idle endpoint can stop itself
  pull    the serving image, and write its own log lines

The image needs no Docker on the person's computer. Where it comes from:
  docker     this computer's Docker builds and pushes it, when Docker answers
  codebuild  AWS CodeBuild builds it inside the person's account from a small build folder Captain uploads
             (the installed captain_search package and a Dockerfile). A few minutes, a few cents.
  public     a published Captain serving image is copied into the account. NOT available: no image is published.
             The path exists and stays off until `serving.public_image` is set in the workspace settings.

A login that lacks a permission gets one sentence naming the step and the action, and the exact policy to hand to
an admin (`login_policy`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import time
import zipfile

import yaml

from ..storage.backend import NotFound
from ..storage.registry import dump_yaml
from ..workspace import Context, whoami

KEY = "serving/setup.yaml"
TAGS = {"app": "captain", "captain:purpose": "serving"}
TRUST = lambda svc: {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": svc}, "Action": "sts:AssumeRole"}]}  # noqa: E731
# general1.small on Linux, list price. Measured 2026-11-11 in us-west-1: the build itself took 78 seconds, and the
# whole setup 94. The estimate stays at a cautious 3 minutes: a cold registry or a slow mirror takes longer.
BUILD_MINUTES, BUILD_USD_PER_MIN = 3, 0.005
DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY captain_search /app/captain_search
COPY requirements.txt /app/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/* \\
 && pip install --no-cache-dir -r /app/requirements.txt && useradd --create-home --uid 10001 cst
USER cst
ENV CST_HOME=/home/cst/.cst PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8080
COPY serve.sh /usr/local/bin/serve
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/serve"]
"""
SERVE_SH = """#!/bin/sh
# SageMaker starts a serving container with the one argument `serve`. Anything else is a cst command.
set -e
CST="python -c 'import sys; from captain_search.cli.main import app; sys.argv[0] = \\"cst\\"; app()'"
if [ "$1" = "serve" ] && [ -n "$CST_STAGING_BUNDLE" ]; then
  exec sh -c "$CST ${CST_WORKSPACE:+-w \\"$CST_WORKSPACE\\"} projects staging-serve \\"$CST_STAGING_BUNDLE\\" --host 0.0.0.0 --port 8080"
fi
exec sh -c "$CST $*"
"""


class MissingPermission(ValueError):
    """The connected login may not do one step. `.policy` is the exact policy an admin can grant."""

    def __init__(self, step: str, action: str, policy: dict):
        super().__init__(f"Your AWS login can't {step}: it lacks {action}. Ask an admin to grant the policy below, then try again.")
        self.step, self.action, self.policy = step, action, policy


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def names(prefix: str = "captain") -> dict:
    p = prefix.strip("-") or "captain"
    return {"role": f"{p}-serving", "repo": f"{p}-serving", "build_role": f"{p}-serving-build", "build": f"{p}-serving-build",
            "target": "aws-serving", "endpoints": p}


def _serving_uri(bucket: str | None, account: str | None, region: str, workspace: str) -> str:
    """Where a workspace kept on this computer puts its serving bundle and request log. `bucket` is a bucket name
    (a folder named after the workspace is used inside it) or a full s3:// address with the folder to use."""
    if not bucket:
        return f"s3://captain-serving-{account}-{region}/{workspace}"
    b = bucket.strip().rstrip("/")
    if b.startswith("s3://"):
        return b if "/" in b[5:] else f"{b}/{workspace}"
    if "://" in b or not b:
        raise ValueError("the bucket is a bucket name, or an address like s3://my-bucket/a-folder")
    return f"s3://{b}/{workspace}"


def _where(storage_uri: str) -> tuple[str, str]:
    bucket, _, prefix = storage_uri.split("://", 1)[-1].partition("/")
    return bucket, prefix.strip("/")


def role_policy(storage_uri: str, region: str, account: str, n: dict) -> dict:
    """What the endpoint may do, and nothing more."""
    bucket, p = _where(storage_uri)
    base = f"arn:aws:s3:::{bucket}/{p + '/' if p else ''}"
    lst: dict = {"Sid": "ListItsFolder", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"}
    if p:
        lst["Condition"] = {"StringLike": {"s3:prefix": [p, f"{p}/*"]}}
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadTheBundle", "Effect": "Allow", "Action": "s3:GetObject", "Resource": [base + "workspace.yaml", base + "staging/*"]},
        {"Sid": "WriteTheRequestLog", "Effect": "Allow", "Action": "s3:PutObject", "Resource": base + "staging/*"},
        lst,
        {"Sid": "StopItself", "Effect": "Allow", "Action": "sagemaker:DeleteEndpoint",
         "Resource": f"arn:aws:sagemaker:{region}:{account}:endpoint/{n['endpoints']}-*"},
        {"Sid": "PullTheImage", "Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{n['repo']}"},
        {"Sid": "RegistryLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "ItsOwnLogs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/*"}]}


def build_policy(storage_uri: str, region: str, account: str, n: dict) -> dict:
    """What the image build may do: read the build folder, push to the one repository, write its log."""
    bucket, p = _where(storage_uri)
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{bucket}/{p + '/' if p else ''}serving/build/*"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{n['repo']}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{n['build']}*"}]}


def login_policy(storage_uri: str, region: str, account: str, n: dict, image_source: str = "codebuild") -> dict:
    """The exact policy the person's own login needs for setup, staging and teardown. For an admin to grant."""
    bucket, _ = _where(storage_uri)
    roles = [f"arn:aws:iam::{account}:role/{n['role']}"] + ([f"arn:aws:iam::{account}:role/{n['build_role']}"] if image_source != "docker" else [])
    st = [
        {"Sid": "ServingRoles", "Effect": "Allow", "Action": ["iam:CreateRole", "iam:GetRole", "iam:TagRole", "iam:ListRoleTags", "iam:PutRolePolicy",
                                                               "iam:DeleteRolePolicy", "iam:DeleteRole", "iam:PassRole", "iam:SimulatePrincipalPolicy"],
         "Resource": roles},
        {"Sid": "ServingImage", "Effect": "Allow", "Action": ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:TagResource",
                                                               "ecr:DeleteRepository", "ecr:BatchDeleteImage"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{n['repo']}"},
        {"Sid": "ServingBucket", "Effect": "Allow", "Action": ["s3:CreateBucket", "s3:PutBucketTagging", "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
        {"Sid": "StagingEndpoints", "Effect": "Allow", "Action": ["sagemaker:CreateModel", "sagemaker:CreateEndpointConfig", "sagemaker:CreateEndpoint",
                                                                   "sagemaker:UpdateEndpoint", "sagemaker:DescribeEndpoint", "sagemaker:DeleteEndpoint",
                                                                   "sagemaker:DeleteEndpointConfig", "sagemaker:DeleteModel", "sagemaker:InvokeEndpoint",
                                                                   "sagemaker:AddTags", "sagemaker:ListTags", "sagemaker:ListEndpoints",
                                                                   "sagemaker:ListEndpointConfigs", "sagemaker:ListModels"],
         "Resource": "*"}]
    if image_source != "docker":
        st.append({"Sid": "ImageBuild", "Effect": "Allow", "Action": ["codebuild:CreateProject", "codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:DeleteProject"],
                   "Resource": f"arn:aws:codebuild:{region}:{account}:project/{n['build']}"})
    else:
        st.append({"Sid": "ImagePush", "Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"})
        st[1]["Action"] += ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"]
    st.append({"Sid": "TheirLogs", "Effect": "Allow", "Action": "logs:DeleteLogGroup",
               "Resource": [f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{n['build']}*",
                            f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/Endpoints/{n['endpoints']}-*"]})
    return {"Version": "2012-10-17", "Statement": st}


def docker_answers(timeout: float = 5.0) -> bool:
    """Is a Docker on this computer up right now? A hung one counts as no."""
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_source(ctx: Context, want: str = "auto") -> str:
    """Which way the image gets into the account. `public` only when a published image is set, which none is."""
    from .. import settings as ST

    if want == "public":
        if not ((ST.get(ctx).get("serving") or {}).get("public_image")):
            raise ValueError("There is no published Captain serving image yet, so it can't be copied into your account. "
                             "Build it in your account instead (the default).")
        return "public"
    if want in ("docker", "codebuild"):
        return want
    return "docker" if docker_answers() else "codebuild"


# What each host's image needs beside the base install: the library for its storage and its sign-in.
FLAVOR_EXTRAS = {"aws": (), "modal": (), "gcp": ("gcs",), "azure": ("azure",)}


def build_folder(flavor: str = "aws") -> tuple[bytes, str]:
    """The zip a cloud's own builder (or Docker) builds the image from: the installed captain_search package, its
    requirements, a Dockerfile and the entry script. No repository checkout is needed, so the installed app can do
    this. `flavor` adds what one host needs: the storage library for Google Cloud or Azure. On Modal the image
    runs as root, because Modal mounts its volume owned by root."""
    import captain_search
    from importlib.metadata import PackageNotFoundError, requires

    root = os.path.dirname(os.path.abspath(captain_search.__file__))
    extras = FLAVOR_EXTRAS[flavor]
    try:
        every = requires("captain-search") or []
        reqs = [r.split(";")[0].strip() for r in every if "extra ==" not in r]
        reqs += [r.split(";")[0].strip() for r in every if any(f'extra == "{x}"' in r or f"extra == '{x}'" in r for x in extras)]
    except PackageNotFoundError:
        reqs = []
    reqs = sorted(set(reqs) | {"boto3>=1.35"})
    dockerfile = DOCKERFILE.replace("USER cst\n", "") if flavor == "modal" else DOCKERFILE
    digest = hashlib.sha256()  # of the contents, so the same Captain gives the same image tag (a zip's own bytes carry times)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in sorted(files):
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(base, f)
                rel = os.path.join("captain_search", os.path.relpath(full, root))
                z.write(full, rel)
                with open(full, "rb") as fh:
                    digest.update(rel.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
        z.writestr("requirements.txt", "\n".join(reqs) + "\n")
        z.writestr("Dockerfile", dockerfile)
        info = zipfile.ZipInfo("serve.sh")
        info.external_attr = 0o755 << 16
        z.writestr(info, SERVE_SH)
    digest.update(("\n".join(reqs) + dockerfile + SERVE_SH).encode())
    return buf.getvalue(), digest.hexdigest()


def build_folder_dir(path: str, flavor: str = "aws") -> str:
    """The build folder written out under `path`, for a builder that reads a folder. Returns its digest."""
    folder, digest = build_folder(flavor)
    zipfile.ZipFile(io.BytesIO(folder)).extractall(path)
    os.chmod(os.path.join(path, "serve.sh"), 0o755)
    return digest


def _buildspec(image: str, registry: str, region: str, public: str | None) -> str:
    make = f"docker pull {public} && docker tag {public} {image}" if public else f"docker build --platform linux/amd64 -t {image} ."
    return yaml.safe_dump({"version": 0.2, "phases": {
        "pre_build": {"commands": [f"aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {registry}"]},
        "build": {"commands": [make]}, "post_build": {"commands": [f"docker push {image}"]}}}, sort_keys=False)


def read(ctx: Context) -> dict | None:
    try:
        return yaml.safe_load(ctx.store.get(KEY))
    except NotFound:
        return None


def _write(ctx: Context, rec: dict) -> None:
    try:
        _, tag = ctx.store.get_with_tag(KEY)
    except NotFound:
        tag = None
    ctx.store.put_if(KEY, dump_yaml(rec), tag)


def plan(ctx: Context, profile: str, region: str, *, name_prefix: str = "captain", instance_type: str = "ml.m5.large",
         source: str = "auto", bucket: str | None = None, account: str | None = None) -> dict:
    """What setup would create, in plain rows, what it costs, and the policy the login needs. Changes nothing."""
    from .. import cloud

    n = names(name_prefix)
    src = image_source(ctx, source)
    if account is None:
        account = cloud.aws_session(profile, region).client("sts").get_caller_identity()["Account"]
    local = not ctx.workspace.storage_uri.startswith("s3://")
    storage = ctx.workspace.storage_uri if not local else _serving_uri(bucket, account, region, ctx.workspace.name)
    rows = [
        {"resource": "IAM role", "name": n["role"], "why": "The endpoint runs as this. It can read the serving bundle, write the request log, "
                                                           "pull the serving image and delete its own endpoint. Nothing else."},
        {"resource": "ECR repository", "name": n["repo"], "why": "Holds Captain's serving image in your account."}]
    if src != "docker":
        rows += [{"resource": "IAM role", "name": n["build_role"], "why": "The image build runs as this. It can read the build folder and push to that one repository."},
                 {"resource": "CodeBuild project", "name": n["build"], "why": "Builds the serving image inside your account. It runs once, for about a minute and a half."}]
    if local:
        rows.append({"resource": "S3 bucket", "name": _where(storage)[0],
                     "why": "Your workspace's data stays on this computer. Only the serving bundle (the model and its catalog) and the "
                            "request log go to this bucket."})
    rows.append({"resource": "Captain compute target", "name": n["target"], "why": f"SageMaker endpoint, {instance_type}, in {region}. Staging uses it."})
    build_cost = round(BUILD_MINUTES * BUILD_USD_PER_MIN, 2) if src != "docker" else 0.0
    return {"profile": profile, "region": region, "account": account, "names": n, "image_source": src, "storage": storage,
            "local_workspace": local, "instance_type": instance_type, "creates": rows,
            "cost": {"setup_usd": build_cost,
                     "text": ("Setting up costs about $" + f"{build_cost:.2f}" + f" once. Building the image takes about a minute and a half in AWS CodeBuild (the estimate allows {BUILD_MINUTES} minutes). "
                              if src != "docker" else "Setting up costs nothing: this computer's Docker builds the image. ")
                             + "The role, the repository and the target cost nothing while idle. The image is under 1 GB, about $0.10 a month to "
                               "keep. An endpoint bills by the hour only while it is staged."},
            "login_policy": login_policy(storage, region, account, n, src),
            "text": f"Set up serving in your AWS account {account[:4]}... in {region}: " + ", ".join(f"{r['resource']} {r['name']}" for r in rows) + "."}


def _denied(e: Exception) -> str | None:
    err = (getattr(e, "response", None) or {}).get("Error") or {}
    code = str(err.get("Code") or "")
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError") or "not authorized" in str(err.get("Message") or "").lower():
        return str(err.get("Message") or code)
    return None


def setup(ctx: Context, profile: str, region: str, *, name_prefix: str = "captain", instance_type: str = "ml.m5.large",
          source: str = "auto", bucket: str | None = None, confirm: bool = False, progress=None, poll_s: float = 10.0) -> dict:
    """Create it all with the connected login. Safe to run again: what exists is kept. On a missing permission it
    stops with the step, the action and the policy, and what it already made stays listed for teardown."""
    from .. import __version__, cloud
    from .. import compute as C
    from .. import settings as ST0

    sess = cloud.aws_session(profile, region)
    account = sess.client("sts").get_caller_identity()["Account"]
    p = plan(ctx, profile, region, name_prefix=name_prefix, instance_type=instance_type, source=source, bucket=bucket, account=account)
    if not confirm:
        raise ValueError("Confirm first. " + p["text"] + " " + p["cost"]["text"])
    n, src, storage = p["names"], p["image_source"], p["storage"]
    say = progress or (lambda _t: None)
    tags_kv = [{"Key": k, "Value": v} for k, v in TAGS.items()]
    rec = read(ctx) or {"created": [], "created_at": _now(), "created_by": whoami()}
    rec.update(profile=profile, region=region, account=account, names=n, storage=storage, image_source=src, status="setting up")
    made = rec["created"]

    def note(kind: str, name: str) -> None:
        if not any(x["type"] == kind and x["name"] == name for x in made):
            made.append({"type": kind, "name": name, "region": region})
        _write(ctx, rec)

    def step(what: str, action: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if _denied(e):
                rec["status"] = "needs permission"
                _write(ctx, rec)
                raise MissingPermission(what, action, p["login_policy"]) from None
            raise

    iam, ecr = sess.client("iam"), sess.client("ecr")

    def role(name: str, service: str, policy: dict, why: str) -> str:
        say(f"Creating the role {name}")
        try:
            step(f"look up the role {name}", "iam:GetRole", lambda: iam.get_role(RoleName=name))
        except MissingPermission:
            raise
        except Exception:  # noqa: BLE001  not there yet
            step(f"create the role {name}", "iam:CreateRole", lambda: iam.create_role(
                RoleName=name, AssumeRolePolicyDocument=json.dumps(TRUST(service)), Tags=tags_kv, Description=f"Captain serving: {why}"))
            note("iam-role", name)
        step(f"set what {name} may do", "iam:PutRolePolicy", lambda: iam.put_role_policy(RoleName=name, PolicyName="captain-serving", PolicyDocument=json.dumps(policy)))
        return f"arn:aws:iam::{account}:role/{name}"

    if p["local_workspace"]:
        b = _where(storage)[0]
        say(f"Creating the bucket {b} for the serving bundle and the request log")
        s3 = sess.client("s3")

        def mk():
            try:
                s3.head_bucket(Bucket=b)
            except Exception:  # noqa: BLE001
                kw = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
                s3.create_bucket(Bucket=b, **kw)
                s3.put_bucket_tagging(Bucket=b, Tagging={"TagSet": tags_kv})
                note("s3-bucket", b)

        step(f"create the bucket {b}", "s3:CreateBucket", mk)
    role_arn = role(n["role"], "sagemaker.amazonaws.com", role_policy(storage, region, account, n), "what a staging endpoint may do")
    say(f"Creating the image repository {n['repo']}")

    def repo():
        try:
            ecr.describe_repositories(repositoryNames=[n["repo"]])
        except Exception as e:  # noqa: BLE001
            if _denied(e):
                raise
            ecr.create_repository(repositoryName=n["repo"], tags=tags_kv)
            note("ecr-repository", n["repo"])

    step(f"create the image repository {n['repo']}", "ecr:CreateRepository", repo)
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    folder, digest = build_folder()
    public0 = (ST0.get(ctx).get("serving") or {}).get("public_image") if src == "public" else None
    tag = f"{__version__}-{(hashlib.sha256(public0.encode()).hexdigest() if public0 else digest)[:10]}"
    image = f"{registry}/{n['repo']}:{tag}"
    have = False
    try:
        have = bool(ecr.describe_images(repositoryName=n["repo"], imageIds=[{"imageTag": tag}]).get("imageDetails"))
    except Exception:  # noqa: BLE001  not there: build it
        have = False
    t0 = time.time()
    if have:
        say("The serving image for this version of Captain is already in your account")
    elif src == "docker":
        say("Building the serving image with this computer's Docker")
        _docker_build(sess, folder, image, registry, region)
    else:
        from .. import settings as ST

        public = (ST.get(ctx).get("serving") or {}).get("public_image") if src == "public" else None
        build_role = role(n["build_role"], "codebuild.amazonaws.com", build_policy(storage, region, account, n), "what the image build may do")
        b, pre = _where(storage)
        key = f"{pre + '/' if pre else ''}serving/build/{tag}.zip"
        step("upload the build folder", "s3:PutObject", lambda: sess.client("s3").put_object(Bucket=b, Key=key, Body=folder))
        note("s3-object", f"s3://{b}/{key}")
        cb = sess.client("codebuild")
        spec = {"name": n["build"], "source": {"type": "S3", "location": f"{b}/{key}", "buildspec": _buildspec(image, registry, region, public)},
                "artifacts": {"type": "NO_ARTIFACTS"}, "serviceRole": build_role, "timeoutInMinutes": 30,
                "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0", "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True},
                "tags": [{"key": k, "value": v} for k, v in TAGS.items()]}

        def project():
            for i in range(6):  # a role made a moment ago is not always visible to CodeBuild yet
                try:
                    cb.create_project(**spec)
                    note("codebuild-project", n["build"])
                    return
                except Exception as e:  # noqa: BLE001
                    msg = str((getattr(e, "response", None) or {}).get("Error", {}).get("Message") or e)
                    if "already exists" in msg:
                        cb.update_project(**spec) if hasattr(cb, "update_project") else None
                        return
                    if "CodeBuild is not authorized to perform: sts:AssumeRole" in msg and i < 5:
                        time.sleep(min(poll_s, 5))
                        continue
                    raise

        say(f"Creating the image build {n['build']}")
        step(f"create the image build {n['build']}", "codebuild:CreateProject", project)
        note("log-group", f"/aws/codebuild/{n['build']}")  # the build writes its log there: removed with the rest
        say(f"Building the serving image in your account (about {BUILD_MINUTES} minutes)")
        bid = step("start the image build", "codebuild:StartBuild", lambda: cb.start_build(projectName=n["build"]))["build"]["id"]
        while True:
            b0 = step("watch the image build", "codebuild:BatchGetBuilds", lambda: cb.batch_get_builds(ids=[bid]))["builds"][0]
            if b0["buildStatus"] == "SUCCEEDED":
                break
            if b0["buildStatus"] != "IN_PROGRESS":
                phase = next((ph for ph in b0.get("phases") or [] if ph.get("phaseStatus") not in (None, "SUCCEEDED")), {})
                why = "; ".join(c.get("message", "") for c in phase.get("contexts") or []) or b0["buildStatus"]
                rec["status"] = "image build failed"
                _write(ctx, rec)
                raise ValueError(f"The image build did not finish ({phase.get('phaseType') or 'build'}): {why[:300]}")
            if time.time() - t0 > 1800:
                raise ValueError("The image build was still running after 30 minutes. Look at it in CodeBuild, then run setup again.")
            time.sleep(poll_s)
    rec["image"] = image
    rec["build_seconds"] = None if have else round(time.time() - t0)
    cfg = {"image": image, "execution_role_arn": role_arn, "instance_type": instance_type, "instance_count": "1", "name_prefix": n["endpoints"]}
    if p["local_workspace"]:
        cfg["serving_storage"] = storage
    say("Adding the compute target")
    try:
        C.get(ctx, n["target"])
        C.add(ctx, n["target"], "aws-sagemaker", profile, region, cfg, replace=True)
    except NotFound:
        C.add(ctx, n["target"], "aws-sagemaker", profile, region, cfg)
        note("compute-target", n["target"])
    rec.update(status="ready", target=n["target"], ready_at=_now())
    _write(ctx, rec)
    return {**rec, "text": f"Serving is set up in your AWS account. Staging can use the target {n['target']}."}


def _docker_build(sess, folder: bytes, image: str, registry: str, region: str) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        zipfile.ZipFile(io.BytesIO(folder)).extractall(d)
        os.chmod(os.path.join(d, "serve.sh"), 0o755)
        tok = sess.client("ecr").get_authorization_token()["authorizationData"][0]["authorizationToken"]
        import base64

        pw = base64.b64decode(tok).decode().split(":", 1)[1]
        for argv, stdin in ((["docker", "login", "--username", "AWS", "--password-stdin", registry], pw),
                            (["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false", "-t", image, "--load", d], None),
                            (["docker", "push", image], None)):
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise ValueError(f"Docker could not {argv[1]} the serving image: {(r.stderr or r.stdout).strip()[-300:]}")


def status(ctx: Context) -> dict:
    """What serving setup exists for this workspace: {status: none | ready | ..., created: [...], target, image}."""
    rec = read(ctx)
    if not rec:
        return {"status": "none", "created": [], "text": "Serving isn't set up in your cloud account yet."}
    return {**rec, "text": ("Serving is set up: " if rec.get("status") == "ready" else f"Serving setup is {rec.get('status')}: ")
                           + ", ".join(f"{x['type']} {x['name']}" for x in rec.get("created") or []) + "."}


def teardown(ctx: Context, progress=None) -> dict:
    """Remove what setup made: the compute target, the build project, the image repository with its images, the
    roles, and the build folder. A bucket Captain made is emptied of its serving prefix and removed only when
    nothing else is in it. What could not be removed is named. Refused while an endpoint is staged on the target."""
    from .. import cloud
    from .. import projects as PR
    from .. import project_state as PS

    rec = read(ctx)
    if not rec:
        raise ValueError("Serving was never set up for this workspace.")
    for pr in PR.listing(ctx):
        for env, st in ((PS.read(ctx, pr["id"])[0].get("staging") or {}).items()):
            if st.get("status") == "running" and st.get("target") == rec.get("target"):
                raise ValueError(f"{pr['name']} has a model staged on this target. Stop it first, then remove the setup.")
    say = progress or (lambda _t: None)
    sess = cloud.aws_session(rec["profile"], rec["region"])
    left = []
    # A workspace kept on this computer put its serving bundles, their catalog and the request log in a folder of
    # a bucket. That folder is Captain's and nothing else's, so it goes: the three things Captain writes there,
    # never the rest of the bucket. The log was already published into the workspace when each endpoint stopped.
    emptied = 0
    if rec.get("storage") and rec["storage"] != ctx.workspace.storage_uri:
        b, pre = _where(rec["storage"])
        base = pre + "/" if pre else ""
        say(f"Removing the serving bundle and request log from s3://{b}/{base}")
        try:
            s3 = sess.client("s3")
            for part in (base + "staging/", base + "serving/", base + "workspace.yaml"):
                token = None
                while True:
                    got = s3.list_objects_v2(Bucket=b, Prefix=part, **({"ContinuationToken": token} if token else {}))
                    for o in got.get("Contents", []):
                        s3.delete_object(Bucket=b, Key=o["Key"])
                        emptied += 1
                    token = got.get("NextContinuationToken")
                    if not token:
                        break
        except Exception as e:  # noqa: BLE001
            left.append({"type": "s3-folder", "name": f"s3://{b}/{base}", "error": str((getattr(e, "response", None) or {}).get("Error", {}).get("Message") or e)[:300]})
    order = {"compute-target": 0, "codebuild-project": 1, "log-group": 2, "ecr-repository": 2, "iam-role": 3, "s3-object": 4, "s3-bucket": 5}
    for x in sorted(rec.get("created") or [], key=lambda x: order.get(x["type"], 9)):
        say(f"Removing {x['type']} {x['name']}")
        try:
            t, name = x["type"], x["name"]
            if t == "compute-target":
                body, tag = ctx.store.get_with_tag(f"compute/{name}.yaml")
                ctx.store.put_if(f"compute/{name}.yaml", dump_yaml({**yaml.safe_load(body), "deleted": True, "purposes": []}), tag)
            elif t == "codebuild-project":
                sess.client("codebuild").delete_project(name=name)
            elif t == "log-group":
                sess.client("logs").delete_log_group(logGroupName=name)
            elif t == "ecr-repository":
                sess.client("ecr").delete_repository(repositoryName=name, force=True)
                # SageMaker keeps a lineage record of each image an endpoint ran. Those of this repository go too.
                from ..compute_providers.aws import _drop_lineage

                repo_uri = f"{rec['account']}.dkr.ecr.{rec['region']}.amazonaws.com/{name}"
                _drop_lineage(sess.client("sagemaker"), lambda _n: False, lambda uri: uri.startswith(repo_uri + ":") or uri.startswith(repo_uri + "@"))
            elif t == "iam-role":
                iam = sess.client("iam")
                try:
                    iam.delete_role_policy(RoleName=name, PolicyName="captain-serving")
                except Exception:  # noqa: BLE001
                    pass
                iam.delete_role(RoleName=name)
            elif t == "s3-object":
                b, _, k = name.split("://", 1)[1].partition("/")
                sess.client("s3").delete_object(Bucket=b, Key=k)
            elif t == "s3-bucket":
                s3 = sess.client("s3")
                keys = [o["Key"] for o in s3.list_objects_v2(Bucket=name).get("Contents", [])]
                if keys:
                    left.append({**x, "error": f"it still holds {len(keys)} object(s), such as {keys[0]}. Captain doesn't delete data it can't account for."})
                    continue
                s3.delete_bucket(Bucket=name)
        except Exception as e:  # noqa: BLE001
            msg = str((getattr(e, "response", None) or {}).get("Error", {}).get("Message") or e)
            if any(w in msg.lower() for w in ("not found", "does not exist", "cannot be found", "nosuch")):
                continue
            left.append({**x, "error": msg[:300]})
    rec.update(created=left, status="removed" if not left else "partly removed", removed_at=_now(), emptied=emptied)
    _write(ctx, rec)
    return {**rec, "text": "Serving setup is removed. Nothing of Captain's is left in the account." if not left else
            "Removed, except: " + "; ".join(f"{x['type']} {x['name']} ({x['error']})" for x in left)}
