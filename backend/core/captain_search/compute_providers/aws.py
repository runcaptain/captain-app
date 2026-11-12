"""AWS: ECS on Fargate, Batch, and SageMaker (serving only). Logs come from CloudWatch for both job kinds."""
from __future__ import annotations

import datetime as dt

from . import ComputeProvider, Job, ProviderError, env_list, split_csv, status

TAGS = {"captain:purpose": "cst-compute"}


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, (int, float)):  # Batch reports epoch milliseconds
        t = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _who() -> str:
    from ..workspace import whoami

    return whoami()


class _AWS(ComputeProvider):
    provider = "aws"

    def _session(self):
        from .. import cloud

        return cloud.aws_session(self.profile, self.region)

    def _client(self, name: str):
        return self._session().client(name)

    def _identity(self) -> str:
        return self._client("sts").get_caller_identity()["Arn"]

    def _cw_logs(self, group: str | None, stream: str | None, since: str | None) -> dict:
        if not group or not stream:
            return {"lines": [], "next": since, "note": "this job has no CloudWatch log stream, so its output is not kept"}
        logs = self._client("logs")
        kw = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        if since:
            kw["nextToken"] = since
        lines, token = [], since
        try:
            for _ in range(20):  # 20 pages of up to 10,000 events: far more than a backtest prints
                r = logs.get_log_events(**kw)
                lines += [{"ts": _iso(e["timestamp"]), "message": e["message"]} for e in r.get("events", [])]
                nxt = r.get("nextForwardToken")
                if not nxt or nxt == token:
                    break
                token = kw["nextToken"] = nxt
        except logs.exceptions.ResourceNotFoundException:
            return {"lines": [], "next": since, "note": "no log lines yet: the container has not started writing"}
        return {"lines": lines, "next": token, "where": f"CloudWatch {group} {stream}"}


class ECSProvider(_AWS):
    kind, label = "aws-ecs", "AWS ECS (Fargate)"
    purposes = ["backtest", "serve"]
    # Captain's AWS account, us-west-1: demo seed, backtest, cancel (2026-11-06), and the starter ranker trained as a
    # job with its cost recorded (2026-11-10).
    live_tested = "2026-11-10"
    fields = [
        {"name": "cluster", "label": "Cluster", "required": True},
        {"name": "task_definition", "label": "Task definition (runner image)", "required": True},
        {"name": "subnets", "label": "Subnets (comma separated)", "required": True},
        {"name": "security_groups", "label": "Security groups (comma separated)", "required": False},
        {"name": "assign_public_ip", "label": "Public IP (ENABLED when the subnets have no NAT gateway)", "required": False,
         "default": "DISABLED"},
        {"name": "service", "label": "Service name (for serving)", "required": False},
        {"name": "container", "label": "Container name", "required": False, "default": "runner"}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        return [{"service": "ecs", "region": self.region, "call": "run_task", "args": {
            "cluster": c["cluster"], "taskDefinition": c["task_definition"], "launchType": "FARGATE", "count": 1,
            "startedBy": job.id[:36],
            "tags": [{"key": k, "value": v} for k, v in {**TAGS, "captain:job": job.id}.items()],
            "networkConfiguration": {"awsvpcConfiguration": {
                "subnets": split_csv(c["subnets"]), "securityGroups": split_csv(c.get("security_groups")),
                "assignPublicIp": (c.get("assign_public_ip") or "DISABLED").upper()}},
            "overrides": {"containerOverrides": [{"name": c.get("container") or "runner", "command": job.argv,
                                                  "environment": env_list(job.env)}]}}}]

    def submit(self, job: Job) -> dict:
        ecs = self._client("ecs")
        args = self.plan(job)[0]["args"]
        r = ecs.run_task(**args)
        if r.get("failures") or not r.get("tasks"):
            why = "; ".join(f"{f.get('arn', '')} {f.get('reason')} {f.get('detail') or ''}".strip() for f in r.get("failures", []))
            raise ProviderError(f"ECS did not start the task: {why or 'no task returned'}")
        task = r["tasks"][0]
        h = {"cluster": args["cluster"], "task_arn": task["taskArn"], "region": self.region,
             "task_definition": task.get("taskDefinitionArn")}
        h.update(self._log_location(ecs, h["task_definition"] or args["taskDefinition"], h["task_arn"]))
        return h

    def _log_location(self, ecs, td: str, task_arn: str) -> dict:
        name = self.config.get("container") or "runner"
        cd = next((d for d in ecs.describe_task_definition(taskDefinition=td)["taskDefinition"]["containerDefinitions"]
                   if d["name"] == name), None)
        lc = (cd or {}).get("logConfiguration") or {}
        o = lc.get("options") or {}
        if lc.get("logDriver") != "awslogs" or "awslogs-group" not in o:
            return {"log_group": None, "log_stream": None}
        prefix = o.get("awslogs-stream-prefix")
        tid = task_arn.rsplit("/", 1)[-1]
        return {"log_group": o["awslogs-group"], "log_stream": f"{prefix}/{name}/{tid}" if prefix else None}

    def status(self, h: dict) -> dict:
        r = self._client("ecs").describe_tasks(cluster=h["cluster"], tasks=[h["task_arn"]])
        if not r.get("tasks"):
            return status("failed", "ECS no longer reports this task. It forgets stopped tasks after about an hour, "
                                    "so its last state was not recorded. The logs still show what it did.")
        t = r["tasks"][0]
        # Fargate bills from the start of the image pull, a little before the container starts.
        times = {"started_at": _iso(t.get("startedAt")), "finished_at": _iso(t.get("stoppedAt")),
                 "billed_from": _iso(t.get("pullStartedAt"))}
        last = t.get("lastStatus")
        if last in ("PROVISIONING", "PENDING", "ACTIVATING"):
            return status("queued", f"Fargate is starting the task ({last.lower()})", **times)
        if last != "STOPPED":
            if t.get("desiredStatus") == "STOPPED":
                return status("running", f"stopping: {t.get('stoppedReason') or 'stop requested'}", **times)
            return status("running", "the container is running", **times)
        name = self.config.get("container") or "runner"
        ct = next((c for c in t.get("containers", []) if c.get("name") == name), (t.get("containers") or [{}])[0])
        code, code_reason = ct.get("exitCode"), ct.get("reason")
        stop_code, why = t.get("stopCode"), t.get("stoppedReason") or ""
        if stop_code == "UserInitiated":
            if code == 0:
                return status("succeeded", "a stop was asked for, but the job had already finished with code 0",
                              exit_code=0, **times)
            return status("stopped", why or "stopped by a person", exit_code=code, **times)
        if stop_code == "TaskFailedToStart" or code is None:
            detail = "; ".join(x for x in (why, code_reason) if x)
            return status("failed", f"the task never ran the container: {detail or 'no reason given'}", **times)
        if code == 0:
            return status("succeeded", "the container exited with code 0", exit_code=0, **times)
        return status("failed", f"cst exited with code {code}. The last log lines say why."
                      + (f" ECS adds: {code_reason}" if code_reason else ""), exit_code=code, **times)

    def logs(self, h: dict, since: str | None = None) -> dict:
        return self._cw_logs(h.get("log_group"), h.get("log_stream"), since)

    def cancel(self, h: dict) -> dict:
        self._client("ecs").stop_task(cluster=h["cluster"], task=h["task_arn"], reason=f"cancelled from Captain by {_who()}"[:255])
        return self.status(h)

    def test(self) -> dict:
        c = self.config
        try:
            who = self._identity()
            ecs = self._client("ecs")
            cl = ecs.describe_clusters(clusters=[c["cluster"]])["clusters"]
            if not cl or cl[0]["status"] != "ACTIVE":
                return {"ok": False, "detail": f"signed in as {who}, but there is no active cluster {c['cluster']} in {self.region}"}
            td = ecs.describe_task_definition(taskDefinition=c["task_definition"])["taskDefinition"]
            image = next((d["image"] for d in td["containerDefinitions"] if d["name"] == (c.get("container") or "runner")), None)
            if image is None:
                return {"ok": False, "detail": f"task definition {c['task_definition']} has no container named {c.get('container') or 'runner'}"}
            subnets = split_csv(c["subnets"])
            found = self._client("ec2").describe_subnets(SubnetIds=subnets)["Subnets"]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}
        return {"ok": True, "detail": f"signed in as {who}. Cluster {c['cluster']} is active, task definition "
                                      f"{td['family']}:{td['revision']} runs {image}, {len(found)} subnets found."}


class BatchProvider(_AWS):
    kind, label = "aws-batch", "AWS Batch job"
    purposes = ["backtest"]
    live_tested = "2026-11-06"  # Captain's AWS account, us-west-1, Fargate compute environment: backtest, cancel
    fields = [
        {"name": "job_queue", "label": "Job queue", "required": True},
        {"name": "job_definition", "label": "Job definition (runner image)", "required": True},
        {"name": "vcpus", "label": "vCPUs", "required": False, "default": "4"},
        {"name": "memory_mb", "label": "Memory (MB)", "required": False, "default": "16384"}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        return [{"service": "batch", "region": self.region, "call": "submit_job", "args": {
            "jobName": _job_name(job), "jobQueue": c["job_queue"], "jobDefinition": c["job_definition"],
            "tags": {**TAGS, "captain:job": job.id}, "propagateTags": True,
            "containerOverrides": {"command": job.argv, "environment": env_list(job.env),
                                   "resourceRequirements": [{"type": "VCPU", "value": str(c.get("vcpus") or 4)},
                                                            {"type": "MEMORY", "value": str(c.get("memory_mb") or 16384)}]}}}]

    def submit(self, job: Job) -> dict:
        r = self._client("batch").submit_job(**self.plan(job)[0]["args"])
        return {"job_id": r["jobId"], "job_arn": r.get("jobArn"), "region": self.region}

    def _describe(self, h: dict) -> dict | None:
        js = self._client("batch").describe_jobs(jobs=[h["job_id"]]).get("jobs") or []
        return js[0] if js else None

    def status(self, h: dict) -> dict:
        j = self._describe(h)
        if j is None:
            return status("failed", "Batch no longer reports this job (it keeps finished jobs for about a week)")
        s, why = j["status"], j.get("statusReason") or ""
        ct = j.get("container") or {}
        code = ct.get("exitCode")
        times = {"started_at": _iso(j.get("startedAt")), "finished_at": _iso(j.get("stoppedAt"))}
        if s in ("SUBMITTED", "PENDING"):
            return status("queued", "Batch accepted the job and is placing it", **times)
        if s == "RUNNABLE":
            return status("queued", "waiting for capacity in the compute environment", **times)
        if s == "STARTING":
            return status("queued", "the container is starting", **times)
        if s == "RUNNING":
            return status("running", "the container is running", **times)
        if s == "SUCCEEDED":
            return status("succeeded", "the container exited with code 0", exit_code=code, **times)
        if why.startswith("Captain: cancelled"):
            return status("stopped", why.removeprefix("Captain: "), exit_code=code, **times)
        if code not in (None, 0):
            return status("failed", f"cst exited with code {code}. The last log lines say why.", exit_code=code, **times)
        return status("failed", why or ct.get("reason") or "Batch marked the job failed without a reason", exit_code=code, **times)

    def logs(self, h: dict, since: str | None = None) -> dict:
        j = self._describe(h) or {}
        ct = j.get("container") or {}
        o = (ct.get("logConfiguration") or {}).get("options") or {}
        return self._cw_logs(o.get("awslogs-group") or "/aws/batch/job", ct.get("logStreamName"), since)

    def cancel(self, h: dict) -> dict:
        self._client("batch").terminate_job(jobId=h["job_id"], reason=f"Captain: cancelled by {_who()}"[:255])
        return self.status(h)

    def test(self) -> dict:
        c = self.config
        try:
            who = self._identity()
            b = self._client("batch")
            q = b.describe_job_queues(jobQueues=[c["job_queue"]])["jobQueues"]
            if not q or q[0]["state"] != "ENABLED" or q[0]["status"] != "VALID":
                return {"ok": False, "detail": f"signed in as {who}, but job queue {c['job_queue']} is missing, disabled or invalid"}
            jd = c["job_definition"]
            # A bare name goes in jobDefinitionName; jobDefinitions takes only name:revision or an ARN.
            kw = {"jobDefinitions": [jd]} if ":" in jd else {"jobDefinitionName": jd}
            d = b.describe_job_definitions(**kw, status="ACTIVE")["jobDefinitions"]
            d = sorted(d, key=lambda x: x["revision"], reverse=True)
            if not d:
                return {"ok": False, "detail": f"no active job definition {c['job_definition']}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}
        return {"ok": True, "detail": f"signed in as {who}. Queue {c['job_queue']} is enabled, "
                                      f"job definition {d[0]['jobDefinitionName']}:{d[0]['revision']} is active."}


class SageMakerProvider(_AWS):
    """Serving only. Deploys go through compute.plan_serve and compute.execute_serve."""
    kind, label = "aws-sagemaker", "AWS SageMaker endpoint"
    purposes = ["serve"]
    # Captain's AWS account, us-west-1, ml.t2.medium: demo-bm25@v1 deployed, /invocations matched local results, deleted.
    live_tested = "2026-11-07"
    can_logs = can_cancel = False
    fields = [
        {"name": "image", "label": "Serving image URI (ECR)", "required": True},
        {"name": "execution_role_arn", "label": "Execution role ARN", "required": True},
        {"name": "instance_type", "label": "Instance type", "required": False, "default": "ml.m5.large"},
        {"name": "instance_count", "label": "Instance count", "required": False, "default": "1"},
        {"name": "name_prefix", "label": "Name prefix for staging resources", "required": False, "default": "captain"},
        {"name": "serving_storage", "label": "Bucket folder for the serving bundle (for a workspace kept on this computer)", "required": False}]

    def plan(self, job: Job) -> list[dict]:
        raise ValueError("a SageMaker endpoint serves models and cannot run jobs. Use an ECS or Batch target.")

    submit = plan

    def status(self, h):
        raise ValueError("SageMaker targets run no jobs")

    def logs(self, h, since=None):
        return self.status(h)

    def cancel(self, h):
        return self.status(h)

    def test(self) -> dict:
        try:
            return {"ok": True, "detail": f"signed in as {self._identity()}. The endpoint itself is not checked."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}

    # ---- staging endpoints (staging/): created, tagged, waited for and removed by Captain -------------

    WAIT_S = 1500  # a new endpoint takes 4 to 10 minutes; SageMaker's own limit for a container to start is longer
    POLL_S = 10

    def _names(self, st: dict) -> dict:
        import re

        prefix = (self.config.get("name_prefix") or "captain").strip("-")
        base = re.sub(r"[^A-Za-z0-9-]", "-", f"{prefix}-{st['project']}-{st['env']}").strip("-")[:50]
        one = f"{base}-{st['id'][-8:]}"[:63]
        return {"endpoint": base, "config": one, "model": one}

    def _tags(self, st: dict) -> list[dict]:
        return [{"Key": k, "Value": str(v)} for k, v in {"app": "captain", "project": st["project"], "env": st["env"],
                                                         "captain:stage": st["id"], **TAGS}.items()]

    CANT_STOP = "This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."
    CANT_LOG = ("The serving role can't write the workspace bucket, so the endpoint can't keep its request log: no "
                "served-features check and no A/B readout.")

    def stage_preflight(self, st: dict, storage_uri: str) -> dict:
        """Ask IAM (simulate, nothing changes) whether the execution role may delete its own endpoint and write the
        workspace bucket. When IAM won't answer, say that it couldn't be checked rather than guess."""
        role = self.config["execution_role_arn"]
        acct = role.split(":")[4] if role.count(":") >= 5 else "*"
        name = self._names(st)["endpoint"]
        bucket, _, prefix = storage_uri.split("://", 1)[-1].partition("/")
        ask = {"sagemaker:DeleteEndpoint": f"arn:aws:sagemaker:{self.region}:{acct}:endpoint/{name.lower()}",
               "s3:PutObject": f"arn:aws:s3:::{bucket}/{prefix.strip('/') + '/' if prefix.strip('/') else ''}staging/preflight"}
        got = {}
        try:
            iam = self._client("iam")
            for action, arn in ask.items():
                r = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=[action], ResourceArns=[arn])
                got[action] = any(x.get("EvalDecision") == "allowed" for x in r.get("EvaluationResults", []))
        except Exception as e:  # noqa: BLE001
            return {"checked": False, "can_delete": None, "can_write": None, "warnings": [],
                    "note": "Captain couldn't check whether the serving role can stop its own endpoint or write the "
                            f"workspace bucket ({_aws_sentence(e)[:160]}). If it can't, the endpoint keeps billing "
                            "until you stop it from Captain."}
        warnings = ([] if got["sagemaker:DeleteEndpoint"] else [self.CANT_STOP]) + ([] if got["s3:PutObject"] else [self.CANT_LOG])
        return {"checked": True, "can_delete": got["sagemaker:DeleteEndpoint"], "can_write": got["s3:PutObject"],
                "warnings": warnings, "note": None}

    def stage_plan(self, st: dict) -> list[dict]:
        c, n, tags = self.config, self._names(st), self._tags(st)
        env = dict(st.get("container_env") or {})
        if env:
            env.update({"CST_STAGING_ENDPOINT": n["endpoint"], "CST_STAGING_REGION": self.region or ""})
        update = any(r["type"] == "endpoint" and r["name"] == n["endpoint"] for r in st.get("existing") or [])
        return [
            {"service": "sagemaker", "region": self.region, "call": "create_model", "args": {
                "ModelName": n["model"], "ExecutionRoleArn": c["execution_role_arn"], "Tags": tags,
                "PrimaryContainer": {"Image": st.get("image") or c["image"], "Environment": env}}},
            {"service": "sagemaker", "region": self.region, "call": "create_endpoint_config", "args": {
                "EndpointConfigName": n["config"], "Tags": tags, "ProductionVariants": [{
                    "VariantName": "main", "ModelName": n["model"], "InstanceType": c.get("instance_type") or "ml.m5.large",
                    "InitialInstanceCount": int(c.get("instance_count") or 1)}]}},
            {"service": "sagemaker", "region": self.region, "call": "update_endpoint" if update else "create_endpoint",
             "args": {"EndpointName": n["endpoint"], "EndpointConfigName": n["config"], **({} if update else {"Tags": tags})}},
        ]

    def stage(self, st: dict, progress=None) -> dict:
        import time

        say = progress or (lambda _t: None)
        sm = self._client("sagemaker")
        plan, n = self.stage_plan(st), self._names(st)
        kinds = {"create_model": ("model", n["model"]), "create_endpoint_config": ("endpoint-config", n["config"]),
                 "create_endpoint": ("endpoint", n["endpoint"])}
        made: list[dict] = []
        old = [r for r in st.get("existing") or [] if r["type"] != "endpoint"]
        updating = plan[-1]["call"] == "update_endpoint"
        try:
            for step in plan:
                say({"create_model": "Registering the model with SageMaker", "create_endpoint_config": "Describing the endpoint",
                     "create_endpoint": "Starting the endpoint (this takes 4 to 10 minutes)",
                     "update_endpoint": "Moving the endpoint to the new version (this takes 4 to 10 minutes)"}[step["call"]])
                getattr(sm, step["call"])(**step["args"])
                if step["call"] in kinds:
                    made.append({"type": kinds[step["call"]][0], "name": kinds[step["call"]][1], "region": self.region})
            deadline = time.time() + self.WAIT_S
            while True:
                d = sm.describe_endpoint(EndpointName=n["endpoint"])
                s = d["EndpointStatus"]
                if s == "InService" and d.get("EndpointConfigName") == n["config"]:
                    break
                if s in ("Failed", "OutOfService") or (s == "InService" and updating and time.time() > deadline):
                    raise ProviderError(f"SageMaker could not start the endpoint: {d.get('FailureReason') or s}")
                if time.time() > deadline:
                    raise ProviderError(f"the endpoint was still {s} after {self.WAIT_S // 60} minutes")
                time.sleep(self.POLL_S)
        except BaseException as e:
            say("Removing what was created, because the endpoint did not start")
            # A failed update leaves the endpoint on its old version: keep it and its old model, remove only the new parts.
            left = self.unstage(made)
            if updating:
                left = left + [r for r in st.get("existing") or []]
            err = ProviderError(_aws_sentence(e)) if not isinstance(e, ProviderError) and isinstance(e, Exception) else e
            err.left_behind = left
            if err is e:
                raise
            raise err from None
        if updating and old:
            self.unstage(old)
        ep = {"type": "endpoint", "name": n["endpoint"], "region": self.region}
        return {"endpoint": {"name": n["endpoint"], "region": self.region},
                "resources": [ep] + [r for r in made if r["type"] != "endpoint"]}

    def unstage(self, resources: list[dict]) -> list[dict]:
        """Delete these (endpoint first, then its config and model). Returns what is still there afterwards."""
        import time

        sm = self._client("sagemaker")
        order = {"endpoint": 0, "endpoint-config": 1, "model": 2}
        calls = {"endpoint": ("delete_endpoint", "EndpointName"), "endpoint-config": ("delete_endpoint_config", "EndpointConfigName"),
                 "model": ("delete_model", "ModelName")}
        left = []
        for r in sorted(resources, key=lambda r: order.get(r["type"], 9)):
            call, arg = calls[r["type"]]
            try:
                getattr(sm, call)(**{arg: r["name"]})
            except Exception as e:  # noqa: BLE001
                if not _missing(e):
                    left.append({**r, "error": _aws_sentence(e)[:300]})
        # An endpoint takes a little while to go. Wait for it, so "removed" means removed.
        for r in resources:
            if r["type"] != "endpoint" or any(x["name"] == r["name"] and x["type"] == "endpoint" for x in left):
                continue
            for _ in range(60):
                try:
                    sm.describe_endpoint(EndpointName=r["name"])
                except Exception as e:  # noqa: BLE001
                    if _missing(e):
                        break
                time.sleep(min(self.POLL_S, 5))
            else:
                left.append({**r, "error": "SageMaker is still deleting it"})
        # The endpoint's own log group goes with it. Best effort: a login without logs:DeleteLogGroup leaves it, named.
        for r in resources:
            if r["type"] == "endpoint" and not any(x["name"] == r["name"] and x["type"] == "endpoint" for x in left):
                try:
                    self._client("logs").delete_log_group(logGroupName=f"/aws/sagemaker/Endpoints/{r['name']}")
                except Exception as e:  # noqa: BLE001
                    if not _missing(e) and "ResourceNotFound" not in str(e):
                        left.append({"type": "log-group", "name": f"/aws/sagemaker/Endpoints/{r['name']}", "region": self.region, "error": _aws_sentence(e)[:200]})
        return left

    def staged_status(self, rec: dict) -> dict:
        name = (rec.get("endpoint") or {}).get("name")
        try:
            d = self._client("sagemaker").describe_endpoint(EndpointName=name)
        except Exception as e:  # noqa: BLE001
            if _missing(e):
                return {"state": "gone", "reason": "the endpoint no longer exists"}
            raise
        s = d["EndpointStatus"]
        if s == "InService":
            return {"state": "running"}
        if s in ("Creating", "Updating", "SystemUpdating", "RollingBack"):
            return {"state": "starting", "reason": s}
        if s == "Deleting":
            return {"state": "gone", "reason": "the endpoint is being deleted"}
        return {"state": "failed", "reason": d.get("FailureReason") or s}

    def invoke(self, rec: dict, payload: dict) -> dict:
        import json

        rt = self._client("sagemaker-runtime")
        try:
            r = rt.invoke_endpoint(EndpointName=(rec.get("endpoint") or {}).get("name"), ContentType="application/json",
                                   Accept="application/json", Body=json.dumps(payload).encode())
        except Exception as e:  # noqa: BLE001
            raise ProviderError("The staging endpoint refused the request: " + _aws_sentence(e)[:300]) from None
        body = r["Body"].read() if hasattr(r["Body"], "read") else r["Body"]
        return json.loads(body)

    def tagged(self, project: str, env: str) -> list[dict]:
        """Every SageMaker endpoint, endpoint config and model here that carries Captain's tags for this project."""
        sm = self._client("sagemaker")
        out = []
        listers = (("endpoint", "list_endpoints", "Endpoints", "EndpointName", "EndpointArn"),
                   ("endpoint-config", "list_endpoint_configs", "EndpointConfigs", "EndpointConfigName", "EndpointConfigArn"),
                   ("model", "list_models", "Models", "ModelName", "ModelArn"))
        for kind, call, key, name, arn in listers:
            token = None
            while True:
                r = getattr(sm, call)(**({"NextToken": token} if token else {}), MaxResults=100)
                for item in r.get(key, []):
                    tags = {t["Key"]: t["Value"] for t in sm.list_tags(ResourceArn=item[arn]).get("Tags", [])}
                    if tags.get("app") == "captain" and tags.get("project") == project and tags.get("env") == env:
                        out.append({"type": kind, "name": item[name], "region": self.region})
                token = r.get("NextToken")
                if not token:
                    break
        return out


def _job_name(job: Job) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "-", f"cst-{job.label}-{job.id[-6:]}")[:128]


def _missing(e: Exception) -> bool:
    """SageMaker says "not found" as a ValidationException with a sentence, never as its own error type."""
    msg = str(getattr(e, "response", {}).get("Error", {}).get("Message") or e).lower()
    return any(x in msg for x in ("could not find", "does not exist", "not found", "cannot find"))


def _aws_sentence(e: BaseException) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and resp.get("Error"):
        return f"{getattr(e, 'operation_name', 'the call')}: {resp['Error'].get('Message') or resp['Error'].get('Code')}"
    return str(e) or type(e).__name__
