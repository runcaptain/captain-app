"""Azure: Container Apps jobs and Azure Machine Learning command jobs.

Both ran real jobs on a real subscription on 2026-11-10, with the workspace in Azure Blob (az://): a demo seed, an
offline evaluation with its live stream, a retrain, logs and cancel. docs/compute-live-testing.md has what was run
and what it found. Serving on either is planned (compute.plan_serve) but not executable yet.

Known limits, from that run:
- Neither service reports a container's exit code, so a job's exit_code stays empty.
- Container Apps logs come from Log Analytics, a minute or two behind the job.
- An Azure ML job's started_at is when it was created, not when its container started. A cluster that scales
  from zero adds about three minutes before the first line.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import tempfile

from . import IMAGE_DEFAULT, ComputeProvider, Job, env_list, status
from .azure_serving import ContainerAppsServing


def _iso(t) -> str | None:
    if not t:
        return None
    if isinstance(t, str):
        return t
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


class _Azure(ComputeProvider):
    provider = "azure"
    # Container Apps jobs and Azure ML command jobs, each on a real subscription with an az:// workspace: submit,
    # status, logs, cancel, `demo seed --legacy`, `backtest run --compute` with its live stream, and a retrain.
    live_tested = "2026-11-10"

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


class ContainerAppsProvider(ContainerAppsServing, _Azure):
    """Jobs here, staging endpoints in azure_serving.py (the fields from `environment_id` down are staging's, and
    `cst compute setup serving azure` fills them)."""
    kind, label = "azure-container-apps", "Azure Container Apps (jobs and apps)"
    purposes = ["backtest", "serve"]
    fields = [
        {"name": "resource_group", "label": "Resource group", "required": True},
        {"name": "job", "label": "Job name (for offline evaluations)", "required": False},
        {"name": "app", "label": "Container app name (for serving)", "required": False},
        {"name": "image", "label": "Runner image", "required": True, "default": IMAGE_DEFAULT},
        {"name": "container", "label": "Container name in the job template", "required": False, "default": "runner"},
        {"name": "log_analytics_workspace_id", "label": "Log Analytics workspace ID (for logs)", "required": False},
        {"name": "environment_id", "label": "Container Apps environment a staging endpoint runs in", "required": False},
        {"name": "identity_id", "label": "Managed identity a staging endpoint runs as", "required": False},
        {"name": "identity_client_id", "label": "That identity's client ID", "required": False},
        {"name": "registry_server", "label": "Registry that holds the serving image", "required": False},
        {"name": "serving_storage", "label": "Storage container folder for the serving bundle and request log", "required": False},
        {"name": "name_prefix", "label": "Name prefix for staging apps", "required": False, "default": "captain"},
        {"name": "cpu", "label": "vCPU for one staging copy", "required": False, "default": "1"},
        {"name": "memory_mb", "label": "Memory for one staging copy, in MB", "required": False, "default": "2048"},
        {"name": "min_copies", "label": "Staging copies kept up while idle (0 for none)", "required": False, "default": "0"},
        {"name": "max_copies", "label": "Most staging copies at once", "required": False, "default": "1"}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        if not c.get("job"):
            raise ValueError("this Container Apps target has no job name, so it cannot run offline evaluations")
        return [{"service": "containerapps", "call": "jobs.begin_start", "args": {
            "resource_group_name": c["resource_group"], "job_name": c["job"],
            # args, not command: the image's entrypoint is cst
            "template": {"containers": [{"name": c.get("container") or "runner", "image": c["image"], "args": job.argv,
                                         "env": env_list(job.env)}]}}}]

    def _client(self):
        from azure.mgmt.appcontainers import ContainerAppsAPIClient  # type: ignore

        cred, sub = self._cred()
        return ContainerAppsAPIClient(cred, sub)

    def _inherit(self, cl, a: dict) -> dict:
        """The start template, with what the job's own container already has. A start template replaces the job's
        container whole: one that names only image, args and env runs at Container Apps' smallest size (0.5 CPU,
        1 GB) whatever the job was made with, and loses the job's own environment variables (a user-assigned
        identity's AZURE_CLIENT_ID, say). So the CPU and memory, and any variable Captain doesn't set, come from
        the job."""
        tmpl = getattr(cl.jobs.get(a["resource_group_name"], a["job_name"]), "template", None)
        out = []
        for c in a["template"]["containers"]:
            base = next((b for b in (getattr(tmpl, "containers", None) or []) if b.name == c["name"]), None)
            if base is None:
                raise ValueError(f"job {a['job_name']} has no container named {c['name']!r}. Set this target's "
                                 "container name to the one in the job.")
            mine = {e["name"] for e in c["env"]}
            keep = [e for e in (getattr(base, "env", None) or []) if e.name not in mine]
            c = {**c, "env": keep + c["env"]}
            if getattr(base, "resources", None) is not None:
                c["resources"] = base.resources
            out.append(c)
        return {**a["template"], "containers": out}

    def submit(self, job: Job) -> dict:
        a = self.plan(job)[0]["args"]
        cl = self._client()
        r = cl.jobs.begin_start(a["resource_group_name"], a["job_name"], self._inherit(cl, a)).result()
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
        # time_t is when the container printed the line. TimeGenerated is when Log Analytics took the batch in, so
        # lines of one batch share it and come back in any order. The cursor is time_t as text, at its full
        # precision: cut to seconds, the next read would drop the lines printed later in that second.
        q = (f"ContainerAppConsoleLogs_CL | where ContainerJobName_s == '{h['job']}' "
             f"and ContainerGroupName_s startswith '{h['execution']}'"
             " | extend t = column_ifexists('time_t', TimeGenerated)"
             + (f" | where t > todatetime('{since}')" if since else "")
             + " | order by t asc | project t, Log_s, cursor = tostring(t)")
        try:
            r = LogsQueryClient(cred).query_workspace(wid, q, timespan=dt.timedelta(days=7))
        except Exception as e:  # noqa: BLE001
            # The table is made by the environment's first log line: until then the query can't resolve it.
            if "ContainerAppConsoleLogs_CL" in str(e) and ("resolve" in str(e).lower() or "SEM0100" in str(e)):
                return {"lines": [], "next": since, "where": "Log Analytics",
                        "note": "no lines yet: Log Analytics shows a job's first lines a minute or two after it prints them"}
            raise
        lines, last = [], since
        # a partial answer (the query hit a limit) still carries the rows it found, in partial_data
        for t in getattr(r, "tables", None) or getattr(r, "partial_data", None) or []:
            for row in t.rows:
                lines.append({"ts": _iso(row[0]), "message": row[1]})
                last = row[2]
        return {"lines": lines, "next": last, "where": "Log Analytics"}

    def cancel(self, h: dict) -> dict:
        self._client().jobs.begin_stop_execution(h["resource_group"], h["job"], h["execution"]).result()
        s = self.status(h)
        if s["state"] == "stopped" and not s.get("finished_at"):
            # Container Apps records no end time for an execution that was stopped, so the stop is the end
            s["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return s

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
            raise ValueError("this Azure ML target has no compute cluster, so it cannot run offline evaluations")
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
        # azure-ai-ml hands the status back as an enum that prints as "JobStatus.QUEUED": use its value, "Queued"
        s = str(getattr(j.status, "value", j.status) or "NotStarted")
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
        s = self.status(h)
        if s["state"] == "stopped" and not s.get("finished_at"):
            s["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # the job has no end time
        return s

    def test(self) -> dict:
        try:
            ml = self._ml()
            if self.config.get("compute"):
                cc = ml.compute.get(self.config["compute"])
                return {"ok": True, "detail": f"signed in. Compute {cc.name} exists in workspace {self.config['workspace_name']}."}
            return {"ok": True, "detail": f"signed in to workspace {ml.workspaces.get(self.config['workspace_name']).name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:400]}
