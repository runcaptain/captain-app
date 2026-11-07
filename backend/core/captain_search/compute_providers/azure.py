"""Azure: Container Apps jobs and Azure Machine Learning command jobs.

Not tested on a real account (2026-11-06): Captain has no Azure service principal for this yet. The SDK calls
match azure-mgmt-appcontainers 3.x, azure-ai-ml 1.x and azure-monitor-query 1.x, and are unit-tested against
fakes with those shapes.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import tempfile

from . import IMAGE_DEFAULT, ComputeProvider, Job, env_list, status


def _iso(t) -> str | None:
    if not t:
        return None
    if isinstance(t, str):
        return t
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


class _Azure(ComputeProvider):
    provider = "azure"
    live_tested = None

    def _cred(self):
        from .. import cloud

        cred, conn, sub = cloud.azure_credential(self.profile)
        if cred is None:
            raise ValueError("this Azure login is a storage connection string, which cannot run compute. "
                             "Use a service principal or the default credential.")
        if not sub:
            raise ValueError("this Azure login has no subscription ID, which compute needs")
        return cred, sub


_ACA = {"Processing": "queued", "Running": "running", "Succeeded": "succeeded", "Failed": "failed",
        "Degraded": "failed", "Stopped": "stopped", "Unknown": "running"}


class ContainerAppsProvider(_Azure):
    kind, label = "azure-container-apps", "Azure Container Apps (jobs and apps)"
    purposes = ["backtest", "serve"]
    fields = [
        {"name": "resource_group", "label": "Resource group", "required": True},
        {"name": "job", "label": "Job name (for backtests)", "required": False},
        {"name": "app", "label": "Container app name (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT},
        {"name": "container", "label": "Container name in the job template", "required": False, "default": "runner"},
        {"name": "log_analytics_workspace_id", "label": "Log Analytics workspace ID (for logs)", "required": False}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        if not c.get("job"):
            raise ValueError("this Container Apps target has no job name, so it cannot run backtests")
        return [{"service": "containerapps", "call": "jobs.begin_start", "args": {
            "resource_group_name": c["resource_group"], "job_name": c["job"],
            # args, not command: the image's entrypoint is cst
            "template": {"containers": [{"name": c.get("container") or "runner", "image": c["image"], "args": job.argv,
                                         "env": env_list(job.env)}]}}}]

    def _client(self):
        from azure.mgmt.appcontainers import ContainerAppsAPIClient  # type: ignore

        cred, sub = self._cred()
        return ContainerAppsAPIClient(cred, sub)

    def submit(self, job: Job) -> dict:
        a = self.plan(job)[0]["args"]
        r = self._client().jobs.begin_start(a["resource_group_name"], a["job_name"], a["template"]).result()
        return {"resource_group": a["resource_group_name"], "job": a["job_name"], "execution": r.name, "id": getattr(r, "id", None)}

    def status(self, h: dict) -> dict:
        e = self._client().job_execution(h["resource_group"], h["job"], h["execution"])
        s = getattr(e, "status", None) or "Unknown"
        state = _ACA.get(s, "running")
        reason = {"queued": "Container Apps is starting the execution", "running": "the container is running",
                  "succeeded": "the execution succeeded", "failed": f"the execution ended {s.lower()}. The logs say why.",
                  "stopped": "the execution was stopped"}[state]
        if s == "Unknown":
            reason = "Container Apps reports the state as unknown"
        return status(state, reason, started_at=_iso(getattr(e, "start_time", None)), finished_at=_iso(getattr(e, "end_time", None)))

    def logs(self, h: dict, since: str | None = None) -> dict:
        wid = self.config.get("log_analytics_workspace_id")
        if not wid:
            return {"lines": [], "next": since, "note": "add the Log Analytics workspace ID to this target to read logs here"}
        from azure.monitor.query import LogsQueryClient  # type: ignore

        cred, _ = self._cred()
        q = (f"ContainerAppConsoleLogs_CL | where ContainerJobName_s == '{h['job']}' "
             f"and ContainerGroupName_s startswith '{h['execution']}'"
             + (f" | where TimeGenerated > datetime({since})" if since else "")
             + " | project TimeGenerated, Log_s | order by TimeGenerated asc")
        r = LogsQueryClient(cred).query_workspace(wid, q, timespan=dt.timedelta(days=7))
        lines, last = [], since
        for t in r.tables:
            for row in t.rows:
                lines.append({"ts": _iso(row[0]), "message": row[1]})
                last = _iso(row[0])
        return {"lines": lines, "next": last, "where": "Log Analytics"}

    def cancel(self, h: dict) -> dict:
        self._client().jobs.begin_stop_execution(h["resource_group"], h["job"], h["execution"]).result()
        return self.status(h)

    def test(self) -> dict:
        c = self.config
        try:
            cl = self._client()
            if not c.get("job"):
                return {"ok": True, "detail": "signed in. No job name, so this target only serves."}
            j = cl.jobs.get(c["resource_group"], c["job"])
            return {"ok": True, "detail": f"signed in. Job {j.name} exists in {c['resource_group']}."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}


_AML = {"NotStarted": "queued", "Queued": "queued", "Preparing": "queued", "Starting": "queued", "Provisioning": "queued",
        "Paused": "queued", "Running": "running", "Finalizing": "running", "CancelRequested": "running",
        "Completed": "succeeded", "Failed": "failed", "NotResponding": "failed", "Canceled": "stopped"}


class AzureMLProvider(_Azure):
    kind, label = "azure-ml", "Azure Machine Learning"
    purposes = ["backtest", "serve"]
    fields = [
        {"name": "resource_group", "label": "Resource group", "required": True},
        {"name": "workspace_name", "label": "AML workspace", "required": True},
        {"name": "compute", "label": "Compute cluster (for jobs)", "required": False},
        {"name": "endpoint", "label": "Online endpoint (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        if not c.get("compute"):
            raise ValueError("this Azure ML target has no compute cluster, so it cannot run backtests")
        import shlex

        return [{"service": "azureml", "call": "jobs.create_or_update(command)", "args": {
            "resource_group": c["resource_group"], "workspace_name": c["workspace_name"], "compute": c["compute"],
            "environment_image": c["image"], "command": "cst " + " ".join(shlex.quote(a) for a in job.argv),
            "environment_variables": job.env, "display_name": f"cst-{job.label}-{job.id[-6:]}",
            "tags": {"captain:purpose": "cst-compute", "captain:job": job.id}}}]

    def _ml(self):
        from azure.ai.ml import MLClient  # type: ignore

        cred, sub = self._cred()
        return MLClient(cred, sub, self.config["resource_group"], self.config["workspace_name"])

    def submit(self, job: Job) -> dict:
        from azure.ai.ml import command  # type: ignore
        from azure.ai.ml.entities import Environment as AMLEnv  # type: ignore

        a = self.plan(job)[0]["args"]
        j = command(command=a["command"], environment=AMLEnv(image=a["environment_image"]), compute=a["compute"],
                    environment_variables=a["environment_variables"], display_name=a["display_name"], tags=a["tags"])
        r = self._ml().jobs.create_or_update(j)
        return {"name": r.name, "studio_url": getattr(r, "studio_url", None)}

    def status(self, h: dict) -> dict:
        j = self._ml().jobs.get(h["name"])
        s = j.status or "NotStarted"
        state = _AML.get(s, "running")
        reason = {"queued": f"Azure ML is preparing the job ({s})", "running": f"the job is {s.lower()}",
                  "succeeded": "the job completed", "stopped": "the job was cancelled",
                  "failed": f"the job ended {s}. Its logs say why"
                            + (f": {h['studio_url']}" if h.get("studio_url") else ".")}[state]
        if s == "CancelRequested":
            reason = "cancelling"
        props = getattr(j, "creation_context", None)
        return status(state, reason, started_at=_iso(getattr(props, "created_at", None)))

    def logs(self, h: dict, since: str | None = None) -> dict:
        d = tempfile.mkdtemp(prefix="cst-aml-")
        self._ml().jobs.download(h["name"], download_path=d, all=False)
        files = sorted(glob.glob(os.path.join(d, "**", "user_logs", "std_log*.txt"), recursive=True))
        text = "".join(open(f, errors="replace").read() for f in files)
        start = int(since or 0)
        return {"lines": [{"ts": None, "message": x} for x in text[start:].splitlines()], "next": str(len(text)),
                "where": "Azure ML job logs"}

    def cancel(self, h: dict) -> dict:
        self._ml().jobs.begin_cancel(h["name"]).result()
        return self.status(h)

    def test(self) -> dict:
        try:
            ml = self._ml()
            if self.config.get("compute"):
                cc = ml.compute.get(self.config["compute"])
                return {"ok": True, "detail": f"signed in. Compute {cc.name} exists in workspace {self.config['workspace_name']}."}
            return {"ok": True, "detail": f"signed in to workspace {ml.workspaces.get(self.config['workspace_name']).name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}
