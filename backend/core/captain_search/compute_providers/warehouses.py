"""Compute inside a data platform: Databricks Jobs and Snowflake Snowpark Container Services.

Each signs in its own way, with secrets kept in the keychain (the target stores only the secret's name):
- Databricks: a personal access token, a service principal through OAuth machine-to-machine, or a reused Databricks
  connection. Every call sends `Authorization: Bearer ...` and a `User-Agent: captain-search/<version>`.
- Snowflake: key-pair (a PEM private key, optionally with a passphrase), a password, or a reused Snowflake
  connection. Sessions name the application `Captain_Search`.

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


def _secret(config: dict, field: str) -> str | None:
    """A secret field's value: the keychain entry named by <field>_secret (written by the app), else none."""
    from ..workspace import get_secret

    name = config.get(f"{field}_secret")
    if not name:
        return None
    try:
        return get_secret(name)
    except Exception:  # noqa: BLE001
        raise PermissionError(f"the {field.replace('_', ' ')} isn't in this computer's keychain ({name}). "
                              "Add it again in Settings, Connected services.") from None


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("captain-search")
    except Exception:  # noqa: BLE001
        return "dev"


_TOKENS: dict[tuple, tuple[str, float]] = {}


def databricks_auth(ctx, config: dict) -> tuple[str, dict]:
    """(host, headers) for the Databricks REST API: Authorization plus a User-Agent naming Captain."""
    import time

    import httpx

    from ..workspace import render_secrets

    mode = config.get("auth") or ("connection" if config.get("connection") and not config.get("host") else "token")
    ua = {"User-Agent": f"captain-search/{_version()}"}
    if mode == "connection":
        conn = _conn(ctx, config.get("connection"))
        u = urlparse(render_secrets(conn.url))
        if not u.password:
            raise PermissionError(f"the connection {conn.name} has no token")
        return u.hostname, {"Authorization": f"Bearer {u.password}", **ua}
    host = str(config.get("host") or "").removeprefix("https://").strip("/")
    if not host:
        raise ValueError("give the Databricks workspace host")
    if mode == "token":
        tok = _secret(config, "token")
        if not tok:
            raise PermissionError("add an access token, or pick another way to sign in")
        return host, {"Authorization": f"Bearer {tok}", **ua}
    if mode == "service-principal":
        cid, sec = config.get("client_id"), _secret(config, "client_secret")
        if not cid or not sec:
            raise PermissionError("a service principal needs its client ID and secret")
        key = (host, cid)
        cached = _TOKENS.get(key)
        if cached and cached[1] > time.time() + 60:
            return host, {"Authorization": f"Bearer {cached[0]}", **ua}
        # OAuth machine-to-machine: https://docs.databricks.com/en/dev-tools/auth/oauth-m2m.html
        r = httpx.post(f"https://{host}/oidc/v1/token", data={"grant_type": "client_credentials", "scope": "all-apis"},
                       auth=(cid, sec), headers=ua, timeout=30)
        if r.status_code >= 400:
            raise PermissionError(f"Databricks refused the service principal ({r.status_code}). Check its client ID "
                                  "and secret, and that it may use the workspace.")
        body = r.json()
        _TOKENS[key] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return host, {"Authorization": f"Bearer {body['access_token']}", **ua}
    raise ValueError(f"unknown sign-in {mode!r}: use token, service-principal or connection")


class DatabricksJobsProvider(ComputeProvider):
    kind, label = "databricks-jobs", "Databricks Jobs"
    provider = "databricks"
    purposes = ["backtest"]
    live_tested = None
    fields = [
        {"name": "auth", "label": "Sign in with", "type": "choice", "required": True, "default": "token",
         "choices": ["token", "service-principal", "connection"],
         "help": "token: a personal access token. service-principal: OAuth with a client ID and secret (best for a "
                 "team). connection: reuse a Databricks database connection's host and token."},
        {"name": "host", "label": "Workspace host", "required": False,
         "placeholder": "dbc-1234abcd-5678.cloud.databricks.com", "help": "Not needed when signing in with a connection"},
        {"name": "token", "label": "Access token", "type": "secret", "required": False,
         "help": "For token sign-in. It needs the Jobs scope. Kept in your keychain only."},
        {"name": "client_id", "label": "Service principal client ID", "required": False},
        {"name": "client_secret", "label": "Service principal secret", "type": "secret", "required": False,
         "help": "For service-principal sign-in. Kept in your keychain only."},
        {"name": "connection", "label": "Databricks connection", "required": False, "kind": "connection:databricks",
         "help": "For connection sign-in"},
        {"name": "job_id", "label": "Job ID of a job that runs cst", "required": True},
    ]

    def _api(self):
        host, headers = databricks_auth(self.ctx, self.config)
        return f"https://{host}/api/2.1/jobs", headers

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
        {"name": "auth", "label": "Sign in with", "type": "choice", "required": True, "default": "key-pair",
         "choices": ["key-pair", "password", "connection"],
         "help": "key-pair: a private key registered on the user (Snowflake's recommendation for services). "
                 "password: the user's password. connection: reuse a Snowflake database connection."},
        {"name": "account", "label": "Account identifier", "required": False, "placeholder": "myorg-myaccount",
         "help": "Not needed when signing in with a connection"},
        {"name": "user", "label": "User", "required": False},
        {"name": "role", "label": "Role", "required": False, "help": "Needs USAGE on the compute pool and image repository"},
        {"name": "warehouse", "label": "Warehouse", "required": False},
        {"name": "private_key", "label": "Private key (PEM)", "type": "secret", "required": False,
         "help": "For key-pair sign-in. Kept in your keychain only."},
        {"name": "private_key_passphrase", "label": "Private key passphrase", "type": "secret", "required": False},
        {"name": "password", "label": "Password", "type": "secret", "required": False,
         "help": "For password sign-in. Kept in your keychain only."},
        {"name": "connection", "label": "Snowflake connection", "required": False, "kind": "connection:snowflake",
         "help": "For connection sign-in"},
        {"name": "compute_pool", "label": "Compute pool", "required": True},
        {"name": "image", "label": "Runner image in your image repository", "required": True,
         "placeholder": "/db/schema/repo/captain-runner:latest"},
    ]

    def _engine(self):
        """A SQLAlchemy engine signed in the configured way. Returns a context manager."""
        import contextlib

        from ..connectors.sql import open_engine

        cfg = self.config
        mode = cfg.get("auth") or ("connection" if cfg.get("connection") and not cfg.get("account") else "key-pair")
        if mode == "connection":
            return open_engine(_conn(self.ctx, cfg.get("connection")))
        from urllib.parse import quote, urlencode

        from sqlalchemy import create_engine

        if not cfg.get("account") or not cfg.get("user"):
            raise ValueError("give the Snowflake account identifier and user")
        q = {k: cfg[k] for k in ("warehouse", "role") if cfg.get(k)}
        q["application"] = "Captain_Search"
        connect_args = {}
        if mode == "password":
            pw = _secret(cfg, "password")
            if not pw:
                raise PermissionError("add the password, or pick another way to sign in")
            url = f"snowflake://{quote(cfg['user'])}:{quote(pw, safe='')}@{cfg['account']}/?{urlencode(q)}"
        elif mode == "key-pair":
            pem = _secret(cfg, "private_key")
            if not pem:
                raise PermissionError("add the private key, or pick another way to sign in")
            from cryptography.hazmat.primitives import serialization

            phrase = _secret(cfg, "private_key_passphrase")
            key = serialization.load_pem_private_key(pem.encode(), password=phrase.encode() if phrase else None)
            connect_args["private_key"] = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                            serialization.NoEncryption())
            url = f"snowflake://{quote(cfg['user'])}@{cfg['account']}/?{urlencode(q)}"
        else:
            raise ValueError(f"unknown sign-in {mode!r}: use key-pair, password or connection")

        @contextlib.contextmanager
        def eng():
            e = create_engine(url, connect_args=connect_args)
            try:
                yield e
            finally:
                e.dispose()

        return eng()

    def _sql(self, sql: str) -> list:
        from sqlalchemy import text

        with self._engine() as eng, eng.connect() as c:
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
