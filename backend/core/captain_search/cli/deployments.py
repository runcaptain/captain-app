"""`cst deploy <project>` and `cst deployments`: staged models from the terminal, named the way the pages are.

  cst deploy <project>              stage the current candidate. The last line printed is the address.
  cst deployments list              state, copies, where and since when, across projects
  cst deployments history <project> each version: number, when, who, model version
  cst deployments logs <project>    the endpoint's log lines
  cst deployments stop <project>    stop it and remove what Captain created for it
  cst deployments rollback <project> [version]

`cst projects stage`, `staging` and `unstage` do the same things and stay.
"""
from __future__ import annotations

import time
from typing import Optional

import typer
from typer.core import TyperGroup

from .main import S, app, ctx, emit, fail
from .more import deploy_app
from .projects import _do, _staging_errors, stage, unstage

deployments_app = typer.Typer(no_args_is_help=True, help="Staged models: list them, read their history and log, stop one or roll it back")
app.add_typer(deployments_app, name="deployments")


class _DeployGroup(TyperGroup):
    """`cst deploy <project>` stages that project. A first word that is one of the group's own commands (promote,
    rollback, status, history, env) still means that command, so a project with one of those names is staged with
    `cst deploy to-staging <project>`."""

    def resolve_command(self, ctx, args):
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["to-staging", *args]
        return super().resolve_command(ctx, args)


deploy_app.info.cls = _DeployGroup
deploy_app.info.help = ("Deploys. `cst deploy <project>` stages the project's candidate and prints its address. "
                        "The commands below are for environments with gates and rollback.")


@deploy_app.command("to-staging", hidden=True)
def deploy(name: str,
           candidate: Optional[str] = typer.Option(None, "--candidate", help="a candidate id or model version; default the newest judged candidate"),
           baseline: bool = typer.Option(False, "--baseline", help="stage the baseline itself"),
           env: str = typer.Option("staging", "--env"),
           target: Optional[str] = typer.Option(None, "--target", help="compute target, or `local` for this computer; default the workspace's serving target"),
           idle_minutes: Optional[float] = typer.Option(None, "--idle-minutes", help="stop by itself after this long with no requests; default 240"),
           allow_large: bool = typer.Option(False, "--allow-large", help="stage on this computer even when the model is over the size limit for it"),
           yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Stage the project's current candidate as a live endpoint. The last line printed is its address."""
    from .. import staging as SG

    stage(name, candidate, baseline, env, target, None, None, None, idle_minutes, allow_large, yes)
    if S.json or S.dry_run:
        return
    ep = ((SG.state(ctx(), name, env) or {}).get("endpoint")) or {}
    typer.echo(ep.get("url") or ep.get("name") or "")


@deployments_app.command("list")
def deployments_list(stopped: bool = typer.Option(False, "--stopped", help="the stopped ones"),
                     every: bool = typer.Option(False, "--all", help="live and stopped")):
    """Every staged model across projects: state, copies, where, and since when."""
    from ..staging import observe as OB

    d = _do(OB.deployments, ctx(), "all" if every else "stopped" if stopped else "live")
    lines = [f"{r['name'][:22]:22} {r['status'][:18]:18} copies {r['copies']}  {r['where'][:44]:44} {r['since_text']}"
             + (f"  ({r['requests_24h']:,} requests in 24 h)" if r.get("requests_24h") else "") for r in d["rows"]]
    emit(d, "\n".join(lines) if lines else d["text"])


@deployments_app.command("history")
def deployments_history(name: str):
    """Each version put on the project's endpoint, newest first: number, when, who, and the model version."""
    from ..staging import traffic as TF

    d = _do(TF.versions, ctx(), name)
    emit(d, "\n".join(f"v{r['n']:<3} {r['at'][:16].replace('T', ' ')}  {str(r['by'] or '')[:18]:18} {r['model_version']}"
                      + ("  Live" if r["live"] else "") for r in d["rows"]) or d["text"])


@deployments_app.command("logs")
def deployments_logs(name: str,
                     grep: Optional[str] = typer.Option(None, "--filter", help="only lines containing this"),
                     request: Optional[str] = typer.Option(None, "--request", help="only the lines of this request id"),
                     limit: int = typer.Option(200, "--limit", "-n", help="how many of the newest lines"),
                     follow: bool = typer.Option(False, "--follow", "-f", help="keep printing new lines until stopped")):
    """The endpoint's log lines, oldest first, with times to the millisecond (UTC)."""
    from ..staging import observe as OB

    c = ctx()
    d = _do(OB.logs, c, name, "staging", grep, limit, request)
    show = lambda rows: "\n".join(f"{x['ts']}  {x['text']}" for x in rows)  # noqa: E731
    if not follow or S.json:
        emit(d, show(d["rows"]) if d["rows"] else d["text"])
        return
    if d["rows"]:
        typer.echo(show(d["rows"]))
    last = d.get("last")
    while d.get("streaming"):
        time.sleep(3)
        d = _do(OB.logs, c, name, "staging", grep, 2000, request, last)
        if d["rows"]:
            typer.echo(show(d["rows"]))
            last = d["last"]
    typer.echo("The endpoint stopped, so the log is complete.", err=True)


@deployments_app.command("stop")
def deployments_stop(name: str, env: str = typer.Option("staging", "--env")):
    """Stop the project's endpoint and remove every resource Captain created for it."""
    unstage(name, env)


@deployments_app.command("rollback")
def deployments_rollback(name: str,
                         version: Optional[int] = typer.Argument(None, help="the version number from `cst deployments history`; default the one before the live one")):
    """Put an earlier version back on the project's endpoint."""
    from ..staging import traffic as TF

    c = ctx()
    v = _do(TF.versions, c, name)
    if version is None:
        older = [r for r in v["rows"] if not r["live"]]
        if not v["live"] or not older:
            fail("There is no earlier version to roll back to." if v["live"] else "Nothing is staged, so there is nothing to roll back.")
        version = older[0]["n"]
    row = next((r for r in v["rows"] if r["n"] == version), None)
    if row is None:
        fail(f"There is no version {version} for this endpoint. See cst deployments history {name}.")
    if S.dry_run:
        emit({"dry_run": True, "version": version, "model_version": row["model_version"]}, f"Would put version {version} ({row['model_version']}) back on the endpoint.")
        return
    try:
        r = TF.rollback_to(c, name, version)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, f"Rolled back to version {version} ({r['rolled_back_to']}).")
