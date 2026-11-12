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
           on: Optional[str] = typer.Option(None, "--on", help="where to run it: local, aws, modal, gcp or azure. The first time on a cloud, what it needs there is set up as part of the deploy"),
           login: Optional[str] = typer.Option(None, "--login", help="with --on: which connected cloud login, when there is more than one"),
           remove: bool = typer.Option(False, "--remove", help="stop the model, and remove the first-time setup too when this deploy made it and nothing else uses it"),
           idle_minutes: Optional[float] = typer.Option(None, "--idle-minutes", help="stop by itself after this long with no requests; default 240"),
           allow_large: bool = typer.Option(False, "--allow-large", help="stage on this computer even when the model is over the size limit for it"),
           yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Stage the project's current candidate as a live endpoint. The last line printed is its address."""
    from .. import staging as SG

    if on or remove:
        return _deploy_on(name, on, login, candidate, baseline, env, allow_large, yes, remove)
    stage(name, candidate, baseline, env, target, None, None, None, idle_minutes, allow_large, yes)
    if S.json or S.dry_run:
        return
    ep = ((SG.state(ctx(), name, env) or {}).get("endpoint")) or {}
    typer.echo(ep.get("url") or ep.get("name") or "")


def _deploy_on(name, on, login, candidate, baseline, env, allow_large, yes, remove):
    """`cst deploy <project> --on <host>`: one plan, one confirm, then the first-time work and the model, step by
    step. The last line printed is the model's address."""
    import json as _json

    from ..staging import NeedsConfirm
    from ..staging import deploy as DP

    c = ctx()
    try:
        if remove:
            if S.dry_run:
                st = DP.status(c, name, env)
                return emit({**st, "dry_run": True}, "Would stop the model and remove: " + (", ".join(f"{x['resource']} {x['name']}" for x in st.get("exists_now") or []) or "nothing else"))
            r = DP.remove(c, name, env, progress=None if S.json else lambda t: typer.echo(t, err=True))
            return emit(r, r["text"])
        p = DP.plan(c, name, on, login, candidate, baseline, env, allow_large)
        lines = [p["text"]]
        for g in p["creates"]["groups"]:
            lines += [f"{g['title']}:"] + [f"  {r['resource']:28} {r['name']}" for r in g["items"]]
        if S.dry_run:
            return emit({**p, "dry_run": True}, "\n".join(lines))
        if p["needs_confirm"] and not yes:
            if S.json:
                fail("Confirm first with --yes. " + p["cost"]["text"])
            typer.echo("\n".join(lines))
            if not typer.confirm("Deploy it?"):
                raise typer.Exit(1)

        def hear(kind, **d):
            if S.json:
                return
            if kind == "step":
                typer.echo(f"[{d['index']}/{d['of']}] {d['title']}", err=True)
            elif kind == "line":
                typer.echo(f"      {d['text']}", err=True)

        r = DP.run(c, name, on, login, candidate, baseline, env, confirm=True, allow_large=allow_large, on_event=hear)
    except NeedsConfirm as e:
        fail(str(e))
    except _staging_errors() as e:
        st = DP.status(c, name, env)
        if st.get("status") == "failed" and st.get("failed_step"):
            left = ", ".join(f"{x['resource']} {x['name']}" for x in st.get("exists_now") or [])
            msg = (f"The deploy stopped at \"{st['failed_step']['title']}\": {e}\n"
                   + (f"What exists now: {left}.\n" if left else "Nothing was created.\n")
                   + f"Run the same command again to pick it up where it stopped" + (f", or `cst deploy {name} --remove` to take it away." if left else "."))
            if getattr(e, "policy", None):
                msg += "\n" + _json.dumps(e.policy, indent=2)
            if S.json:
                emit({"error": str(e), "deploy": st})
                raise typer.Exit(1)
            fail(msg)
        fail(str(e))
    emit(r, f"Deployed {r['model_version']} to {r['label']}.\n{r.get('address') or ''}")


UP = ("running", "asleep", "starting", "unknown")


@deployments_app.command("list")
def deployments_list(stopped: bool = typer.Option(False, "--stopped", help="the ones that are not running"),
                     every: bool = typer.Option(False, "--all", help="running and not"),
                     fresh: bool = typer.Option(False, "--fresh", help="ask the clouds now, not the answer from a few seconds ago")):
    """Every deployed model across projects and clouds, as each cloud reports it. Captain's records are corrected
    from the clouds first. Things found in a cloud that no project knows are listed too."""
    from ..staging import fleet as FL

    d = _do(FL.deployed, ctx(), fresh)
    rows = [r for r in d["rows"] if every or ((r["state"] in UP or r["link"] == "unlinked") != stopped)]
    lines = [f"{(r['project_name'] or r['name'] or '')[:22]:22} {r['state_label'][:24]:24} {r['where'][:40]:40} "
             f"{(r['model_version'] or 'not linked to a project')[:34]:34} {r['state_text']}" for r in rows]
    lines += [f"Could not ask {a['cloud_label']} ({a['login'] or 'no login'}): {a['error']}" for a in d["asked"] if not a["ok"]]
    emit({**d, "rows": rows}, "\n".join(lines) if lines else ("Nothing is deployed." if not d["rows"] else d["text"]))


@deployments_app.command("sync")
def deployments_sync():
    """Ask every connected cloud what is deployed, correct Captain's records from the answer, and print what
    differed: what was corrected, what a cloud has that no project knows, and which clouds could not be asked."""
    from ..staging import fleet as FL

    d = _do(FL.differences, ctx())
    emit(d, d["text"])


@deployments_app.command("show")
def deployments_show(name: str, span: str = typer.Option("1h", "--range", help="15m, 1h, 24h or 7d"),
                     env: str = typer.Option("staging", "--env")):
    """One project's deployment: its state as the cloud reports it, where it runs, its traffic and what it costs."""
    from ..staging import fleet as FL
    from ..staging import numbers as NB

    c = ctx()
    row = _do(FL.one, c, name, env, True)
    if row is None:
        fail(f"{name} has nothing deployed. Deploy it with: cst deploy {name} --on <aws|modal|gcp|azure|local>")
    n = _do(NB.numbers, c, name, env, span)
    s, cost = n["summary"], n.get("cost") or {}
    lines = [f"{row['project_name']}: {row['state_text']}",
             f"Model     {row['model_version'] or 'not known'}",
             f"Where     {row['where']}" + (f" ({row['name']})" if row.get("name") else ""),
             f"Since     {row['since'] or 'not known'}" + (f", by {row['by']}" if row.get("by") else ""),
             f"Traffic   {n['text']}"]
    if s.get("requests"):
        lines.append(f"          {s['per_minute']:g} requests a minute on average, {s['per_minute_peak']:g} at the busiest.")
    if s.get("cpu") is not None:
        lines.append(f"Machine   CPU {s['cpu']:g}% on average, {s['cpu_peak']:g}% at the most. Memory {s['memory']:g}%."
                     + (f" Copies: {s['copies_now']} now, {s['copies_peak']} at the most." if s.get("copies_now") else ""))
    lines.append(f"Cost      {cost.get('text') or 'not known'}")
    lines.append(f"Source    {n['source_text']}")
    lines += [f"Not read  {x}" for x in n["not_read"]] + [f"Corrected {x}" for x in row["corrected"]]
    emit({"deployment": row, "numbers": n}, "\n".join(lines))


@deployments_app.command("start")
def deployments_start(name: str, env: str = typer.Option("staging", "--env"),
                      yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Start a stopped deployment again: the same model version, in the same place."""
    from ..staging import deploy as DP
    from ..staging import fleet as FL

    c = ctx()
    row = _do(FL.one, c, name, env, True)
    if row is None:
        fail(f"{name} has nothing to start again. Deploy it with: cst deploy {name} --on <place>")
    if row["state"] in ("running", "asleep", "starting"):
        fail(f"{name} is already deployed: {row['state_text']}")
    if not row.get("model_version"):
        fail("Captain does not know which model version this ran, so it can't start it again. Deploy a version with: cst deploy " + name)
    if S.dry_run:
        emit({"dry_run": True, "model_version": row["model_version"], "where": row["where"]}, f"Would start {row['model_version']} again on {row['where']}.")
        return
    try:
        r = DP.run(c, name, row["cloud"], row["login"], candidate=row["model_version"], env=env, confirm=yes)
    except _staging_errors() as e:
        fail(str(e))
    FL.forget(c)
    emit(r, f"Started {row['model_version']} again on {row['where']}.")


@deployments_app.command("remove")
def deployments_remove(name: str = typer.Argument(..., help="a project, or the id of something found in a cloud with no project (from `cst deployments list --json`)"),
                       env: str = typer.Option("staging", "--env"),
                       yes: bool = typer.Option(False, "--yes", "-y", help="confirm: this cannot be undone")):
    """Remove a deployment from the cloud: stop the model, and take away the first-time setup too when this
    workspace made it and nothing else uses it. With the id of something found in a cloud, remove that."""
    from ..staging import deploy as DP
    from ..staging import fleet as FL

    c = ctx()
    if name.startswith("cloud:"):
        if S.dry_run:
            emit({"dry_run": True, "id": name}, f"Would remove {name.rsplit(':', 1)[-1]} from the cloud.")
            return
        try:
            r = FL.remove_found(c, name, confirm=yes)
        except ValueError as e:
            fail(str(e))
        emit(r, r["text"])
        return
    if not yes and not S.dry_run:
        fail(f"Confirm with --yes. This stops {name}'s model and removes what Captain made for it in the cloud. It cannot be undone.")
    if S.dry_run:
        emit({"dry_run": True, "project": name}, f"Would stop {name}'s model and remove what Captain made for it in the cloud.")
        return
    try:
        r = DP.remove(c, name, env)
    except _staging_errors() as e:
        fail(str(e))
    FL.forget(c)
    emit(r, r.get("text") or f"Removed {name}'s deployment.")


@deployments_app.command("link")
def deployments_link(found: str = typer.Argument(..., help="the id of something found in a cloud with no project (from `cst deployments list --json`)"),
                     project: str = typer.Argument(..., help="the project to give it to")):
    """Give a project something found in a cloud that no project knows, so it can be watched, stopped and removed
    from there."""
    from ..staging import fleet as FL

    try:
        r = FL.link(ctx(), found, project)
    except ValueError as e:
        fail(str(e))
    emit(r, r["text"])


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
