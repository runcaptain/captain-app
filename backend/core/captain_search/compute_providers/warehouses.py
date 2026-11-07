"""Compute inside a data platform: Databricks Jobs and Snowflake Snowpark Container Services.

Both reuse a database connection the workspace already has (its host and token, or its Snowflake login), so a team
that keeps its data in Databricks or Snowflake can run backtests there without a separate cloud login.

- **Databricks:** triggers an existing Databricks job that runs `cst` (a Python wheel or notebook task with the
  captain-search package), passing the job's argv as its parameters, through the Jobs API 2.1. Captain doesn't
  create clusters: the team owns the job's compute and policy.
- **Snowflake:** runs the runner image as a job service in a compute pool (`EXECUTE JOB SERVICE ... ASYNC = TRUE`),
  with the image in the account's image repository. Logs come from SYSTEM$GET_SERVICE_LOGS.

Not tested on a real account (2026-11-07). The REST and SQL shapes follow the Databricks Jobs 2.1 and Snowpark
Container Services docs, and are unit-tested against fakes.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from . import ComputeProvider, Job, status


def _conn(ctx, name: str):
    if not name:
        raise ValueError("pick the connection this target runs through")
    return ctx.load_connection(name)


class DatabricksJobsProvider(ComputeProvider):
    kind, label = "databricks-jobs", "Databricks Jobs"
    provider = "databricks"
    purposes = ["backtest"]
    live_tested = None
    fields = [
        {"name": "connection", "label": "Databricks connection", "required": True, "kind": "connection:databricks"},
        {"name": "job_id", "label": "Job ID of a job that runs cst", "required": True},
    ]

    def _api(self):
        from ..workspace import render_secrets

        conn = _conn(self.ctx, self.config.get("connection"))
        u = urlparse(render_secrets(conn.url))
        return f"https://{u.hostname}/api/2.1/jobs", {"Authorization": f"Bearer {u.password or ''}"}

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import httpx

        base, h = self._api()
        r = httpx.request(method, f"{base}/{path}", json=body if method == "POST" else None,
                          params=body if method == "GET" else None, headers=h, timeout=60)
        if r.status_code in (401, 403):
            raise PermissionError("Databricks refused the login: the token needs access to Jobs.")
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"Databricks said: {str(msg)[:300]}")
        return r.json() if r.content else {}

    def plan(self, job: Job) -> list[dict]:
        return [{"service": "databricks", "call": "POST /api/2.1/jobs/run-now", "args": {
            "job_id": int(self.config["job_id"]), "python_params": job.argv,
            "job_parameters": {"cst_env": json.dumps(job.env)}}}]

    def submit(self, job: Job) -> dict:
        out = self._call("POST", "run-now", self.plan(job)[0]["args"])
        return {"run_id": out["run_id"]}

    def status(self, handle: dict) -> dict:
        r = self._call("GET", "runs/get", {"run_id": handle["run_id"]})
        st = r.get("state", {})
        life, result = st.get("life_cycle_state"), st.get("result_state")
        if life in ("PENDING", "QUEUED", "BLOCKED", "WAITING_FOR_RETRY"):
            return status("queued", st.get("state_message", ""))
        if life in ("RUNNING", "TERMINATING"):
            return status("running", st.get("state_message", ""))
        if result == "SUCCESS":
            return status("succeeded", "")
        if result in ("CANCELED", "CANCELLED"):
            return status("stopped", "the run was cancelled")
        return status("failed", st.get("state_message") or f"the run ended as {result or life}")

    def logs(self, handle: dict, since: str | None = None) -> dict:
        r = self._call("GET", "runs/get", {"run_id": handle["run_id"]})
        lines = []
        for t in r.get("tasks", []):
            out = self._call("GET", "runs/get-output", {"run_id": t["run_id"]})
            text = out.get("logs") or out.get("error") or ""
            lines += [{"ts": None, "message": ln} for ln in text.splitlines()]
        return {"lines": lines, "next": since, "where": "Databricks run output",
                "note": "Databricks returns each task's output once the task ends"}

    def cancel(self, handle: dict) -> dict:
        self._call("POST", "runs/cancel", {"run_id": handle["run_id"]})
        return status("running", "asked Databricks to cancel the run")

    def test(self) -> dict:
        try:
            j = self._call("GET", "get", {"job_id": int(self.config["job_id"])})
            return {"ok": True, "detail": f"job {j.get('settings', {}).get('name', self.config['job_id'])} is reachable"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}


def _ident(s: str) -> str:
    s = str(s or "")
    if not s.replace("_", "").replace(".", "").replace("$", "").isalnum():
        raise ValueError(f"{s!r} isn't a plain Snowflake name")
    return s


class SnowparkContainersProvider(ComputeProvider):
    kind, label = "snowflake-spcs", "Snowflake (Snowpark Container Services)"
    provider = "snowflake"
    purposes = ["backtest"]
    live_tested = None
    fields = [
        {"name": "connection", "label": "Snowflake connection", "required": True, "kind": "connection:snowflake"},
        {"name": "compute_pool", "label": "Compute pool", "required": True},
        {"name": "image", "label": "Runner image in your image repository", "required": True,
         "placeholder": "/db/schema/repo/captain-runner:latest"},
    ]

    def _sql(self, sql: str) -> list:
        from sqlalchemy import text

        from ..connectors.sql import open_engine

        with open_engine(_conn(self.ctx, self.config.get("connection"))) as eng, eng.connect() as c:
            return [tuple(r) for r in c.execute(text(sql))]

    def _spec(self, job: Job) -> str:
        spec = {"spec": {"containers": [{"name": "cst", "image": self.config["image"], "args": job.argv,
                                         "env": dict(job.env)}]}}
        return json.dumps(spec)

    def _name(self, job_or_handle) -> str:
        jid = job_or_handle["service"] if isinstance(job_or_handle, dict) else f"CST_{job_or_handle.id}"
        return _ident(jid.upper().replace("-", "_"))

    def plan(self, job: Job) -> list[dict]:
        sql = (f"EXECUTE JOB SERVICE IN COMPUTE POOL {_ident(self.config['compute_pool'])} NAME = {self._name(job)} "
               f"ASYNC = TRUE FROM SPECIFICATION $${self._spec(job)}$$")
        return [{"service": "snowflake", "call": "SQL", "args": {"sql": sql}}]

    def submit(self, job: Job) -> dict:
        self._sql(self.plan(job)[0]["args"]["sql"])
        return {"service": self._name(job)}

    def status(self, handle: dict) -> dict:
        rows = self._sql(f"SELECT SYSTEM$GET_SERVICE_STATUS('{self._name(handle)}')")
        try:
            st = (json.loads(rows[0][0]) or [{}])[0]
        except (ValueError, IndexError, TypeError):
            return status("queued", "Snowflake hasn't reported the job yet")
        s = str(st.get("status", "")).upper()
        if s in ("PENDING", "READY"):
            return status("queued" if s == "PENDING" else "running", st.get("message", ""))
        if s == "RUNNING":
            return status("running", st.get("message", ""))
        if s in ("DONE", "SUCCEEDED"):
            return status("succeeded", "")
        return status("failed", st.get("message") or f"the job service ended as {s or 'unknown'}")

    def logs(self, handle: dict, since: str | None = None) -> dict:
        rows = self._sql(f"SELECT SYSTEM$GET_SERVICE_LOGS('{self._name(handle)}', 0, 'cst', 1000)")
        text_ = rows[0][0] if rows else ""
        return {"lines": [{"ts": None, "message": ln} for ln in str(text_ or "").splitlines()], "next": since,
                "where": "Snowflake service logs"}

    def cancel(self, handle: dict) -> dict:
        self._sql(f"DROP SERVICE IF EXISTS {self._name(handle)}")
        return status("stopped", "dropped the job service")

    def test(self) -> dict:
        try:
            rows = self._sql(f"DESCRIBE COMPUTE POOL {_ident(self.config['compute_pool'])}")
            return {"ok": bool(rows), "detail": f"compute pool {self.config['compute_pool']} is reachable"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:300]}
