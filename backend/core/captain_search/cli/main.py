"""cst: the Captain Search Tools command line.

Every command takes --json for machine output and every write takes --dry-run.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

import typer

from .. import index as idx
from ..connectors.guard import WriteRefused
from ..connectors.sql import TooLarge, run_sql
from ..data import datasets as ds
from ..model.entities import Connection, DatasetKind, Network, Provenance
from ..storage.backend import AlreadyExists, Conflict, NotFound
from .. import workspace as wsp

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Captain Search Tools")
ws_app = typer.Typer(no_args_is_help=True, help="Workspaces (one per bucket prefix)")
secret_app = typer.Typer(no_args_is_help=True, help="Secrets in the OS keychain")
conn_app = typer.Typer(no_args_is_help=True, help="Database connections")
data_app = typer.Typer(no_args_is_help=True, help="Versioned datasets")
app.add_typer(ws_app, name="workspace")
app.add_typer(secret_app, name="secret")
app.add_typer(conn_app, name="conn")
app.add_typer(data_app, name="data")
from .sync import app as sync_app
app.add_typer(sync_app, name="team")
from .team_server import app as team_server_app
app.add_typer(team_server_app, name="team-server")


@dataclass
class State:
    workspace: Optional[str] = None
    json: bool = False
    dry_run: bool = False


S = State()


@app.callback()
def _globals(
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace name"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what a write would do, write nothing"),
):
    S.workspace, S.json, S.dry_run = workspace, as_json, dry_run


def emit(obj, human: str | None = None) -> None:
    if S.json:
        typer.echo(json.dumps(obj, indent=2, default=str))
    else:
        typer.echo(human if human is not None else json.dumps(obj, indent=2, default=str))


def fail(msg: str, code: int = 1):
    if S.json:
        typer.echo(json.dumps({"error": msg}))
    else:
        typer.secho(f"error: {msg}", fg="red", err=True)
    raise typer.Exit(code)


def ctx():
    try:
        return wsp.open_context(S.workspace)
    except (RuntimeError, NotFound) as e:
        fail(str(e))


def table_text(table, n: int) -> str:
    import pyarrow as pa  # noqa: F401

    t = table.slice(0, n)
    cols = t.column_names
    data = [[("" if v is None else str(v)) for v in t.column(c).to_pylist()] for c in cols]
    widths = [min(40, max([len(c)] + [len(x) for x in col])) for c, col in zip(cols, data)]
    line = lambda vals: "  ".join(v[:w].ljust(w) for v, w in zip(vals, widths))
    out = [line(cols), line(["-" * w for w in widths])]
    out += [line([data[j][i] for j in range(len(cols))]) for i in range(t.num_rows)]
    return "\n".join(out)


# ---- init / workspace ----------------------------------------------------------------


@app.command()
def init(
    name: str = typer.Argument(..., help="Workspace name"),
    storage: str = typer.Option(..., "--storage", help="s3://bucket/prefix, gs://..., az://account/container/..., or a local path"),
    org: str = typer.Option("captain", "--org"),
    profile: Optional[str] = typer.Option(None, "--profile", help="cloud profile for the bucket (see `cst cloud`)"),
):
    """Create a workspace in a bucket (or attach to one that already exists)."""
    if S.dry_run:
        return emit({"would_create": name, "storage": storage}, f"would create workspace {name} at {storage}")
    c = wsp.init_workspace(name, storage, org, profile)
    emit(c.workspace.model_dump(mode="json"), f"workspace {name} ready at {c.store.uri}")


@ws_app.command("list")
def ws_list():
    cfg = wsp.load_config()
    rows = [{"name": n, "storage": w["storage"], "current": n == cfg.get("current")} for n, w in cfg.get("workspaces", {}).items()]
    emit(rows, "\n".join(f"{'*' if r['current'] else ' '} {r['name']}  {r['storage']}" for r in rows) or "no workspaces")


@ws_app.command("use")
def ws_use(name: str):
    cfg = wsp.load_config()
    if name not in cfg.get("workspaces", {}):
        fail(f"unknown workspace {name}")
    cfg["current"] = name
    wsp.save_config(cfg)
    emit({"current": name}, f"now using {name}")


@ws_app.command("show")
def ws_show():
    c = ctx()
    emit(c.workspace.model_dump(mode="json"),
         f"{c.workspace.name}  org={c.workspace.org}  storage={c.workspace.storage_uri}\n"
         "access control: bucket IAM (members list is informational in v1)")


# ---- secrets -------------------------------------------------------------------------


@secret_app.command("set")
def secret_set(name: str, value: Optional[str] = typer.Option(None, "--value", help="Omit to be prompted")):
    """Store a secret in the OS keychain. Reference it in a connection URL as {secret:NAME}."""
    if S.dry_run:
        return emit({"would_set": name})
    v = value if value is not None else typer.prompt(name, hide_input=True)
    wsp.set_secret(name, v)
    emit({"set": name}, f"stored {name} in the keychain")


@secret_app.command("list")
def secret_list():
    names = wsp.load_config().get("secret_names", [])
    emit(names, "\n".join(names) or "no secrets stored")


@secret_app.command("rm")
def secret_rm(name: str):
    if S.dry_run:
        return emit({"would_remove": name})
    wsp.delete_secret(name)
    emit({"removed": name}, f"removed {name}")


# ---- index ---------------------------------------------------------------------------


@app.command()
def sync():
    """Rebuild the local index from the bucket."""
    counts = idx.sync(ctx())
    emit(counts, "  ".join(f"{k}={v}" for k, v in counts.items()))


@app.command("index")
def index_cmd():
    """Print INDEX.md for the workspace, generated from the bucket."""
    c = ctx()
    md = idx.index_md(c)
    emit({"markdown": md}, md)


# ---- connections ---------------------------------------------------------------------


@conn_app.command("add")
def conn_add(
    name: str,
    engine: str = typer.Option(..., "--engine", help="postgres, mysql, snowflake, bigquery, redshift, athena, trino, duckdb"),
    url: str = typer.Option(..., "--url", help="SQLAlchemy URL. Put secrets as {secret:NAME}"),
    ssh: Optional[str] = typer.Option(None, "--ssh", help="user@bastion for an ssh tunnel"),
    ssh_port: int = typer.Option(22, "--ssh-port"),
    runner: Optional[str] = typer.Option(None, "--runner", help="URL of a remote runner inside the network"),
    writable: bool = typer.Option(False, "--writable", help="Allow writes. Off by default"),
    row_limit: int = typer.Option(1_000_000, "--row-limit", help="Most rows a laptop query may return"),
    replace: bool = typer.Option(False, "--replace"),
):
    """Add a shared connection. No secrets are stored in it."""
    if _inline_password(url):
        fail("the URL contains an inline password. Store it with `cst secret set NAME` and write {secret:NAME} instead.")
    c = ctx()
    mode = "remote-runner" if runner else ("ssh-tunnel" if ssh else "direct")
    conn = Connection(org=c.workspace.org, workspace=c.workspace.name, created_by=wsp.whoami(), name=wsp.check_name(name),
                      engine=engine, url=url, writable=writable, row_limit_local=row_limit,
                      network=Network(mode=mode, ssh_host=ssh, ssh_port=ssh_port, runner_url=runner))
    if S.dry_run:
        return emit(conn.model_dump(mode="json"))
    try:
        c.save_connection(conn, replace=replace)
    except AlreadyExists:
        fail(f"connection {name} exists. Pass --replace to change it.")
    except Conflict:
        fail(f"connection {name} changed while saving. Try again.")
    emit(conn.model_dump(mode="json"), f"added {name} ({engine}, {mode}{', WRITABLE' if writable else ', read-only'})")


def _inline_password(url: str) -> bool:
    from sqlalchemy.engine import make_url

    try:
        u = make_url(wsp.SECRET_RE.sub("CSTSECRET", url))
    except Exception:
        return False
    return bool(u.password) and str(u.password) != "CSTSECRET"


@conn_app.command("list")
def conn_list():
    c = ctx()
    import yaml

    rows = []
    for n in c.connections():
        raw = yaml.safe_load(c.store.get(c.conn_key(n)))
        if raw.get("deleted"):
            continue
        rows.append({"name": n, "engine": raw["engine"], "network": raw["network"]["mode"], "writable": raw["writable"]})
    emit(rows, "\n".join(f"{r['name']}  {r['engine']}  {r['network']}{'  WRITABLE' if r['writable'] else ''}" for r in rows) or "no connections")


@conn_app.command("test")
def conn_test(name: str):
    c = ctx()
    conn = _load_conn(c, name)
    from ..connectors.checks import verify

    r = verify(conn)
    if not r["ok"]:
        fail(f"{name}: {r['error']}")
    emit({"name": name, **r}, f"{name}: ok in {r['ms']} ms")


@conn_app.command("rm")
def conn_rm(name: str):
    c = ctx()
    _load_conn(c, name)
    if S.dry_run:
        return emit({"would_remove": name})
    c.delete_connection(name)
    emit({"removed": name}, f"removed {name}")


def _load_conn(c, name: str) -> Connection:
    import yaml

    try:
        raw = yaml.safe_load(c.store.get(c.conn_key(name)))
    except NotFound:
        fail(f"no connection named {name}")
    if raw.pop("deleted", False):
        fail(f"connection {name} was removed")
    return Connection.model_validate(raw)


# ---- sql -----------------------------------------------------------------------------


@app.command()
def sql(
    conn_name: str = typer.Argument(..., metavar="CONN"),
    execute: Optional[str] = typer.Option(None, "-e", "--execute", help="SQL text"),
    file: Optional[str] = typer.Option(None, "-f", "--file", help="SQL file"),
    save: Optional[str] = typer.Option(None, "--save", help="Snapshot the result as dataset NAME"),
    kind: DatasetKind = typer.Option(DatasetKind.generic, "--kind"),
    description: str = typer.Option("", "--description", "-m"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Keep only the first N rows (marks the dataset truncated)"),
    show: int = typer.Option(20, "--show", help="Rows to print"),
    local: bool = typer.Option(False, "--local", help="Allow results over the laptop row limit"),
):
    """Run read-only SQL on a connection. With --save, snapshot the result as a versioned dataset."""
    if bool(execute) == bool(file):
        fail("give exactly one of -e SQL or -f FILE")
    text = execute if execute else open(file).read()
    c = ctx()
    conn = _load_conn(c, conn_name)
    from ..metastore import record_query

    try:
        try:
            r = run_sql(conn, text, limit=limit, allow_large=local)
        except Exception:
            record_query(c, conn_name, text, source="cli", ok=False)  # query history shows failures too
            raise
        record_query(c, conn_name, text, rows=r.table.num_rows, ms=r.elapsed_ms, source="cli")
    except WriteRefused as e:
        fail(str(e))
    except TooLarge as e:
        fail(str(e))
    out = {"rows": r.table.num_rows, "truncated": r.truncated, "ms": r.elapsed_ms}
    human = [table_text(r.table, show), f"\n{r.table.num_rows:,} rows in {r.elapsed_ms} ms{' (truncated)' if r.truncated else ''}"]
    if save:
        prov = Provenance(source="sql", connection=conn_name, sql=text, file=file)
        try:
            m = ds.snapshot(c, wsp.check_name(save), r.table, kind=kind, provenance=prov,
                            description=description, truncated=r.truncated, dry_run=S.dry_run)
        except ValueError as e:
            fail(str(e))
        out["dataset"] = f"{m.name}@v{m.version}" if not S.dry_run else f"{m.name}@(next)"
        human.append(("would save " if S.dry_run else "saved ") + out["dataset"])
    if S.json:
        out["preview"] = r.table.slice(0, show).to_pylist()
    emit(out, "\n".join(human))


# ---- data ----------------------------------------------------------------------------


@data_app.command("list")
def data_list(kind: Optional[DatasetKind] = typer.Option(None, "--kind")):
    c = ctx()
    rows = []
    for n in c.registry.names("dataset"):
        m = c.registry.load("dataset", n)
        if kind and m.dataset_kind != kind:
            continue
        rows.append({"name": n, "latest": m.version, "kind": m.dataset_kind.value, "rows": m.row_count,
                     "by": m.created_by, "created": m.created_at.isoformat()})
    emit(rows, "\n".join(f"{r['name']}@v{r['latest']}  {r['kind']}  {r['rows']:,} rows  {r['by']}" for r in rows) or "no datasets")


@data_app.command("show")
def data_show(ref: str):
    c = ctx()
    m = _resolve(c, ref)
    emit(m.model_dump(mode="json"), _yaml(m.model_dump(mode="json")))


@data_app.command("head")
def data_head(ref: str, n: int = typer.Option(10, "-n")):
    c = ctx()
    m = _resolve(c, ref)
    t = ds.load_table(c, m)
    from ..data import reads as RD

    RD.record(c, m.name, m.version, "cli")  # "agent" when CST_AGENT is set
    emit(t.slice(0, n).to_pylist(), table_text(t, n))


@data_app.command("pull")
def data_pull(ref: str, out: str = typer.Option(".", "-o", help="Output directory")):
    c = ctx()
    m = _resolve(c, ref)
    from ..data import reads as RD

    RD.record(c, m.name, m.version, "cli")
    big = m.row_count > 1_000_000
    if big:
        fail(f"{ref} has {m.row_count:,} rows. Datasets that large stay in the cloud. Read it from a runner instead.")
    os.makedirs(out, exist_ok=True)
    paths = []
    for f in m.files:
        p = os.path.join(out, m.name.replace("/", "_") + f"-v{m.version}-" + os.path.basename(f.path))
        if not S.dry_run:
            with open(p, "wb") as fh:
                fh.write(c.registry.get_blob(f))
        paths.append(p)
    emit(paths, "\n".join(paths))


@data_app.command("snapshot")
def data_snapshot(
    path: str,
    name: str = typer.Option(..., "--name"),
    kind: DatasetKind = typer.Option(DatasetKind.generic, "--kind"),
    description: str = typer.Option("", "--description", "-m"),
):
    """Snapshot a local csv, tsv, parquet or jsonl file as a versioned dataset."""
    c = ctx()
    t = ds.read_file(path)
    m = ds.snapshot(c, wsp.check_name(name), t, kind=kind, provenance=Provenance(source="file", file=os.path.abspath(path)),
                    description=description, dry_run=S.dry_run)
    emit(m.model_dump(mode="json"), f"{'would save' if S.dry_run else 'saved'} {m.name}@v{m.version}  {m.row_count:,} rows")


@data_app.command("verify")
def data_verify(ref: str):
    """Check every file of a dataset version against its checksums."""
    c = ctx()
    m = _resolve(c, ref)
    problems = c.registry.verify("dataset", m.name, m.version)
    emit({"ref": f"{m.name}@v{m.version}", "ok": not problems, "problems": problems},
         "ok" if not problems else "\n".join(problems))
    if problems:
        raise typer.Exit(2)


@data_app.command("alias")
def data_alias(ref: str, alias: str):
    """Point an alias (e.g. prod, latest-good) at a dataset version. Compare-and-swap."""
    c = ctx()
    m = _resolve(c, ref)
    try:
        _, tag = c.registry.get_alias("dataset", m.name, alias)
    except NotFound:
        tag = None
    if S.dry_run:
        return emit({"would_alias": f"{m.name}@{alias}", "to": m.version})
    try:
        c.registry.set_alias("dataset", m.name, alias, m.version, tag, wsp.whoami())
    except Conflict:
        fail(f"alias {alias} changed while setting it. Re-run.")
    emit({"alias": f"{m.name}@{alias}", "version": m.version}, f"{m.name}@{alias} -> v{m.version}")


def _resolve(c, ref):
    try:
        m, _ = c.registry.resolve("dataset", ref)
    except (NotFound, ValueError) as e:
        fail(str(e))
    return m


def _yaml(obj) -> str:
    import yaml

    return yaml.safe_dump(obj, sort_keys=False)


from . import more  # noqa: E402,F401  registers the remaining commands


def main():  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
