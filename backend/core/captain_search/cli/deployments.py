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

deployments_app = typer.Typer(no_args_is_help=True, help="Deployed models: list them as each cloud reports them, read their traffic, cost, history and log, stop, start, roll back or remove one")
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
           baseline: bool = typer.Option(False, "--baseline", help="deploy the baseline itself"),
           env: str = typer.Option("staging", "--env"),
           target: Optional[str] = typer.Option(None, "--target", help="compute target, or `local` for this computer; default the workspace's serving target"),
           on: Optional[str] = typer.Option(None, "--on", help="where to run it: local, aws, modal, gcp or azure. The first time on a cloud, what it needs there is set up as part of the deploy"),
           login: Optional[str] = typer.Option(None, "--login", help="with --on: which connected cloud login, when there is more than one"),
           remove: bool = typer.Option(False, "--remove", help="stop the model, and remove the first-time setup too when this deploy made it and nothing else uses it"),
           idle_minutes: Optional[float] = typer.Option(None, "--idle-minutes", help="stop by itself after this long with no requests; default 240"),
           allow_large: bool = typer.Option(False, "--allow-large", help="deploy on this computer even when the model is over the size limit for it"),
           session: Optional[str] = typer.Option(None, "--session", help="a Weights session (ws-...): save it as a new version and deploy that version, in one call. Needs --on"),
           note: str = typer.Option("", "--note", help="with --session: a note saying what the new version is"),
           yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Stage the project's current candidate as a live endpoint. The last line printed is its address.

    --candidate takes a model version, or a weights model version written weights:<model>@v<N> (a network that
    answers POST /predict). --session saves a Weights session as a new version and deploys it."""
    from .. import staging as SG

    if session and not on:
        fail("Say where to deploy the session's network: --on local, aws, modal, gcp or azure.")
    if on or remove:
        return _deploy_on(name, on, login, candidate, baseline, env, allow_large, yes, remove, session, note)
    stage(name, candidate, baseline, env, target, None, None, None, idle_minutes, allow_large, yes)
    if S.json or S.dry_run:
        return
    ep = ((SG.state(ctx(), name, env) or {}).get("endpoint")) or {}
    typer.echo(ep.get("url") or ep.get("name") or "")


def _deploy_on(name, on, login, candidate, baseline, env, allow_large, yes, remove, session=None, note=""):
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
        if session:
            # Planned for the version the session was opened on: nothing is saved until the place is confirmed.
            from ..staging import predict as PD

            smodel, sver = PD.session_base(c, session)
            candidate, baseline = PD.ref_of(smodel, sver), False
        p = DP.plan(c, name, on, login, candidate, baseline, env, allow_large)
        lines = [p["text"].replace(candidate, f"this session's edits to {smodel} v{sver}") if session else p["text"]]
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

        if session:
            r = PD.deploy_session(c, name, session, on, login, note=note, env=env, confirm=True, allow_large=allow_large, on_event=hear)
        else:
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
    saved = (r.get("saved") or {}).get("text")
    emit(r, (f"{saved}\n" if saved else "") + f"Deployed {r['model_version']} to {r['label']}.\n{r.get('address') or ''}")


@deployments_app.command("try")
def deployments_try(name: str,
                    send: bool = typer.Option(False, "--send", help="send the example request now, as test traffic"),
                    inputs: Optional[str] = typer.Option(None, "--inputs", help="for a model with named inputs: the inputs as JSON, such as '{\"x1\": 0.9, \"x2\": 0.1}' or '[0.9, 0.1]'. Sends it"),
                    query: Optional[str] = typer.Option(None, "--query", help="for a ranker: the search to send. Sends it"),
                    env: str = typer.Option("staging", "--env")):
    """What the deployed model takes and gives back, with a ready example request. With --send, --inputs or --query
    the request is sent as test traffic and the answer is printed."""
    import json as _json

    from ..staging import predict as PD

    c = ctx()
    if not (send or inputs is not None or query is not None):
        r = _do(PD.try_info, c, name, env)
        lines = [f"{r['model_version']} answers at {r['address']}", r["text"]]
        lines += [f"  in   {i['name']}: {i.get('meaning') or i.get('type')}" + ("".join(f"\n         as {a}" for a in i.get("accepts") or [])) for i in r["inputs"]]
        lines += [f"  out  {o['name']}: {o.get('meaning') or o.get('type')}" for o in r["outputs"]]
        lines += ["An example request, from " + r["example_from"] + ":", r["request"]]
        return emit(r, "\n".join(lines))
    body: dict = {}
    if inputs is not None:
        try:
            body["inputs"] = _json.loads(inputs)
        except ValueError:
            fail("--inputs must be JSON, such as '{\"x1\": 0.9, \"x2\": 0.1}' or '[0.9, 0.1]'.")
    if query is not None:
        body["query"] = query
    if S.dry_run:
        return emit({"dry_run": True, "would": "send one test request", "body": body}, "would send one test request to the deployed model")
    r = _do(PD.try_send, c, name, body, env)
    emit(r, r["text"])


def _weights_deploy(project: str = typer.Argument(..., help="the project that deploys it"),
                    on: str = typer.Option("local", "--on", help="where to run it: local, aws, modal, gcp or azure"),
                    login: Optional[str] = typer.Option(None, "--login", help="which connected cloud login, when there is more than one"),
                    note: str = typer.Option("", "--as", "--note", help="a note saying what this version is"),
                    session: Optional[str] = typer.Option(None, "--session", "-s", help="the session to save and deploy. Left out: the one opened last"),
                    env: str = typer.Option("staging", "--env"),
                    yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Save the session as a new version and deploy that version, in one call. The last line printed is the
    model's address. The deployment's record says which version it started from and which edits it carries, and
    `cst deployments rollback` returns to the version that ran before."""
    from .weights import _sid

    _deploy_on(project, on, login, None, False, env, False, yes, False, _sid(session), note)


def _register_weights_deploy() -> None:
    from .weights import weights_app

    weights_app.command("deploy")(_weights_deploy)


_register_weights_deploy()


UP = ("running", "asleep", "starting", "unknown")


@deployments_app.command("list")
def deployments_list(running: bool = typer.Option(False, "--running", help="only the ones that are running, asleep or starting"),
                     stopped: bool = typer.Option(False, "--stopped", help="only the ones that are not running"),
                     every: bool = typer.Option(False, "--all", help="every one, which is what it shows anyway"),
                     fresh: bool = typer.Option(False, "--fresh", help="ask the clouds now, not the answer from a few seconds ago")):
    """Every deployed model across projects and clouds, as each cloud reports it. Captain's records are corrected
    from the clouds first. Things found in a cloud that no project knows are listed too."""
    from ..staging import fleet as FL

    d = _do(FL.deployed, ctx(), fresh)
    # Every deployment by default, running first: one that is stopped is still a deployment a person may start
    # again or remove, and a list that counts it and does not show it is no list.
    up = lambda r: r["state"] in UP or r["link"] == "unlinked"  # noqa: E731
    rows = [r for r in d["rows"] if every or not (running or stopped) or (running and up(r)) or (stopped and not up(r))]
    line = lambda r: (f"{(r['project_name'] or r['name'] or '')[:22]:22} {r['state_label'][:24]:24} {r['where'][:40]:40} "  # noqa: E731
                      f"{(r.get('model_label') or r['model_version'] or 'not linked to a project')[:34]:34} {r['state_text']}")
    # What is in a cloud now, then what was stopped earlier, under its own heading: Captain only remembers those.
    now, earlier = [r for r in rows if r["in_cloud"]], [r for r in rows if not r["in_cloud"]]
    lines = [line(r) for r in now]
    # A deploy under way: its steps under its row, each with how long it took or is taking.
    for r in now:
        for st in (r.get("deploying") or {}).get("steps") or []:
            took = f"{st['seconds']:g} s" if st.get("seconds") is not None and st.get("status") in ("done", "failed") else ""
            lines.append(f"    {str(st.get('status') or ''):8} {st.get('title')}" + (f"  {took}" if took else "") + (f"  (usually {st['typical']})" if st.get("typical") and st.get("status") != "done" else ""))
    if earlier:
        lines += ([""] if lines else []) + ["Stopped earlier. Nothing of these is in a cloud or billing. Start one again with: cst deployments start <project>"]
        lines += [line(r) for r in earlier]
    lines += [f"Could not ask {a['cloud_label']} ({a['login'] or 'no login'}): {a['error']}" for a in d["asked"] if not a["ok"]]
    emit({**d, "rows": rows}, "\n".join(([d["text"]] if d["rows"] else []) + lines) if lines else d["text"])


@deployments_app.command("forget")
def deployments_forget(name: str, env: str = typer.Option("staging", "--env"),
                       yes: bool = typer.Option(False, "--yes", "-y", help="confirm: Captain's record of it is removed")):
    """Forget a model that was stopped earlier: remove Captain's own record of the deployment. Nothing in any cloud
    changes, because nothing of it is there."""
    from ..staging import fleet as FL

    if S.dry_run:
        emit({"dry_run": True, "project": name}, f"Would forget {name}'s stopped model. Nothing in the cloud would change.")
        return
    try:
        r = FL.forget_stopped(ctx(), name, env, confirm=yes)
    except ValueError as e:
        fail(str(e))
    emit(r, r["text"])


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
             f"Model     {row.get('model_label') or row['model_version'] or 'not known'}",
             f"Where     {row['where']}" + (f" ({row['name']})" if row.get("name") else ""),
             "Deployed  " + (", ".join(x for x in (f"by {row['by_name']}" if row.get("by_name") else None, FL.ago_words(row.get("since"))) if x) or "not known")
             + (f". Version {row['version_n']} of this deployment." if row.get("version_n") else "."),
             f"Traffic   {n['text']}"]
    if s.get("requests"):
        lines.append(f"          {s['per_minute']:g} requests a minute on average.")
    if s.get("calls_text") and s.get("calls") != s.get("requests"):
        lines.append(f"          {s['calls_text']}")
    if s.get("cpu") is not None:
        lines.append(f"Machine   CPU {s['cpu']:g}% on average, {s['cpu_peak']:g}% at the most. Memory {s['memory']:g}%.")
    if s.get("copies_text"):
        lines.append(f"Copies    {s['copies_text']}")
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
    """Stop the project's deployed model. It stops answering and stops billing. What the first deploy created in
    your cloud account stays, so the next deploy is quick: `cst deployments remove` takes that away too."""
    from .. import staging as SG
    from ..staging import fleet as FL

    c = ctx()
    rec = SG.public(_do(SG.state, c, name, env))
    if not rec or rec.get("status") not in ("running", "starting", "failed"):
        fail(f"{name} has no model deployed, so there is nothing to stop.")
    made = ", ".join(f"{r['type']} {r['name']}" for r in rec.get("resources") or [])
    if S.dry_run:
        emit({"dry_run": True, "would_remove": rec.get("resources") or [], "deployed": rec}, f"Would stop {name}'s model" + (f" and remove {made}." if made else "."))
        return
    try:
        r = SG.unstage(c, name, env)
    except _staging_errors() as e:
        fail(str(e))
    FL.forget(c)
    left = r.get("resources") or []
    emit(r, f"Stopped {name}'s model. It no longer answers or bills." + (f" Removed {made}." if made and not left else "")
         + (" Could not remove: " + ", ".join(f"{x.get('type')} {x.get('name')} ({x.get('error', 'still there')})" for x in left) + "." if left else ""))


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


@deployments_app.command("protect")
def deployments_protect(name: str,
                        max_request_kb: Optional[float] = typer.Option(None, "--max-request-kb", help="refuse a request bigger than this"),
                        rate: Optional[float] = typer.Option(None, "--rate", help="requests a second each copy takes; 0 for no limit"),
                        budget_ms: Optional[float] = typer.Option(None, "--budget-ms", help="how long this model may take before the compared model answers; 0 for none"),
                        fallback: Optional[bool] = typer.Option(None, "--fallback/--no-fallback", help="answer with the compared model when this one is slow or fails")):
    """What the deployed model accepts before a real site sends it traffic: a request size limit, a rate limit and
    a time budget with a fallback answer. With no option, show them."""
    from ..staging import protect as PT

    c = ctx()
    given = {k: v for k, v in (("max_request_kb", max_request_kb), ("rate_per_s", rate), ("budget_ms", budget_ms), ("fallback", fallback)) if v is not None}
    r = _do(PT.change, c, name, **given) if given else _do(PT.get, c, name)
    emit(r, "\n".join((r["size_text"], r["rate_text"], r["budget_text"], r["key"]["text"])))


@deployments_app.command("key")
def deployments_key(name: str,
                    rotate: bool = typer.Option(False, "--rotate", help="make a new key; the old one keeps working for the grace period"),
                    grace: Optional[float] = typer.Option(None, "--grace-minutes", help="how long the old key keeps working (60 unless given, 0 to end it now)"),
                    show: bool = typer.Option(False, "--show", help="print the key itself, for the site that calls the model")):
    """The deployed model's key: when it was last changed, and a way to change it with no downtime."""
    from .. import staging as SG
    from ..staging import protect as PT

    c = ctx()
    r = _do(PT.rotate_key, c, name, grace) if rotate else _do(PT.key_state, c, name)
    if show:
        try:
            typer.echo(SG._key(SG.state(c, name)))  # only when asked for, and only to this terminal
        except _staging_errors() as e:
            fail(str(e))
        return
    emit(r, ("The key was changed. " if rotate else "") + r["text"] + ("" if r["can_rotate"] else f" {r['why_not']}"))


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
    # `--check` is a person asking, so a model in a cloud is itself asked, once. `watch` never calls it.
    h = _do(HL.check, c, name, asked=True) if run and not S.dry_run else _do(HL.get, c, name)
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
