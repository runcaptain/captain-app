"""AWS: ECS on Fargate, Batch, and SageMaker (serving only). Logs come from CloudWatch for both job kinds."""
from __future__ import annotations

import threading

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


_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()
CLIENT_TTL_S = 1800


def forget_clients() -> None:
    """Drop every kept client: after a login is changed or removed, and between tests."""
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


class _AWS(ComputeProvider):
    provider = "aws"

    def _session(self):
        from .. import cloud

        return cloud.aws_session(self.profile, self.region)

    def _client(self, name: str):
        """One client per login, region and service, kept for this process. Making a client for every call cost
        about 60 ms each time to build it, and a new connection to AWS on top (a fresh TLS handshake, and for a
        login that assumes a role, a fresh call to AWS to sign in). A query from the terminal showed 848 ms and
        480 ms round trips against 4 ms inside the endpoint. A kept client reuses the sign-in and the connection.
        Kept for 30 minutes at most, so a role's one-hour credentials are renewed well before they run out."""
        import time

        who = self.profile if isinstance(self.profile, str) or self.profile is None else (self.profile or {}).get("name")
        key = (who, self.region, name)
        with _CLIENTS_LOCK:
            hit = _CLIENTS.get(key)
            if hit and time.monotonic() - hit[0] < CLIENT_TTL_S:
                return hit[1]
        # The sign-in is kept too, so a second service (asking the endpoint, after starting it) does not sign in again.
        skey = (who, self.region, None)
        with _CLIENTS_LOCK:
            hit = _CLIENTS.get(skey)
            session = hit[1] if hit and time.monotonic() - hit[0] < CLIENT_TTL_S else None
        if session is None:
            session = self._session()  # outside the lock: it can call AWS to sign in
            with _CLIENTS_LOCK:
                _CLIENTS[skey] = (time.monotonic(), session)
        made = session.client(name)
        with _CLIENTS_LOCK:
            _CLIENTS[key] = (time.monotonic(), made)
        return made

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
    # Staging, 2026-11-11, same account and region, ml.t2.medium: the guided setup built the serving image in the
    # account (CodeBuild, 78 s), a starter ranker was staged beside its baseline (ready in 126 s), queried, checked,
    # shadowed, split with simulated shoppers, ramped, rolled back and unstaged, and the setup was removed.
    live_tested = "2026-11-11"
    can_logs = can_cancel = False
    fields = [
        {"name": "image", "label": "Serving image URI (ECR)", "required": True},
        {"name": "execution_role_arn", "label": "Execution role ARN", "required": True},
        {"name": "instance_type", "label": "Instance type", "required": False, "default": "ml.m5.large"},
        {"name": "instance_count", "label": "Instance count", "required": False, "default": "1"},
        {"name": "min_copies", "label": "Fewest copies of a deployed model", "required": False, "default": "1"},
        {"name": "max_copies", "label": "Most copies of a deployed model (more than the fewest turns on scaling with load)", "required": False, "default": "1"},
        {"name": "requests_per_copy", "label": "Requests a minute one copy takes before another is added", "required": False, "default": "3000"},
        {"name": "name_prefix", "label": "Name prefix for what a deploy makes", "required": False, "default": "captain"},
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

    WAIT_S = 1500  # a new endpoint took about 2 minutes when measured; SageMaker's own limit for a container to start is far longer
    POLL_S = 10

    def _names(self, st: dict) -> dict:
        import re

        from .serving import deployed_base, kept_name

        prefix = (self.config.get("name_prefix") or "captain").strip("-")
        base = kept_name(st, "endpoint") or re.sub(r"[^A-Za-z0-9-]", "-", deployed_base(prefix, st["project"], st["env"])).strip("-")[:50]
        one = f"{base}-{st['id'][-8:]}"[:63]
        return {"endpoint": base, "config": one, "model": one}

    def _tags(self, st: dict) -> list[dict]:
        return [{"Key": k, "Value": str(v)} for k, v in {"app": "captain", "project": st["project"], "env": st["env"],
                                                         "captain:stage": st["id"], **TAGS}.items()]

    CANT_STOP = "This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."
    CANT_LOG = ("The serving role can't write the workspace bucket, so the endpoint can't keep its request log: no "
                "served-features check.")

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
                    "InitialInstanceCount": int(st.get("copies_now") or self._copies()["min"])}]}},
            {"service": "sagemaker", "region": self.region, "call": "update_endpoint" if update else "create_endpoint",
             "args": {"EndpointName": n["endpoint"], "EndpointConfigName": n["config"], **({} if update else {"Tags": tags})}},
        ]

    # ---- more than one copy: SageMaker's own scaling on requests per copy -----------------------------
    #
    # Written against a stand-in. Not run on a real account yet.

    VARIANT = "main"
    DIMENSION = "sagemaker:variant:DesiredInstanceCount"
    MAX_COPIES = 10

    def _copies(self, scale: dict | None = None) -> dict:
        """{min, max, per_copy}: from the target's settings, with what was changed on the running endpoint on top."""
        c = self.config

        def num(name, default):
            try:
                return int(float(c.get(name) or default))
            except (TypeError, ValueError):
                raise ProviderError(f"{name} is a whole number") from None
        lo = max(1, num("min_copies", c.get("instance_count") or 1))
        out = {"min": lo, "max": max(lo, num("max_copies", lo)), "per_copy": max(1, num("requests_per_copy", 3000))}
        out.update({k: int(v) for k, v in (scale or {}).items() if k in out and v is not None})
        if out["max"] > self.MAX_COPIES:
            raise ProviderError(f"Captain runs at most {self.MAX_COPIES} copies of a deployed model on SageMaker.")
        return out

    def _resource(self, endpoint: str) -> str:
        return f"endpoint/{endpoint}/variant/{self.VARIANT}"

    def _scaling_off(self, endpoint: str) -> None:
        """Take SageMaker's scaling off an endpoint. It has to be off before the endpoint is moved or deleted."""
        try:
            self._client("application-autoscaling").deregister_scalable_target(
                ServiceNamespace="sagemaker", ResourceId=self._resource(endpoint), ScalableDimension=self.DIMENSION)
        except Exception as e:  # noqa: BLE001
            if not _missing(e) and "ObjectNotFound" not in str(e) and "No scalable target" not in str(e):
                raise

    def _scaling_on(self, endpoint: str, copies: dict) -> None:
        """Scale between the fewest and the most copies on requests a minute per copy. With fewest equal to most
        there is nothing to scale, and any scaling left from before is taken off."""
        if copies["max"] <= copies["min"]:
            self._scaling_off(endpoint)
            return
        aas = self._client("application-autoscaling")
        aas.register_scalable_target(ServiceNamespace="sagemaker", ResourceId=self._resource(endpoint), ScalableDimension=self.DIMENSION,
                                     MinCapacity=copies["min"], MaxCapacity=copies["max"])
        aas.put_scaling_policy(PolicyName=f"{endpoint}-requests-per-copy", ServiceNamespace="sagemaker", ResourceId=self._resource(endpoint),
                               ScalableDimension=self.DIMENSION, PolicyType="TargetTrackingScaling",
                               TargetTrackingScalingPolicyConfiguration={
                                   "TargetValue": float(copies["per_copy"]),
                                   "PredefinedMetricSpecification": {"PredefinedMetricType": "SageMakerVariantInvocationsPerInstance"},
                                   # Add a copy quickly, take one away slowly: a new copy takes minutes to arrive.
                                   "ScaleOutCooldown": 60, "ScaleInCooldown": 300})

    def _copies_up(self, endpoint: str) -> tuple[int | None, int | None]:
        """(running now, asked for) on this endpoint, or (None, None) when SageMaker doesn't say."""
        try:
            v = (self._client("sagemaker").describe_endpoint(EndpointName=endpoint).get("ProductionVariants") or [{}])[0]
            return v.get("CurrentInstanceCount"), v.get("DesiredInstanceCount")
        except Exception:  # noqa: BLE001
            return None, None

    def scale(self, rec: dict) -> dict:
        c = self._copies(rec.get("scale"))
        now, _want = self._copies_up((rec.get("endpoint") or {}).get("name"))
        up = "" if now is None else f" {now} up now."
        if c["max"] > c["min"]:
            text = (f"Keeps {c['min']} up at all times, and adds copies up to {c['max']} when one takes more than "
                    f"{c['per_copy']:,} requests a minute. A new copy takes a few minutes to arrive.{up}")
        else:
            text = f"Keeps {c['min']} up at all times. It never sleeps, and bills for each copy by the hour.{up}"
        return {"min_copies": c["min"], "max_copies": c["max"], "idle_seconds": None, "requests_per_copy": c["per_copy"], "copies_now": now,
                "can_set": ["min_copies", "max_copies"], "text": text}

    def set_scale(self, rec: dict, min_copies: int | None = None, max_copies: int | None = None, idle_seconds: int | None = None) -> dict:
        if idle_seconds is not None:
            raise ValueError("A SageMaker endpoint never sleeps, so there is no idle time to set. It stops by itself after the idle limit.")
        cur = dict(rec.get("scale") or {})
        cur.update({k: int(v) for k, v in (("min", min_copies), ("max", max_copies)) if v is not None})
        if cur.get("min") is not None and cur["min"] < 1:
            raise ValueError("A SageMaker endpoint keeps at least 1 copy up. Stop it to pay nothing.")
        c = self._copies(cur)
        if (min_copies is not None and max_copies is None and int(min_copies) > c["max"]) or c["min"] > c["max"]:
            raise ValueError("The fewest copies can't be more than the most.")
        name = (rec.get("endpoint") or {}).get("name")
        self._scaling_on(name, c)
        now, _want = self._copies_up(name)
        if now is not None and not c["min"] <= now <= c["max"]:
            # Outside the new bounds and nothing is scaling it there (or scaling is off): ask for it directly.
            self._client("sagemaker").update_endpoint_weights_and_capacities(
                EndpointName=name, DesiredWeightsAndCapacities=[{"VariantName": self.VARIANT, "DesiredInstanceCount": min(max(now, c["min"]), c["max"])}])
        return {**self.scale({**rec, "scale": cur}), "scale": cur}

    def stage(self, st: dict, progress=None) -> dict:
        import time

        from ..staging import timings as TM

        say = progress or (lambda _t: None)
        sm = self._client("sagemaker")
        copies = self._copies(st.get("scale"))
        if any(r["type"] == "endpoint" for r in st.get("existing") or []):
            # Moving a running endpoint: start the new version on as many copies as are up now, so a busy endpoint
            # is not cut back to its fewest in the middle of the switch.
            now, _want = self._copies_up(self._names(st)["endpoint"])
            st = {**st, "copies_now": min(max(now or copies["min"], copies["min"]), copies["max"])}
        else:
            st = {**st, "copies_now": copies["min"]}
        plan, n = self.stage_plan(st), self._names(st)
        kinds = {"create_model": ("model", n["model"]), "create_endpoint_config": ("endpoint-config", n["config"]),
                 "create_endpoint": ("endpoint", n["endpoint"])}
        made: list[dict] = []
        old = [r for r in st.get("existing") or [] if r["type"] != "endpoint"]
        updating = plan[-1]["call"] == "update_endpoint"
        try:
            for step in plan:
                say({"create_model": "Registering the model with SageMaker", "create_endpoint_config": "Describing the endpoint",
                     "create_endpoint": "Starting the endpoint" + TM.aside("aws", "start"),
                     # A move has not been timed on a real account yet. A new endpoint is the nearest measured thing.
                     "update_endpoint": "Moving the endpoint to the new version (a new endpoint takes "
                                        + (TM.step_words("aws", "start") or "a few minutes") + ". A move has not been timed yet)"}[step["call"]])
                if step["call"] == "update_endpoint":
                    self._scaling_off(n["endpoint"])  # SageMaker won't move an endpoint while its scaling is on
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
        if copies["max"] > copies["min"] or updating:
            say(f"Turning on scaling with load: {copies['min']} to {copies['max']} copies" if copies["max"] > copies["min"] else "Checking scaling is off")
            try:
                self._scaling_on(n["endpoint"], copies)
            except Exception as e:  # noqa: BLE001  the model is up and answering: say so, and say what is missing
                say(f"The model is running on {st['copies_now']} " + ("copy" if st["copies_now"] == 1 else "copies")
                    + f", but scaling with load could not be turned on: {_aws_sentence(e)[:300]}")
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
            if r["type"] == "endpoint":
                try:
                    self._scaling_off(r["name"])
                except Exception:  # noqa: BLE001  best effort: deleting the endpoint ends its scaling too
                    pass
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
        # SageMaker writes lineage records for every endpoint by itself (a context and an action named after it, which
        # inherit its tags). They cost nothing, but they are Captain's doing, so they go too. Best effort.
        gone = [r["name"] for r in resources if r["type"] == "endpoint" and not any(x["name"] == r["name"] and x["type"] == "endpoint" for x in left)]
        if gone:
            _drop_lineage(sm, lambda name: any(name.startswith(g + "-") and name.endswith("-aws-endpoint") for g in gone))
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
            raise ProviderError("The deployed model refused the request: " + _aws_sentence(e)[:300]) from None
        body = r["Body"].read() if hasattr(r["Body"], "read") else r["Body"]
        return json.loads(body)

    METRICS = (  # id, namespace, metric, statistic
        ("requests", "AWS/SageMaker", "Invocations", "Sum"),
        ("errors_4xx", "AWS/SageMaker", "Invocation4XXErrors", "Sum"),
        ("errors_5xx", "AWS/SageMaker", "Invocation5XXErrors", "Sum"),
        ("p50_us", "AWS/SageMaker", "ModelLatency", "p50"),
        ("p95_us", "AWS/SageMaker", "ModelLatency", "p95"),
        ("overhead_us", "AWS/SageMaker", "OverheadLatency", "p50"),
        ("cpu", "/aws/sagemaker/Endpoints", "CPUUtilization", "Average"),
        ("cpu_max", "/aws/sagemaker/Endpoints", "CPUUtilization", "Maximum"),
        ("memory", "/aws/sagemaker/Endpoints", "MemoryUtilization", "Average"),
        ("memory_max", "/aws/sagemaker/Endpoints", "MemoryUtilization", "Maximum"),
        # Each copy reports its CPU once a minute, so the count of reports in a minute is the copies that were up.
        ("reports", "/aws/sagemaker/Endpoints", "CPUUtilization", "SampleCount"),
    )

    def metrics(self, endpoint: str, start, end, period: int, only: tuple[str, ...] | None = None) -> dict:
        """CloudWatch's own numbers for one endpoint between two times, in buckets of `period` seconds: {id: {time:
        value}}. One read-only call. `only` limits it to some of METRICS. Nothing is sent to the endpoint."""
        cw = self._client("cloudwatch")
        dims = [{"Name": "EndpointName", "Value": endpoint}, {"Name": "VariantName", "Value": self.VARIANT}]
        want = [m for m in self.METRICS if only is None or m[0] in only]
        queries = [{"Id": i, "ReturnData": True, "MetricStat": {"Metric": {"Namespace": ns, "MetricName": name, "Dimensions": dims},
                                                                "Period": int(period), "Stat": stat}} for i, ns, name, stat in want]
        out: dict = {i: {} for i, *_ in want}
        token = None
        while True:
            r = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending",
                                   **({"NextToken": token} if token else {}))
            for res in r.get("MetricDataResults", []):
                for t, v in zip(res.get("Timestamps") or [], res.get("Values") or []):
                    out[res["Id"]][_iso(t)] = float(v)
            token = r.get("NextToken")
            if not token:
                return out

    def metric_names(self, endpoint: str) -> dict:
        """Which numbers CloudWatch holds for an endpoint, by where it keeps them: {"copies": [...], "requests":
        [...]}, each a list of {metric, dimensions}. Read-only. Asked only when the numbers expected are not there,
        to say why: nothing under that name at all, or numbers kept under other dimensions than Captain asks for."""
        cw = self._client("cloudwatch")
        out = {}
        for key, ns in (("copies", "/aws/sagemaker/Endpoints"), ("requests", "AWS/SageMaker")):
            got, token = [], None
            while True:
                r = cw.list_metrics(Namespace=ns, Dimensions=[{"Name": "EndpointName", "Value": endpoint}], **({"NextToken": token} if token else {}))
                got += [{"metric": m["MetricName"], "dimensions": {d["Name"]: d["Value"] for d in m.get("Dimensions") or []}} for m in r.get("Metrics", [])]
                token = r.get("NextToken")
                if not token or len(got) > 200:
                    break
            out[key] = got
        return out

    def found(self, prefix: str = "captain") -> list[dict]:
        """Every SageMaker endpoint here that Captain made, whatever project made it: found by Captain's tag, or by
        the name prefix. Reads only, and never calls a model. One row each: the name, the state as SageMaker reports
        it with its reason, the machine, the copies up, since when, and the project its tags name."""
        sm = self._client("sagemaker")
        start = prefix.strip("-") + "-"
        out, token = [], None
        states = {"InService": "running", "Creating": "starting", "Updating": "starting", "SystemUpdating": "starting",
                  "RollingBack": "starting", "Failed": "failed", "OutOfService": "failed", "UpdateRollbackFailed": "failed"}
        while True:
            r = sm.list_endpoints(**({"NextToken": token} if token else {}), MaxResults=100)
            for e in r.get("Endpoints", []):
                name = e["EndpointName"]
                try:
                    tags = {t["Key"]: t["Value"] for t in sm.list_tags(ResourceArn=e["EndpointArn"]).get("Tags", [])}
                except Exception:  # noqa: BLE001  a login that may not read tags still finds things by name
                    tags = {}
                if tags.get("app") != "captain" and not name.startswith(start):
                    continue
                try:
                    d = sm.describe_endpoint(EndpointName=name)
                except Exception as err:  # noqa: BLE001
                    if _missing(err):
                        continue
                    raise
                if d.get("EndpointStatus") == "Deleting":
                    continue
                v = (d.get("ProductionVariants") or [{}])[0]
                machine = None
                parts = [{"type": "endpoint", "name": name, "region": self.region}]
                try:
                    cfg = sm.describe_endpoint_config(EndpointConfigName=d["EndpointConfigName"])
                    pv = (cfg.get("ProductionVariants") or [{}])[0]
                    machine = pv.get("InstanceType")
                    parts.append({"type": "endpoint-config", "name": d["EndpointConfigName"], "region": self.region})
                    if pv.get("ModelName"):
                        parts.append({"type": "model", "name": pv["ModelName"], "region": self.region})
                except Exception:  # noqa: BLE001
                    pass
                out.append({"type": "endpoint", "name": name, "region": self.region, "resources": parts,
                            "state": states.get(d.get("EndpointStatus"), "starting"),
                            "reason": d.get("FailureReason") if states.get(d.get("EndpointStatus")) == "failed" else
                            (d.get("EndpointStatus") if states.get(d.get("EndpointStatus"), "starting") == "starting" else None),
                            "machine": machine, "copies_now": v.get("CurrentInstanceCount"), "copies_min": None, "copies_max": None,
                            "since": _iso(d.get("CreationTime")), "changed": _iso(d.get("LastModifiedTime")),
                            "project": tags.get("project"), "env": tags.get("env"), "stage": tags.get("captain:stage"),
                            "by": tags.get("captain:by"), "by_tag": tags.get("app") == "captain", "by_prefix": name.startswith(start)})
            token = r.get("NextToken")
            if not token:
                return out

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


def _drop_lineage(sm, mine, artifacts=None) -> int:
    """Delete SageMaker lineage contexts and actions whose name `mine` accepts, and artifacts whose source
    `artifacts` accepts, with their associations. Never raises: this is tidying, not the work itself."""
    n = 0

    def unlink(arn: str) -> None:
        for side in ("SourceArn", "DestinationArn"):
            try:
                for a in sm.list_associations(**{side: arn}).get("AssociationSummaries", []):
                    sm.delete_association(SourceArn=a["SourceArn"], DestinationArn=a["DestinationArn"])
            except Exception:  # noqa: BLE001
                pass

    try:
        for lister, key, name, arn, deleter in (("list_contexts", "ContextSummaries", "ContextName", "ContextArn", "delete_context"),
                                                ("list_actions", "ActionSummaries", "ActionName", "ActionArn", "delete_action")):
            for item in getattr(sm, lister)(MaxResults=100).get(key, []):
                if mine(item[name]):
                    unlink(item[arn])
                    getattr(sm, deleter)(**{name: item[name]})
                    n += 1
        if artifacts:
            for item in sm.list_artifacts(MaxResults=100).get("ArtifactSummaries", []):
                if artifacts(str((item.get("Source") or {}).get("SourceUri") or "")):
                    unlink(item["ArtifactArn"])
                    sm.delete_artifact(ArtifactArn=item["ArtifactArn"])
                    n += 1
    except Exception:  # noqa: BLE001
        pass
    return n
