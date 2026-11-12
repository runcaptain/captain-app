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
                         version: Optional[int] = typer.Argument(None, help="the version number from `cst deployments history`; default the one before the live one"),
                         reason: Optional[str] = typer.Option(None, "--reason", help="why: kept with who rolled back, and required"),
                         auto: Optional[float] = typer.Option(None, "--auto", help="minutes after each deploy in which an unhealthy version is put back by itself; 0 turns it off"),
                         show: bool = typer.Option(False, "--history", help="list every roll back: when, who, from, to and why")):
    """Put an earlier version back on the project's endpoint, through the same deploy every version goes through.
    What is running keeps answering until the earlier version does."""
    from ..staging import rollback as RB

    c = ctx()
    if auto is not None:
        r = _do(RB.set_auto, c, name, auto)
        emit(r, r["text"])
        return
    if show:
        h = _do(RB.history, c, name)
        emit(h, "\n".join([h["text"], h["auto"]["text"]]
                          + [f"  {r['at'][:16].replace('T', ' ')}  {r['by']}: {r['from']} to {r['to']}. {r['reason']}" for r in h["rows"]]))
        return
    older = _do(RB.earlier, c, name)
    if not older:
        fail("There is no earlier version to roll back to.")
    row = older[0] if version is None else next((r for r in older if r["n"] == version), None)
    if row is None:
        fail(f"There is no earlier version {version} to go back to. See cst deployments history {name}.")
    if S.dry_run:
        emit({"dry_run": True, "version": row["n"], "model_version": row["model_version"]}, f"Would put version {row['n']} ({row['model_version']}) back on the endpoint.")
        return
    if not (reason or "").strip():
        fail('Say why: a roll back is recorded with its reason. Pass --reason "...".')
    try:
        r = RB.go(c, name, row["n"], reason)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, f"Rolled back to version {r['version']} ({r['rolled_back_to']}). {r['reason']}")


@deployments_app.command("health")
def deployments_health(name: str,
                       run: bool = typer.Option(False, "--check", help="run a check now and record it, instead of showing the last one"),
                       p95_ms: Optional[float] = typer.Option(None, "--p95-ms", help="set the response time target: 95 in 100 requests under this, at the endpoint"),
                       error_rate: Optional[float] = typer.Option(None, "--error-rate", help="set the share of requests that may fail, like 0.01 for 1%"),
                       history: int = typer.Option(0, "--history", help="also show this many recorded checks")):
    """A deployed model's health: is it answering, its error rate and response time against your target."""
    from ..staging import health as HL

    c = ctx()
    if (p95_ms is not None or error_rate is not None) and not S.dry_run:
        _do(HL.set_target, c, name, p95_ms, error_rate)
    h = _do(HL.check, c, name) if run and not S.dry_run else _do(HL.get, c, name)
    lines = [h.get("text") or "Not checked yet."]
    t = h.get("target") or {}
    lines.append("Target: " + (", ".join(x for x in [f"95 in 100 requests under {t['p95_ms']:g} ms" if t.get("p95_ms") is not None else None,
                                                    f"at most {t['error_rate']:.0%} failing" if t.get("error_rate") is not None else None] if x))
                 + ("" if t.get("set") else " (Captain's default: set your own with --p95-ms and --error-rate)."))
    if h.get("checked_at"):
        lines.append(f"Checked {h['checked_at']}." + (f" Last request {h['last_request_at']}." if h.get("last_request_at") else ""))
    if h.get("alert"):
        lines.append("Sent: " + h["alert"]["text"])
    rows = _do(HL.history, c, name, limit=history)["rows"] if history else []
    lines += [f"  {r.get('checked_at', '')}  {r.get('status', ''):10} {r.get('text', '')}" for r in rows]
    emit({**h, **({"history": rows} if history else {})}, "\n".join(lines))


@deployments_app.command("watch")
def deployments_watch(every: float = typer.Option(60.0, "--every", help="seconds between checks"),
                      once: bool = typer.Option(False, "--once", help="check every deployed model once and exit: for a cron"),
                      quiet: bool = typer.Option(False, "--quiet", help="print only when a status changes"),
                      system: bool = typer.Option(False, "--notify", help="also post a notification on this computer when a status changes")):
    """Check every deployed model on a schedule, record each result, and send a message when one turns unhealthy or
    recovers. Exits 1 with --once when any is unhealthy, so a cron or an agent can act on it."""
    from ..staging import health as HL

    c = ctx()
    while True:
        results = HL.watch_once(c, system=system)
        for r in results:
            if not quiet or r.get("changed"):
                typer.echo(f"{r.get('checked_at', '')}  {r['project']}: {r.get('text', '')}" + (f"  [sent: {r['alert']['text']}]" if r.get("alert") else ""))
        if once:
            if S.json:
                emit(results)
            if any(r.get("status") == "unhealthy" for r in results):
                raise typer.Exit(1)
            if not results and not S.json:
                typer.echo("Nothing is deployed.")
            return
        time.sleep(max(5.0, every))


@deployments_app.command("alerts")
def deployments_alerts(webhook: Optional[str] = typer.Option(None, "--webhook", help="an https:// address that takes a JSON body with `text` (Slack, PagerDuty, your own)"),
                       off: bool = typer.Option(False, "--off", help="stop sending")):
    """Where a message goes when a deployed model turns unhealthy or recovers. The address is never printed back."""
    from .. import settings as ST

    c = ctx()
    if (webhook is not None or off) and not S.dry_run:
        _do(ST.set_alerts, c, "" if off else webhook)
    on = bool((ST.get(c).get("alerts") or {}).get("webhook_url"))
    emit({"webhook_set": on}, "A webhook is set. Messages go there when a deployed model turns unhealthy or recovers." if on
         else "No webhook is set. Set one with --webhook https://...")
