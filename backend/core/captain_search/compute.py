"""Compute targets: where backtests run and where pipelines are served.

A target is shared workspace config (`compute/<name>.yaml`, no secrets). It names a cloud profile; each
person resolves that profile locally. Every cloud action is built as an explicit plan (the exact API
calls) first, so `--dry-run` shows what would happen, and only then executed through the provider SDK.

Backtest jobs run the runner image (runner/Dockerfile) with `cst -w <ws> backtest run <name>`. The job
reads and writes the workspace bucket directly, so cloud compute needs a cloud workspace.
Serving runs the same image with `cst pipeline serve <ref>`; deploys update the service to a new ref.

Status (2026-11-06): `local` is tested. The cloud kinds build plans that are unit-tested; their SDK
calls have not run against live accounts.
"""
from __future__ import annotations

import datetime as dt
import uuid

import yaml

from .storage.backend import NotFound
from .storage.registry import check_name, dump_yaml
from .workspace import Context, whoami

IMAGE_DEFAULT = "ghcr.io/captain/captain-search-runner:0.1.0"

KINDS = [
    {"kind": "local", "provider": "local", "label": "This machine", "purposes": ["backtest"], "fields": []},
    {"kind": "modal", "provider": "modal", "label": "Modal function", "purposes": ["backtest"], "fields": [
        {"name": "app", "label": "Modal app name", "required": True, "default": "captain-search-runner"},
        {"name": "function", "label": "Function", "required": True, "default": "run_backtest"}]},
    {"kind": "aws-batch", "provider": "aws", "label": "AWS Batch job", "purposes": ["backtest"], "fields": [
        {"name": "job_queue", "label": "Job queue", "required": True},
        {"name": "job_definition", "label": "Job definition (runner image)", "required": True},
        {"name": "vcpus", "label": "vCPUs", "required": False, "default": "4"},
        {"name": "memory_mb", "label": "Memory (MB)", "required": False, "default": "16384"}]},
    {"kind": "aws-ecs", "provider": "aws", "label": "AWS ECS (Fargate)", "purposes": ["backtest", "serve"], "fields": [
        {"name": "cluster", "label": "Cluster", "required": True},
        {"name": "task_definition", "label": "Task definition (runner image)", "required": True},
        {"name": "subnets", "label": "Subnets (comma separated)", "required": True},
        {"name": "security_groups", "label": "Security groups (comma separated)", "required": False},
        {"name": "service", "label": "Service name (for serving)", "required": False},
        {"name": "container", "label": "Container name", "required": False, "default": "runner"}]},
    {"kind": "aws-sagemaker", "provider": "aws", "label": "AWS SageMaker endpoint", "purposes": ["serve"], "fields": [
        {"name": "image", "label": "Serving image URI (ECR)", "required": True},
        {"name": "execution_role_arn", "label": "Execution role ARN", "required": True},
        {"name": "instance_type", "label": "Instance type", "required": False, "default": "ml.m5.large"},
        {"name": "instance_count", "label": "Instance count", "required": False, "default": "1"}]},
    {"kind": "gcp-cloud-run", "provider": "gcp", "label": "Google Cloud Run (jobs and services)", "purposes": ["backtest", "serve"], "fields": [
        {"name": "job", "label": "Job name (for backtests)", "required": False},
        {"name": "service", "label": "Service name (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT}]},
    {"kind": "gcp-vertex", "provider": "gcp", "label": "Google Vertex AI custom job", "purposes": ["backtest"], "fields": [
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT},
        {"name": "machine_type", "label": "Machine type", "required": False, "default": "n2-standard-8"},
        {"name": "service_account", "label": "Service account email", "required": False}]},
    {"kind": "azure-container-apps", "provider": "azure", "label": "Azure Container Apps (jobs and apps)", "purposes": ["backtest", "serve"], "fields": [
        {"name": "resource_group", "label": "Resource group", "required": True},
        {"name": "job", "label": "Job name (for backtests)", "required": False},
        {"name": "app", "label": "Container app name (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT}]},
    {"kind": "azure-ml", "provider": "azure", "label": "Azure Machine Learning", "purposes": ["backtest", "serve"], "fields": [
        {"name": "resource_group", "label": "Resource group", "required": True},
        {"name": "workspace_name", "label": "AML workspace", "required": True},
        {"name": "compute", "label": "Compute cluster (for jobs)", "required": False},
        {"name": "endpoint", "label": "Online endpoint (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT}]},
]
KIND = {k["kind"]: k for k in KINDS}


def _key(name: str) -> str:
    return f"compute/{name}.yaml"


def add(ctx: Context, name: str, kind: str, profile: str | None, region: str | None, config: dict) -> dict:
    check_name(name)
    if kind not in KIND:
        raise ValueError(f"unknown compute kind {kind!r}: {', '.join(KIND)}")
    spec = KIND[kind]
    cfg = {f["name"]: config.get(f["name"], f.get("default")) for f in spec["fields"]}
    for f in spec["fields"]:
        if f["required"] and not str(cfg.get(f["name"]) or "").strip():
            raise ValueError(f"{f['label']} is required for {spec['label']}")
    if spec["provider"] in ("aws", "gcp", "azure") and not profile:
        raise ValueError(f"{spec['label']} needs a {spec['provider']} cloud profile")
    t = {"name": name, "kind": kind, "provider": spec["provider"], "profile": profile, "region": region,
         "config": cfg, "purposes": spec["purposes"], "created_by": whoami(),
         "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    ctx.store.put_new(_key(name), dump_yaml(t))
    return t


def get(ctx: Context, name: str) -> dict:
    try:
        return yaml.safe_load(ctx.store.get(_key(name)))
    except NotFound:
        if name == "local":
            return {"name": "local", "kind": "local", "provider": "local", "profile": None, "region": None,
                    "config": {}, "purposes": ["backtest"]}
        raise NotFound(f"no compute target {name!r}. Add one on the Compute screen or with `cst compute add`.") from None


def listing(ctx: Context) -> list[dict]:
    out = [get(ctx, "local")]
    for k in ctx.store.list("compute/"):
        if k.endswith(".yaml"):
            out.append(yaml.safe_load(ctx.store.get(k)))
    return out


def _env(ctx: Context) -> dict:
    return {"CST_STORAGE": ctx.workspace.storage_uri, "CST_WORKSPACE": ctx.workspace.name, "CST_USER": whoami()}


def _cmd(ctx: Context, args: list[str]) -> list[str]:
    return ["sh", "-c", 'cst init "$CST_WORKSPACE" --storage "$CST_STORAGE" >/dev/null && cst ' + " ".join(args)]


# ---- backtest jobs ---------------------------------------------------------------------


def plan_backtest(ctx: Context, target: dict, backtest: str) -> list[dict]:
    """The provider API calls that start a backtest run on this target."""
    if target["kind"] == "local":
        return [{"service": "local", "call": "backtest_run", "args": {"name": backtest}}]
    return plan_job(ctx, target, ["backtest", "run", backtest], label=backtest, modal_backtest=backtest)


def plan_job(ctx: Context, target: dict, args: list[str], label: str, modal_backtest: str | None = None) -> list[dict]:
    """The provider API calls that run `cst <args>` once in the runner image on this target."""
    k, c, region = target["kind"], target["config"], target.get("region")
    if k == "local":
        return [{"service": "local", "call": "cst", "args": {"argv": args}}]
    if ctx.workspace.storage_uri.split("://")[0] not in ("s3", "gs", "gcs", "az", "azure"):
        raise ValueError("cloud compute reads the workspace from its bucket, so it needs a cloud workspace "
                         "(s3://, gs:// or az://). This workspace is on local disk.")
    backtest = label
    env, cmd = _env(ctx), _cmd(ctx, ["-w", ctx.workspace.name, *args])
    run_id = f"cst-{label.replace('/', '-')}-{uuid.uuid4().hex[:6]}"[:63]
    if k == "modal":
        if modal_backtest is None:
            raise ValueError("Modal targets run backtests only for now. Use a Batch, ECS, Cloud Run, Vertex or Azure target.")
        return [{"service": "modal", "call": "Function.from_name(app, function).spawn",
                 "args": {"app": c["app"], "function": c["function"], "workspace": env, "backtest": modal_backtest}}]
    if k == "aws-batch":
        return [{"service": "batch", "region": region, "call": "submit_job", "args": {
            "jobName": run_id, "jobQueue": c["job_queue"], "jobDefinition": c["job_definition"],
            "containerOverrides": {"command": cmd, "environment": [{"name": a, "value": b} for a, b in env.items()],
                                   "resourceRequirements": [{"type": "VCPU", "value": str(c.get("vcpus") or 4)},
                                                            {"type": "MEMORY", "value": str(c.get("memory_mb") or 16384)}]}}}]
    if k == "aws-ecs":
        return [{"service": "ecs", "region": region, "call": "run_task", "args": {
            "cluster": c["cluster"], "taskDefinition": c["task_definition"], "launchType": "FARGATE", "count": 1,
            "networkConfiguration": {"awsvpcConfiguration": {
                "subnets": [s.strip() for s in str(c["subnets"]).split(",") if s.strip()],
                "securityGroups": [s.strip() for s in str(c.get("security_groups") or "").split(",") if s.strip()],
                "assignPublicIp": "DISABLED"}},
            "overrides": {"containerOverrides": [{"name": c.get("container") or "runner", "command": cmd,
                                                  "environment": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "gcp-cloud-run":
        if not c.get("job"):
            raise ValueError("this Cloud Run target has no job name, so it cannot run backtests")
        return [{"service": "run", "region": region, "call": "JobsClient.run_job", "args": {
            "name": f"projects/{{project}}/locations/{region}/jobs/{c['job']}",
            "overrides": {"container_overrides": [{"args": cmd, "env": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "gcp-vertex":
        return [{"service": "aiplatform", "region": region, "call": "JobServiceClient.create_custom_job", "args": {
            "parent": f"projects/{{project}}/locations/{region}",
            "custom_job": {"display_name": run_id, "job_spec": {
                "service_account": c.get("service_account") or None,
                "worker_pool_specs": [{"machine_spec": {"machine_type": c.get("machine_type") or "n2-standard-8"},
                                       "replica_count": 1,
                                       "container_spec": {"image_uri": c["image"], "command": cmd,
                                                          "env": [{"name": a, "value": b} for a, b in env.items()]}}]}}}}]
    if k == "azure-container-apps":
        if not c.get("job"):
            raise ValueError("this Container Apps target has no job name, so it cannot run backtests")
        return [{"service": "containerapps", "call": "jobs.begin_start", "args": {
            "resource_group_name": c["resource_group"], "job_name": c["job"],
            "template": {"containers": [{"name": "runner", "image": c["image"], "command": cmd,
                                         "env": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "azure-ml":
        if not c.get("compute"):
            raise ValueError("this Azure ML target has no compute cluster, so it cannot run backtests")
        return [{"service": "azureml", "call": "jobs.create_or_update(command)", "args": {
            "resource_group": c["resource_group"], "workspace_name": c["workspace_name"], "compute": c["compute"],
            "environment_image": c["image"], "command": " ".join(cmd[2:]) if cmd[:2] == ["sh", "-c"] else " ".join(cmd),
            "environment_variables": env, "display_name": run_id}}]
    raise ValueError(f"{k} cannot run jobs")


def execute(ctx: Context, target: dict, plan: list[dict]) -> dict:
    """Run a plan through the provider SDK. Returns {provider_job, console?}."""
    from . import cloud

    out = {}
    for step in plan:
        svc = step["service"]
        if svc == "local":
            continue
        if svc in ("batch", "ecs", "sagemaker"):
            client = cloud.aws_session(target["profile"], step.get("region")).client(svc)
            r = getattr(client, step["call"])(**step["args"])
            out["provider_job"] = r.get("jobArn") or r.get("jobId") or (r.get("tasks") or [{}])[0].get("taskArn") or r.get("EndpointArn")
        elif svc == "modal":
            import modal  # type: ignore

            fn = modal.Function.from_name(step["args"]["app"], step["args"]["function"])
            call = fn.spawn(step["args"]["workspace"], step["args"]["backtest"])
            out["provider_job"] = call.object_id
        elif svc == "run":
            from google.cloud import run_v2  # type: ignore

            creds, project = cloud.gcp_credentials(target["profile"])
            a = dict(step["args"])
            a["name"] = a["name"].replace("{project}", project)
            op = run_v2.JobsClient(credentials=creds).run_job(request=run_v2.RunJobRequest(**a))
            out["provider_job"] = op.operation.name
        elif svc == "aiplatform":
            from google.cloud import aiplatform_v1  # type: ignore

            creds, project = cloud.gcp_credentials(target["profile"])
            region = step.get("region")
            cl = aiplatform_v1.JobServiceClient(credentials=creds, client_options={"api_endpoint": f"{region}-aiplatform.googleapis.com"})
            a = dict(step["args"])
            a["parent"] = a["parent"].replace("{project}", project)
            out["provider_job"] = cl.create_custom_job(parent=a["parent"], custom_job=a["custom_job"]).name
        elif svc == "containerapps":
            from azure.mgmt.appcontainers import ContainerAppsAPIClient  # type: ignore

            cred, _, sub = cloud.azure_credential(target["profile"])
            cl = ContainerAppsAPIClient(cred, sub)
            a = step["args"]
            poller = cl.jobs.begin_start(a["resource_group_name"], a["job_name"], {"containers": a["template"]["containers"]})
            out["provider_job"] = getattr(poller.result(), "id", None)
        elif svc == "azureml":
            from azure.ai.ml import MLClient, command  # type: ignore
            from azure.ai.ml.entities import Environment as AMLEnv  # type: ignore

            cred, _, sub = cloud.azure_credential(target["profile"])
            a = step["args"]
            ml = MLClient(cred, sub, a["resource_group"], a["workspace_name"])
            job = command(command=a["command"], environment=AMLEnv(image=a["environment_image"]),
                          compute=a["compute"], environment_variables=a["environment_variables"], display_name=a["display_name"])
            out["provider_job"] = ml.jobs.create_or_update(job).name
        else:
            raise ValueError(f"no executor for {svc}")
    return out


def test(ctx: Context, name: str) -> dict:
    t = get(ctx, name)
    if t["kind"] == "local":
        return {"ok": True, "detail": "runs in the daemon on this machine"}
    from . import cloud

    if t["provider"] in ("aws", "gcp", "azure"):
        r = cloud.test_profile(t["profile"])
        if not r["ok"]:
            return {"ok": False, "detail": f"profile {t['profile']}: {r['error']}"}
        return {"ok": True, "detail": f"credentials work ({r['identity']}). The {t['kind']} resources themselves are not checked yet."}
    if t["kind"] == "modal":
        try:
            import modal  # type: ignore  # noqa: F401
        except ImportError:
            return {"ok": False, "detail": "the modal package is not installed"}
        return {"ok": True, "detail": "modal is installed. Deploy runner/modal_app.py once with `modal deploy`."}
    return {"ok": False, "detail": "unknown"}


# ---- serving -------------------------------------------------------------------------------


def plan_serve(ctx: Context, target: dict, pipeline_ref: str, env_name: str) -> list[dict]:
    """The provider API calls that point a serving target at a pipeline version."""
    k, c, region = target["kind"], target["config"], target.get("region")
    env = {**_env(ctx), "CST_PIPELINE": pipeline_ref}
    tag = f"cst-{env_name}-{pipeline_ref.replace('@', '-').replace('/', '-')}"[:63]
    serve_cmd = _cmd(ctx, ["-w", ctx.workspace.name, "pipeline", "serve", pipeline_ref, "--port", "8080", "--host", "0.0.0.0"])
    if k == "aws-sagemaker":
        return [
            {"service": "sagemaker", "region": region, "call": "create_model", "args": {
                "ModelName": tag, "ExecutionRoleArn": c["execution_role_arn"],
                "PrimaryContainer": {"Image": c["image"], "Environment": env}}},
            {"service": "sagemaker", "region": region, "call": "create_endpoint_config", "args": {
                "EndpointConfigName": tag, "ProductionVariants": [{
                    "VariantName": "main", "ModelName": tag, "InstanceType": c.get("instance_type") or "ml.m5.large",
                    "InitialInstanceCount": int(c.get("instance_count") or 1)}]}},
            {"service": "sagemaker", "region": region, "call": "update_endpoint_or_create", "args": {
                "EndpointName": f"cst-{env_name}"[:63], "EndpointConfigName": tag}},
        ]
    if k == "aws-ecs":
        if not c.get("service"):
            raise ValueError("this ECS target has no service name, so it cannot serve")
        return [{"service": "ecs", "region": region, "call": "register_task_definition_with_env", "args": {
                    "base": c["task_definition"], "container": c.get("container") or "runner", "environment": env,
                    "command": serve_cmd}},
                {"service": "ecs", "region": region, "call": "update_service", "args": {
                    "cluster": c["cluster"], "service": c["service"], "taskDefinition": "<from previous step>",
                    "forceNewDeployment": True}}]
    if k == "gcp-cloud-run":
        if not c.get("service"):
            raise ValueError("this Cloud Run target has no service name, so it cannot serve")
        return [{"service": "run", "region": region, "call": "ServicesClient.update_service", "args": {
            "name": f"projects/{{project}}/locations/{region}/services/{c['service']}",
            "template": {"containers": [{"image": c["image"], "args": serve_cmd,
                                         "env": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "azure-container-apps":
        if not c.get("app"):
            raise ValueError("this Container Apps target has no app name, so it cannot serve")
        return [{"service": "containerapps", "call": "container_apps.begin_update", "args": {
            "resource_group_name": c["resource_group"], "container_app_name": c["app"],
            "template": {"containers": [{"name": "runner", "image": c["image"], "command": serve_cmd,
                                         "env": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "azure-ml":
        if not c.get("endpoint"):
            raise ValueError("this Azure ML target has no online endpoint, so it cannot serve")
        return [{"service": "azureml", "call": "online_deployments.begin_create_or_update", "args": {
            "resource_group": c["resource_group"], "workspace_name": c["workspace_name"], "endpoint": c["endpoint"],
            "deployment": tag, "image": c["image"], "environment_variables": env, "traffic": {tag: 100}}}]
    raise ValueError(f"{k} cannot serve pipelines")


def execute_serve(ctx: Context, target: dict, plan: list[dict]) -> dict:
    from . import cloud

    out = {}
    for step in plan:
        svc, a = step["service"], step["args"]
        if svc == "sagemaker":
            sm = cloud.aws_session(target["profile"], step.get("region")).client("sagemaker")
            if step["call"] == "update_endpoint_or_create":
                try:
                    sm.update_endpoint(**a)
                except sm.exceptions.ClientError:
                    sm.create_endpoint(**a)
                out["endpoint"] = a["EndpointName"]
            else:
                getattr(sm, step["call"])(**a)
        elif svc == "ecs":
            ecs = cloud.aws_session(target["profile"], step.get("region")).client("ecs")
            if step["call"] == "register_task_definition_with_env":
                td = ecs.describe_task_definition(taskDefinition=a["base"])["taskDefinition"]
                keep = {k: td[k] for k in ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
                                           "requiresCompatibilities", "cpu", "memory") if k in td}
                for cd in keep["containerDefinitions"]:
                    if cd["name"] == a["container"]:
                        cd["environment"] = [{"name": k, "value": v} for k, v in a["environment"].items()]
                        cd["command"] = a["command"]
                out["task_definition"] = ecs.register_task_definition(**keep)["taskDefinition"]["taskDefinitionArn"]
            else:
                ecs.update_service(**{**a, "taskDefinition": out["task_definition"]})
        else:
            raise ValueError(f"serving on {svc} is planned but not executable yet in this build. Run the plan by hand.")
    return out
