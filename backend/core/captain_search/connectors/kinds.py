"""Typed connection kinds: the fields a person fills in for each database, and how they become a connection.

The Captain app builds its "Connect a database" form from this list, so adding a kind here needs no app change.
Secret fields are never part of the connection: the app stores each one with POST /api/secrets under
`<CONNECTION>_<FIELD>` (upper case), and the connection's URL refers to it as {secret:NAME}, resolved only at run time.

Each kind names its SQLAlchemy driver package. `installed` says whether that driver is importable here, so the app can
say "this database's driver isn't installed" instead of failing at connect time.
"""
from __future__ import annotations

import importlib.util
import re
from urllib.parse import quote, urlencode, urlparse

F = dict  # a field: name, label, type (text, number, secret, path, choice), required, default, help, placeholder, choices


def _f(name, label, type="text", required=True, default=None, help="", placeholder="", choices=None):
    return {"name": name, "label": label, "type": type, "required": required, "default": default, "help": help,
            "placeholder": placeholder, "choices": choices}


HOST = _f("host", "Host", placeholder="db.example.com")
DB = _f("database", "Database")
USER = _f("user", "User")
PASSWORD = _f("password", "Password", "secret")

KINDS: list[dict] = [
    {"kind": "snowflake", "label": "Snowflake", "icon": "snowflake", "dialect": "snowflake",
     "driver": "snowflake-sqlalchemy", "module": "snowflake.sqlalchemy",
     "docs": "https://docs.snowflake.com/en/developer-guide/python-connector/sqlalchemy",
     "fields": [_f("account", "Account", help="The part before .snowflakecomputing.com, for example xy12345.us-west-2",
                   placeholder="xy12345.us-west-2"),
                USER, PASSWORD,
                _f("warehouse", "Warehouse", required=False), _f("database", "Database", required=False),
                _f("schema", "Schema", required=False), _f("role", "Role", required=False,
                                                            help="Use a read-only role for search data")]},
    {"kind": "databricks", "label": "Databricks", "icon": "databricks", "dialect": "databricks",
     "driver": "databricks-sqlalchemy", "module": "databricks.sqlalchemy",
     "docs": "https://docs.databricks.com/en/dev-tools/sqlalchemy.html",
     "fields": [_f("server_hostname", "Server hostname", placeholder="dbc-1234abcd-5678.cloud.databricks.com",
                   help="From your SQL warehouse's Connection details"),
                _f("http_path", "HTTP path", placeholder="/sql/1.0/warehouses/abcd1234"),
                _f("token", "Access token", "secret", help="A personal access token or service principal token"),
                _f("catalog", "Catalog", required=False, default="main"), _f("schema", "Schema", required=False, default="default")]},
    {"kind": "bigquery", "label": "BigQuery", "icon": "googlebigquery", "dialect": "bigquery",
     "driver": "sqlalchemy-bigquery", "module": "sqlalchemy_bigquery",
     "docs": "https://googleapis.dev/python/sqlalchemy-bigquery/latest/",
     "fields": [_f("project", "Project ID"), _f("dataset", "Dataset", required=False),
                _f("credentials_path", "Service account key file", "path", required=False,
                   help="Leave empty to use your Google Cloud login (gcloud auth application-default login)")]},
    {"kind": "redshift", "label": "Amazon Redshift", "icon": "amazonredshift", "dialect": "redshift",
     "driver": "psycopg", "module": "psycopg", "docs": "https://docs.aws.amazon.com/redshift/latest/mgmt/connecting-to-cluster.html",
     "fields": [HOST, _f("port", "Port", "number", default=5439), DB, USER, PASSWORD]},
    {"kind": "postgres", "label": "PostgreSQL", "icon": "postgresql", "dialect": "postgres",
     "driver": "psycopg", "module": "psycopg", "docs": "https://www.psycopg.org/psycopg3/docs/",
     "fields": [HOST, _f("port", "Port", "number", default=5432), DB, USER, PASSWORD,
                _f("sslmode", "SSL", "choice", required=False, default="prefer", choices=["disable", "prefer", "require"])]},
    {"kind": "mysql", "label": "MySQL", "icon": "mysql", "dialect": "mysql", "driver": "pymysql", "module": "pymysql",
     "docs": "https://pymysql.readthedocs.io/", "fields": [HOST, _f("port", "Port", "number", default=3306), DB, USER, PASSWORD]},
    {"kind": "trino", "label": "Trino or Presto", "icon": "trino", "dialect": "trino", "driver": "trino", "module": "trino.sqlalchemy",
     "docs": "https://trino.io/docs/current/client/python.html",
     "fields": [HOST, _f("port", "Port", "number", default=443), USER, _f("password", "Password", "secret", required=False),
                _f("catalog", "Catalog"), _f("schema", "Schema", required=False)]},
    {"kind": "athena", "label": "Amazon Athena", "icon": "amazonathena", "dialect": "athena", "driver": "pyathena",
     "module": "pyathena.sqlalchemy", "docs": "https://laughingman7743.github.io/PyAthena/sqlalchemy.html",
     "fields": [_f("region", "Region", default="us-west-1"), _f("s3_staging_dir", "Results location", placeholder="s3://bucket/athena-results/"),
                _f("schema", "Database", required=False, default="default"), _f("work_group", "Workgroup", required=False, default="primary")],
     "note": "Signs in with your AWS login (Cloud and compute)."},
    {"kind": "clickhouse", "label": "ClickHouse", "icon": "clickhouse", "dialect": "clickhouse",
     "driver": "clickhouse-sqlalchemy", "module": "clickhouse_sqlalchemy", "docs": "https://github.com/xzkostyan/clickhouse-sqlalchemy",
     "fields": [HOST, _f("port", "Port", "number", default=8443), DB, USER, PASSWORD,
                _f("https", "Encrypted (HTTPS)", "choice", required=False, default="yes", choices=["yes", "no"],
                   help="ClickHouse Cloud is always encrypted. A local server on port 8123 usually isn't")]},
    {"kind": "sqlserver", "label": "SQL Server", "icon": "microsoftsqlserver", "dialect": "tsql", "driver": "pymssql",
     "module": "pymssql", "docs": "https://docs.sqlalchemy.org/en/20/dialects/mssql.html",
     "fields": [HOST, _f("port", "Port", "number", default=1433), DB, USER, PASSWORD]},
    {"kind": "posthog", "label": "PostHog", "icon": "posthog", "dialect": "clickhouse", "driver": "(built in)",
     "module": "urllib", "docs": "https://posthog.com/docs/hogql",
     "fields": [_f("host", "PostHog address", default="https://us.posthog.com",
                   help="https://us.posthog.com, https://eu.posthog.com, or your own instance"),
                _f("project_id", "Project ID", help="The number in your project's URL"),
                _f("api_key", "Personal API key", "secret", help="Settings > Personal API keys, with the query:read scope")],
     "note": "Queries are HogQL, PostHog's SQL. Read-only."},
    {"kind": "ga4", "label": "Google Analytics 4", "icon": "googleanalytics", "dialect": None,
     "driver": "google-analytics-data", "module": "google.analytics.data_v1beta",
     "docs": "https://developers.google.com/analytics/devguides/reporting/data/v1",
     "fields": [_f("property_id", "Property ID", help="Admin > Property details. Digits only"),
                _f("credentials_path", "Service account key file", "path", required=False,
                   help="Leave empty to use your Google Cloud login. The account needs Viewer on the property")],
     "note": "Reports, not SQL: pick dimensions and metrics. If GA4 exports to BigQuery, connect BigQuery for raw events."},
    {"kind": "duckdb", "label": "DuckDB file", "icon": "duckdb", "dialect": "duckdb", "driver": "duckdb-engine",
     "module": "duckdb_engine", "docs": "https://duckdb.org/docs/guides/python/sql_on_pandas",
     "fields": [_f("path", "Database file", "path", placeholder="/path/to/data.duckdb")]},
    {"kind": "sqlite", "label": "SQLite file", "icon": "sqlite", "dialect": "sqlite", "driver": "(built in)", "module": "sqlite3",
     "docs": "https://docs.sqlalchemy.org/en/20/dialects/sqlite.html", "fields": [_f("path", "Database file", "path")]},
]
BY_KIND = {k["kind"]: k for k in KINDS}


def secret_name(conn: str, field: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", f"{conn}_{field}".upper())


def listing() -> list[dict]:
    out = []
    for k in KINDS:
        try:
            installed = importlib.util.find_spec(k["module"]) is not None
        except (ImportError, ValueError):
            installed = False
        out.append({**{x: k[x] for x in ("kind", "label", "icon", "driver", "docs", "fields")}, "note": k.get("note"),
                    "installed": installed})
    return out


def build(kind: str, conn: str, values: dict) -> dict:
    """values: the non-secret fields. Returns {engine, url, secrets} where secrets lists the names the app must store
    with POST /api/secrets before the connection works."""
    k = BY_KIND.get(kind)
    if k is None:
        raise ValueError(f"unknown database kind {kind!r}. Choose one of: {', '.join(BY_KIND)}")
    v, secrets = {}, {}
    for f in k["fields"]:
        if f["type"] == "secret":
            if f["required"] or values.get(f"{f['name']}_set"):
                secrets[f["name"]] = secret_name(conn, f["name"])
            continue
        val = values.get(f["name"])
        if val in (None, ""):
            val = f["default"]
        if f["required"] and val in (None, ""):
            raise ValueError(f"{f['label']} is needed.")
        if f["type"] == "number" and val not in (None, ""):
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"{f['label']} must be a number.") from None
        if f["type"] == "choice" and val not in (None, "") and val not in (f["choices"] or []):
            raise ValueError(f"{f['label']} must be one of {', '.join(f['choices'])}.")
        v[f["name"]] = val
    for f in k["fields"]:
        if f["type"] != "secret" and isinstance(v.get(f["name"]), str) and re.search(r"[{}\s]", v[f["name"]] or "") and f["type"] != "path":
            raise ValueError(f"{f['label']} can't contain spaces or braces.")
    q = lambda s: quote(str(s), safe="")
    sec = lambda field: "{secret:" + secrets[field] + "}" if field in secrets else ""
    auth = lambda user_field="user", pw_field="password": (
        q(v[user_field]) + (":" + sec(pw_field) if pw_field in secrets else "") + "@")
    params = lambda d: ("?" + urlencode({a: b for a, b in d.items() if b not in (None, "")})) if any(
        b not in (None, "") for b in d.values()) else ""
    if kind == "snowflake":
        path = "/".join(q(x) for x in (v.get("database"), v.get("schema")) if x)
        url = f"snowflake://{auth()}{v['account']}/{path}" + params({"warehouse": v.get("warehouse"), "role": v.get("role")})
    elif kind == "databricks":
        url = (f"databricks://token:{sec('token')}@{v['server_hostname']}"
               + params({"http_path": v["http_path"], "catalog": v.get("catalog"), "schema": v.get("schema")}))
    elif kind == "bigquery":
        url = f"bigquery://{v['project']}" + (f"/{v['dataset']}" if v.get("dataset") else "") + params(
            {"credentials_path": v.get("credentials_path")})
    elif kind in ("redshift", "postgres"):
        url = f"postgresql+psycopg://{auth()}{v['host']}:{v['port']}/{q(v['database'])}" + params(
            {"sslmode": "require" if kind == "redshift" else v.get("sslmode")})
    elif kind == "mysql":
        url = f"mysql+pymysql://{auth()}{v['host']}:{v['port']}/{q(v['database'])}"
    elif kind == "trino":
        path = "/".join(q(x) for x in (v.get("catalog"), v.get("schema")) if x)
        url = f"trino://{auth()}{v['host']}:{v['port']}/{path}" + params({"http_scheme": "https" if v["port"] == 443 else None})
    elif kind == "athena":
        url = f"awsathena+rest://@athena.{v['region']}.amazonaws.com:443/{q(v.get('schema') or 'default')}" + params(
            {"s3_staging_dir": v["s3_staging_dir"], "work_group": v.get("work_group")})
    elif kind == "clickhouse":
        url = f"clickhouse+http://{auth()}{v['host']}:{v['port']}/{q(v['database'])}" + params(
            {"protocol": "https" if (v.get("https") or "yes") == "yes" else None})
    elif kind == "sqlserver":
        url = f"mssql+pymssql://{auth()}{v['host']}:{v['port']}/{q(v['database'])}"
    elif kind == "posthog":
        host = urlparse(v["host"] if "://" in v["host"] else f"https://{v['host']}")
        url = f"posthog+{host.scheme}://key:{sec('api_key')}@{host.netloc}/{q(v['project_id'])}"
    elif kind == "ga4":
        url = f"ga4://{q(v['property_id'])}" + params({"credentials_path": v.get("credentials_path")})
    elif kind == "duckdb":
        url = f"duckdb:///{v['path']}"
    else:  # sqlite
        url = f"sqlite:///{v['path']}"
    engine = {"sqlserver": "sqlserver"}.get(kind, kind)
    return {"engine": engine, "url": url, "secrets": sorted(secrets.values()), "dialect": k["dialect"]}
