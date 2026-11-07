"""Run SQL against a Connection and return an Arrow table.

Safety rules:
- read-only check before anything reaches the database (see guard.py);
- results over the connection's local row limit (default 1M) are refused on a laptop unless
  the caller passes allow_large=True. Large mining jobs belong on the remote runner.
"""
from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from dataclasses import dataclass

import pyarrow as pa

from ..model.entities import Connection
from ..workspace import render_secrets
from .guard import check_read_only


class TooLarge(Exception):
    pass


@dataclass
class Result:
    table: pa.Table
    truncated: bool
    elapsed_ms: int


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _tunnel(conn: Connection, url):
    """ssh -L through the bastion. Uses the user's own ssh config and agent."""
    port = _free_port()
    target = f"{url.host}:{url.port or _default_port(conn.engine)}"
    proc = subprocess.Popen(
        ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-p", str(conn.network.ssh_port),
         "-L", f"127.0.0.1:{port}:{target}", conn.network.ssh_host],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                raise ConnectionError(f"ssh tunnel failed: {proc.stderr.read().decode().strip()}")
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        else:
            raise ConnectionError("ssh tunnel did not open within 15s")
        yield url.set(host="127.0.0.1", port=port)
    finally:
        proc.terminate()


def _default_port(engine: str) -> int:
    return {"postgres": 5432, "postgresql": 5432, "redshift": 5439, "mysql": 3306, "trino": 8080}.get(engine, 5432)


@contextlib.contextmanager
def open_engine(conn: Connection):
    """A SQLAlchemy engine for a connection, with secrets filled in and the ssh tunnel open. Disposed on exit."""
    if conn.network.mode == "remote-runner":
        raise NotImplementedError("remote-runner connections run through `cst serve` on the runner")
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(render_secrets(conn.url))
    tunnel = _tunnel(conn, url) if conn.network.mode == "ssh-tunnel" else contextlib.nullcontext(url)
    with tunnel as real_url:
        eng = create_engine(real_url)
        try:
            yield eng
        finally:
            eng.dispose()


def run_sql(conn: Connection, sql: str, *, limit: int | None = None, allow_large: bool = False,
            params: dict | None = None) -> Result:
    if not conn.writable:
        check_read_only(sql, conn.engine)
    if conn.network.mode == "remote-runner":
        raise NotImplementedError(
            "remote-runner connections run through `cst serve` on the runner, which ships in build step 6"
        )
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(render_secrets(conn.url))
    cap = limit if limit is not None else (None if allow_large else conn.row_limit_local)
    tunnel = _tunnel(conn, url) if conn.network.mode == "ssh-tunnel" else contextlib.nullcontext(url)
    t0 = time.time()
    with tunnel as real_url:
        eng = create_engine(real_url)
        try:
            with eng.connect() as c:
                if not conn.writable and conn.engine in ("postgres", "postgresql", "redshift"):
                    # belt and braces: the database itself refuses writes in this transaction
                    c.exec_driver_sql("SET TRANSACTION READ ONLY")
                res = c.execution_options(stream_results=True).execute(text(sql), params or {})
                cols = list(res.keys())
                rows: list[tuple] = []
                truncated = False
                while True:
                    batch = res.fetchmany(10_000)
                    if not batch:
                        break
                    rows.extend(tuple(r) for r in batch)
                    if cap is not None and len(rows) > cap:
                        if limit is None:
                            raise TooLarge(
                                f"the query returned more than {cap:,} rows. That is too much for a laptop. "
                                "Run it on the remote runner, aggregate in SQL, or pass --local to override."
                            )
                        rows = rows[:cap]
                        truncated = True
                        break
        finally:
            eng.dispose()
    table = pa.Table.from_pylist([dict(zip(cols, r)) for r in rows]) if rows else pa.table({c: [] for c in cols})
    return Result(table=table, truncated=truncated, elapsed_ms=int((time.time() - t0) * 1000))
