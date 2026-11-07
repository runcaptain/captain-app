"""Read-only SQL guard. Connections are read-only unless marked writable.

The check fails closed: a statement that does not parse is refused on a read-only connection.
This is a second line of defense. The first is a read-only database role, which the docs ask
every team to use.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

DIALECTS = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "redshift": "redshift",
    "mysql": "mysql",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "athena": "athena",
    "trino": "trino",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "databricks": "databricks",
    "clickhouse": "clickhouse",
    "sqlserver": "tsql",
    "posthog": "clickhouse",
}

READ_ROOTS = (exp.Select, exp.SetOperation, exp.Describe, exp.Show)
WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,  # anything sqlglot could not model (GRANT, COPY, CALL, ...)
)


class WriteRefused(Exception):
    pass


def check_read_only(sql: str, engine: str) -> None:
    dialect = DIALECTS.get(engine)
    try:
        stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise WriteRefused(f"could not parse the SQL, so it is refused on a read-only connection: {e}") from None
    if not stmts:
        raise WriteRefused("empty SQL")
    for s in stmts:
        # EXPLAIN of a read is a read: allowed when what it explains passes the same check.
        if isinstance(s, exp.Command) and str(s.this).upper() == "EXPLAIN":
            inner = s.expression.name if isinstance(s.expression, exp.Literal) else str(s.expression or "")
            inner = re.sub(r"(?is)^\s*(analyze|verbose|format\s+\w+)\s+", "", inner)
            check_read_only(inner, engine)
            continue
        if not isinstance(s, READ_ROOTS):
            raise WriteRefused(f"{type(s).__name__} is not allowed on a read-only connection")
        bad = next(s.find_all(*WRITE_NODES), None)
        if bad is not None:
            raise WriteRefused(f"{type(bad).__name__} inside the query is not allowed on a read-only connection")
        if isinstance(s, exp.Select) and s.args.get("into"):
            raise WriteRefused("SELECT ... INTO writes a table and is not allowed on a read-only connection")
