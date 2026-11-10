"""Compute targets: where backtests run and where pipelines are served.

A target is shared workspace config (`compute/<name>.yaml`, no secrets). It names a cloud profile; each
person resolves that profile locally. Every cloud action is built as an explicit plan (the exact API
calls) first, so `--dry-run` shows what would happen, and only then executed through the provider SDK.

Each kind is a provider (compute_providers/): it plans, submits, reports status, reads logs and cancels.
Jobs run the runner image (runner/Dockerfile, entrypoint `cst`) with `-w <ws> <args>`; the job finds its
workspace from CST_STORAGE and CST_WORKSPACE and reads and writes the bucket directly, so cloud compute needs a
cloud workspace. Every submitted job is recorded (jobs.py). Serving runs the same image with
`pipeline serve <ref>`; deploys update the service to a new ref.

Which kinds have run on a real account: see each provider's `live_tested` (also in `cst compute kinds`).
"""
from __future__ import annotations

import datetime as dt

import yaml

from . import compute_providers as P
from .storage.backend import NotFound
from .storage.registry import check_name, dump_yaml
from .workspace import Context, whoami

from .compute_providers import IMAGE_DEFAULT  # noqa: F401  (re-exported for older imports)


def kinds() -> list[dict]:
    """Every kind a target can be: built in, plus the team's entry points (captain_search.compute)."""
    return [cls.describe() for cls in P.classes().values()]


def kind_map() -> dict[str, dict]:
    return {k["kind"]: k for k in kinds()}


def _key(name: str) -> str:
    return f"compute/{name}.yaml"


def add(ctx: Context, name: str, kind: str, profile: str | None, region: str | None, config: dict,
        replace: bool = False) -> dict:
    check_name(name)
    km = kind_map()
    spec = km.get(kind)
    if spec is None:
        raise ValueError(f"unknown compute kind {kind!r}: {', '.join(km)}")
    cfg = {}
    for f in spec["fields"]:
        n = f["name"]
        if f.get("type") == "secret":
            # Never in the target file (it's shared in the bucket): the value goes to this computer's keychain, and
            # the target keeps the keychain name. A caller may also pass <field>_secret naming an existing entry.
            raw = config.get(n)
            ref = config.get(f"{n}_secret")
            if raw not in (None, ""):
                from .workspace import set_secret

                ref = f"COMPUTE_{name}_{n}".upper().replace("-", "_")
                set_secret(ref, str(raw))
            if ref:
                cfg[f"{n}_secret"] = ref
            continue
        cfg[n] = config.get(n, f.get("default"))
    for f in spec["fields"]:
        val = cfg.get(f"{f['name']}_secret") if f.get("type") == "secret" else cfg.get(f["name"])
        if f["required"] and not str(val or "").strip():
            raise ValueError(f"{f['label']} is required for {spec['label']}")
    if kind in ("databricks-jobs", "snowflake-spcs"):
        _check_warehouse_auth(kind, cfg)
    if spec["provider"] in ("aws", "gcp", "azure") and not profile:
        raise ValueError(f"{spec['label']} needs a {spec['provider']} cloud profile")
    t = {"name": name, "kind": kind, "provider": spec["provider"], "profile": profile, "region": region,
         "config": cfg, "purposes": spec["purposes"], "created_by": whoami(),
         "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if replace and ctx.store.exists(_key(name)):
        _, tag = ctx.store.get_with_tag(_key(name))
        ctx.store.put_if(_key(name), dump_yaml(t), tag)
    else:
        ctx.store.put_new(_key(name), dump_yaml(t))
    return t


def _check_warehouse_auth(kind: str, cfg: dict) -> None:
    """Say at add time which sign-in fields are missing, rather than at the first job."""
    mode = cfg.get("auth")
    need = {("databricks-jobs", "token"): ["host", "token_secret"],
            ("databricks-jobs", "service-principal"): ["host", "client_id", "client_secret_secret"],
            ("databricks-jobs", "connection"): ["connection"],
            ("snowflake-spcs", "key-pair"): ["account", "user", "private_key_secret"],
            ("snowflake-spcs", "password"): ["account", "user", "password_secret"],
            ("snowflake-spcs", "connection"): ["connection"]}.get((kind, mode))
    if need is None:
        raise ValueError(f"unknown sign-in {mode!r} for {kind}")
    missing = [n.removesuffix("_secret").replace("_", " ") for n in need if not cfg.get(n)]
    if missing:
        raise ValueError(f"signing in with {mode} needs: {', '.join(missing)}")


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


# ---- jobs ------------------------------------------------------------------------------


def _check_cloud_workspace(ctx: Context, target: dict) -> None:
    if target["kind"] != "local" and ctx.workspace.storage_uri.split("://")[0] not in ("s3", "gs", "gcs", "az", "azure"):
        raise ValueError("cloud compute reads the workspace from its bucket, so it needs a cloud workspace "
                         "(s3://, gs:// or az://). This workspace is on local disk.")


def make_job(ctx: Context, target: dict, args: list[str], label: str, backtest: str | None = None) -> P.Job:
    from . import jobs as J

    _check_cloud_workspace(ctx, target)
    return J.make_job(ctx, args, label, backtest=backtest, target=target["name"])


def plan_backtest(ctx: Context, target: dict, backtest: str) -> list[dict]:
    """The provider API calls that start a backtest run on this target."""
    if target["kind"] == "local":
        return [{"service": "local", "call": "backtest_run", "args": {"name": backtest}}]
    return plan_job(ctx, target, ["backtest", "run", backtest], label=backtest, modal_backtest=backtest)


def plan_job(ctx: Context, target: dict, args: list[str], label: str, modal_backtest: str | None = None) -> list[dict]:
    """The provider API calls that run `cst <args>` once in the runner image on this target."""
    job = make_job(ctx, target, args, label, backtest=modal_backtest)
    return P.get(target, ctx).plan(job)


def plan_and_job(ctx: Context, target: dict, args: list[str], label: str, backtest: str | None = None) -> tuple[list[dict], P.Job]:
    """The plan and the job it describes, so what --dry-run or the output shows is what gets submitted."""
    job = make_job(ctx, target, args, label, backtest=backtest)
    return P.get(target, ctx).plan(job), job


def submit_job(ctx: Context, target: dict, args: list[str], label: str, backtest: str | None = None,
               kind: str = "job", job: P.Job | None = None) -> dict:
    """Submit and record one job. Returns the job record (jobs.py)."""
    from . import jobs as J

    job = job or make_job(ctx, target, args, label, backtest=backtest)
    return J.submit(ctx, target, job, kind=kind)


def provider_job(h: dict | None) -> str | None:
    """The provider's own id for a job, for people who look it up in the cloud console."""
    h = h or {}
    return next((h[k] for k in ("task_arn", "job_arn", "job_id", "execution", "name", "call_id", "pid") if h.get(k)), None)


def test(ctx: Context, name: str) -> dict:
    t = get(ctx, name)
    if t["provider"] in ("aws", "gcp", "azure"):
        from . import cloud

        r = cloud.test_profile(t["profile"])
        if not r["ok"]:
            return {"ok": False, "detail": f"profile {t['profile']}: {r['error']}"}
    try:
        return P.get(t, ctx).test()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:400]}


# ---- serving -------------------------------------------------------------------------------


def plan_serve(ctx: Context, target: dict, pipeline_ref: str, env_name: str) -> list[dict]:
    """The provider API calls that point a serving target at a pipeline version."""
    k, c, region = target["kind"], target["config"], target.get("region")
    env = {**_env(ctx), "CST_PIPELINE": pipeline_ref}
    tag = f"cst-{env_name}-{pipeline_ref.replace('@', '-').replace('/', '-')}"[:63]
    serve_cmd = ["-w", ctx.workspace.name, "pipeline", "serve", pipeline_ref, "--port", "8080", "--host", "0.0.0.0"]
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
            "template": {"containers": [{"name": c.get("container") or "runner", "image": c["image"], "args": serve_cmd,
                                         "env": [{"name": a, "value": b} for a, b in env.items()]}]}}}]
    if k == "azure-ml":
        if not c.get("endpoint"):
            raise ValueError("this Azure ML target has no online endpoint, so it cannot serve")
        return [{"service": "azureml", "call": "online_deployments.begin_create_or_update", "args": {
            "resource_group": c["resource_group"], "workspace_name": c["workspace_name"], "endpoint": c["endpoint"],
            "deployment": tag, "image": c["image"], "environment_variables": env, "traffic": {tag: 100}}}]
    raise ValueError(f"{k} cannot serve models")


def execute_serve(ctx: Context, target: dict, plan: list[dict]) -> dict:
    """Run a serving plan. A cloud's refusal comes back as one sentence naming the step, not a traceback."""
    try:
        return _execute_serve(ctx, target, plan)
    except Exception as e:  # noqa: BLE001
        resp = getattr(e, "response", None)
        if isinstance(resp, dict) and resp.get("Error"):
            err = resp["Error"]
            op = getattr(e, "operation_name", "the call")
            raise RuntimeError(f"{target.get('kind')} refused {op}: {err.get('Message') or err.get('Code')}") from None
        raise


def _execute_serve(ctx: Context, target: dict, plan: list[dict]) -> dict:
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
