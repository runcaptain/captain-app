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
        {"name": "instance_count", "label": "Instance count", "required": False, "default": "1"}]

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


def _job_name(job: Job) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "-", f"cst-{job.label}-{job.id[-6:]}")[:128]
