"""Google Cloud: Cloud Run jobs and Vertex AI custom jobs. Logs come from Cloud Logging for both.

Not tested on a real account (2026-11-06): Captain has no GCP service account for this yet. The SDK calls
match google-cloud-run 0.10, google-cloud-aiplatform 1.x and google-cloud-logging 3.x, and are unit-tested
against fakes with those shapes.
"""
from __future__ import annotations

from . import IMAGE_DEFAULT, ComputeProvider, Job, env_list, status


def _iso(t) -> str | None:
    if not t:
        return None
    try:
        return t.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return str(t)


class _GCP(ComputeProvider):
    provider = "gcp"
    live_tested = None

    def _creds(self):
        from .. import cloud

        return cloud.gcp_credentials(self.profile)

    def _logs(self, flt: str, since: str | None) -> dict:
        try:
            from google.cloud import logging_v2  # type: ignore
        except ImportError:
            return {"lines": [], "next": since, "note": "install google-cloud-logging to read Cloud Logging from here"}
        creds, project = self._creds()
        if since:
            flt += f' AND timestamp > "{since}"'
        cl = logging_v2.Client(project=project, credentials=creds)
        lines, last = [], since
        for e in cl.list_entries(filter_=flt, order_by=logging_v2.ASCENDING, max_results=5000):
            msg = e.payload if isinstance(e.payload, str) else (e.payload or {}).get("message", str(e.payload))
            ts = _iso(e.timestamp)
            lines.append({"ts": ts, "message": msg})
            last = e.timestamp.isoformat() if e.timestamp else last
        return {"lines": lines, "next": last, "where": "Cloud Logging"}


class CloudRunProvider(_GCP):
    kind, label = "gcp-cloud-run", "Google Cloud Run (jobs and services)"
    purposes = ["backtest", "serve"]
    fields = [
        {"name": "job", "label": "Job name (for offline evaluations)", "required": False},
        {"name": "service", "label": "Service name (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT}]

    def _job_path(self, project: str = "{project}") -> str:
        return f"projects/{project}/locations/{self.region}/jobs/{self.config['job']}"

    def plan(self, job: Job) -> list[dict]:
        if not self.config.get("job"):
            raise ValueError("this Cloud Run target has no job name, so it cannot run offline evaluations")
        return [{"service": "run", "region": self.region, "call": "JobsClient.run_job", "args": {
            "name": self._job_path(),
            "overrides": {"container_overrides": [{"args": job.argv, "env": env_list(job.env)}]}}}]

    def submit(self, job: Job) -> dict:
        from google.cloud import run_v2  # type: ignore

        a = dict(self.plan(job)[0]["args"])
        creds, project = self._creds()
        a["name"] = a["name"].replace("{project}", project)
        op = run_v2.JobsClient(credentials=creds).run_job(request=run_v2.RunJobRequest(**a))
        # The operation's metadata is the Execution it started: that name is what status, logs and cancel use.
        return {"execution": op.metadata.name, "operation": op.operation.name, "project": project, "region": self.region}

    def _executions(self):
        from google.cloud import run_v2  # type: ignore

        creds, _ = self._creds()
        return run_v2.ExecutionsClient(credentials=creds)

    def status(self, h: dict) -> dict:
        e = self._executions().get_execution(name=h["execution"])
        times = {"started_at": _iso(e.start_time), "finished_at": _iso(e.completion_time)}
        done = next((c for c in e.conditions if c.type_ == "Completed"), None)
        if e.completion_time:
            if e.cancelled_count:
                return status("stopped", "the execution was cancelled", **times)
            if e.failed_count:
                return status("failed", (done.message if done else "") or f"{e.failed_count} task(s) failed", **times)
            return status("succeeded", "every task finished", **times)
        if e.running_count:
            return status("running", "the container is running", **times)
        return status("queued", "Cloud Run is starting the execution", **times)

    def logs(self, h: dict, since: str | None = None) -> dict:
        name = h["execution"].rsplit("/", 1)[-1]
        return self._logs(f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{name}"', since)

    def cancel(self, h: dict) -> dict:
        self._executions().cancel_execution(name=h["execution"])
        return self.status(h)

    def test(self) -> dict:
        try:
            from google.cloud import run_v2  # type: ignore

            creds, project = self._creds()
            if not self.config.get("job"):
                return {"ok": True, "detail": f"signed in to project {project}. No job name, so this target only serves."}
            j = run_v2.JobsClient(credentials=creds).get_job(name=self._job_path(project))
            return {"ok": True, "detail": f"signed in to project {project}. Job {self.config['job']} exists ({j.uid})."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}


_VERTEX = {"JOB_STATE_QUEUED": "queued", "JOB_STATE_PENDING": "queued", "JOB_STATE_PAUSED": "queued",
           "JOB_STATE_RUNNING": "running", "JOB_STATE_UPDATING": "running", "JOB_STATE_CANCELLING": "running",
           "JOB_STATE_SUCCEEDED": "succeeded", "JOB_STATE_PARTIALLY_SUCCEEDED": "failed",
           "JOB_STATE_FAILED": "failed", "JOB_STATE_EXPIRED": "failed", "JOB_STATE_CANCELLED": "stopped"}


class VertexProvider(_GCP):
    kind, label = "gcp-vertex", "Google Vertex AI custom job"
    purposes = ["backtest"]
    fields = [
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT},
        {"name": "machine_type", "label": "Machine type", "required": False, "default": "n2-standard-8"},
        {"name": "service_account", "label": "Service account email", "required": False}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        return [{"service": "aiplatform", "region": self.region, "call": "JobServiceClient.create_custom_job", "args": {
            "parent": f"projects/{{project}}/locations/{self.region}",
            "custom_job": {"display_name": f"cst-{job.label}-{job.id[-6:]}"[:128],
                           "labels": {"captain-purpose": "cst-compute", "captain-job": job.id.lower()[:63]},
                           "job_spec": {
                "service_account": c.get("service_account") or None,
                "worker_pool_specs": [{"machine_spec": {"machine_type": c.get("machine_type") or "n2-standard-8"},
                                       "replica_count": 1,
                                       # args, not command: the image's entrypoint is cst
                                       "container_spec": {"image_uri": c["image"], "args": job.argv, "env": env_list(job.env)}}]}}}}]

    def _client(self):
        from google.cloud import aiplatform_v1  # type: ignore

        creds, project = self._creds()
        return aiplatform_v1.JobServiceClient(credentials=creds, client_options={
            "api_endpoint": f"{self.region}-aiplatform.googleapis.com"}), project

    def submit(self, job: Job) -> dict:
        cl, project = self._client()
        a = self.plan(job)[0]["args"]
        r = cl.create_custom_job(parent=a["parent"].replace("{project}", project), custom_job=a["custom_job"])
        return {"name": r.name, "project": project, "region": self.region}

    def status(self, h: dict) -> dict:
        cl, _ = self._client()
        j = cl.get_custom_job(name=h["name"])
        st = getattr(j.state, "name", str(j.state))
        state = _VERTEX.get(st, "running")
        msg = getattr(getattr(j, "error", None), "message", "") or ""
        reason = {"queued": "Vertex is placing the job", "running": "the container is running",
                  "succeeded": "the job succeeded", "stopped": "the job was cancelled"}.get(state, msg or st)
        if st == "JOB_STATE_CANCELLING":
            reason = "cancelling"
        return status(state, reason, started_at=_iso(j.start_time), finished_at=_iso(j.end_time))

    def logs(self, h: dict, since: str | None = None) -> dict:
        jid = h["name"].rsplit("/", 1)[-1]
        return self._logs(f'resource.type="ml_job" AND resource.labels.job_id="{jid}"', since)

    def cancel(self, h: dict) -> dict:
        cl, _ = self._client()
        cl.cancel_custom_job(name=h["name"])
        return self.status(h)

    def test(self) -> dict:
        try:
            cl, project = self._client()
            # A request, not keywords: the client's flattened arguments are parent only, so page_size= is a TypeError.
            next(iter(cl.list_custom_jobs(request={"parent": f"projects/{project}/locations/{self.region}",
                                                   "page_size": 1})), None)
            return {"ok": True, "detail": f"signed in to project {project}, Vertex answers in {self.region}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}
