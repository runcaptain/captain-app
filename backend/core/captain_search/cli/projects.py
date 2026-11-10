"""cst projects: a project's pinned inputs, its experiments, and the champion they are judged against.

Plural because `cst project` is the captain/ folder (cli/ext.py)."""
from __future__ import annotations

from typing import Optional

import typer

from .. import project_state as PS
from ..storage.backend import Conflict, NotFound
from .main import S, app, ctx, emit, fail

projects_app = typer.Typer(no_args_is_help=True, help="Projects: what you're building, its inputs, experiments and champion")
app.add_typer(projects_app, name="projects")


def _do(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (ValueError, NotFound, Conflict) as e:
        fail(str(e))


def _by_hand(c, name: str, what: str) -> None:
    """These commands change a project directly. An agent session with a proposal open for it is sent to the person
    instead (project_assist.guard_agent). A person at the command line is never stopped."""
    from .. import project_assist as PA

    _do(PA.guard_agent, c, name, what)


@projects_app.command("state")
def state(name: str):
    """The project's state doc: its kind, slots and inputs, experiments, champion and registry."""
    d = _do(PS.get, ctx(), name)
    w = max([len(s["id"]) for s in d["slots"]] + [8])
    lines = [f"kind     {d.get('kind') or '-'}"]
    for s in d["slots"]:
        rec = d["inputs"].get(s["id"]) or {}
        what = rec.get("ref") or d["pins"].get(s["id"]) or (f"live query on {rec['source'].get('connection')}"
                                                              if rec.get("from") == "connection" else "-")
        lines.append(f"{s['id']:{w}} {what}" + (f" ({rec['mode']})" if rec.get("mode") else ""))
    emit(d, "\n".join(lines + [f"champion {d.get('champion') or '-'}", f"{len(d['experiments'])} experiment(s)"]))


@projects_app.command("kinds")
def kinds():
    """What a project can build: the recipes, built in and your own (workspace recipes/ or the repo's captain/recipes/)."""
    from .. import recipes as RC

    rows = RC.listing(ctx())
    emit(rows, "\n".join(f"{r['id']:18} {r['label']} ({r['source']}): "
                         + (", ".join(s['id'] + ("" if s['required'] else "?") for s in r['slots']) or "no fixed inputs")
                         for r in rows))


@projects_app.command("list")
def list_projects():
    """Every project, with what each holds."""
    from .. import projects as PR

    rows = _do(PR.listing, ctx())
    emit(rows, "\n".join(f"{p['id']:24} {p['name']}" for p in rows) or "No projects yet.")


@projects_app.command("new")
def new(name: str, kind: Optional[str] = typer.Option(None, "--kind", help="what you're building: see `cst projects kinds`"),
        description: str = typer.Option("", "--description")):
    """Make a project. With --kind it starts with that recipe's inputs, which you can then change."""
    from .. import projects as PR
    from .. import recipes as RC

    c = ctx()
    if kind:
        _do(RC.get, c, kind)  # an unknown kind fails before the project exists
    p = _do(PR.create, c, name, description)
    st = _do(PS.set_kind, c, p["id"], kind) if kind else None
    emit({**p, "kind": kind, "slots": st["slots"] if st else None},
         f"Made project {p['id']}" + (f" ({st['recipe']['label']}): inputs {', '.join(s['id'] for s in st['slots']) or 'none yet'}" if st else ""))


@projects_app.command("kind")
def set_kind(name: str, kind: str):
    """Switch a project to another kind. Inputs whose slot is still there are kept."""
    c = ctx()
    _by_hand(c, name, "switch the project's kind")
    d = _do(PS.set_kind, c, name, kind)
    emit(d, f"{name} is now {d['recipe']['label']}: inputs {', '.join(s['id'] for s in d['slots']) or 'none'}")


@projects_app.command("slot-add")
def slot_add(name: str, label: str,
             id: Optional[str] = typer.Option(None, "--id", help="default: from the label"),
             role: str = typer.Option("custom", "--role", help="catalog, events, queries, labels, holdout, users, items, interactions or custom"),
             accepts: list[str] = typer.Option([], "--accepts", help="dataset kinds it takes. Repeat for more. Default: any"),
             required: bool = typer.Option(False, "--required"), why: str = typer.Option("", "--why")):
    """Add an input slot of your own."""
    spec = {"label": label, "role": role, "required": required, "why": why, **({"id": id} if id else {}),
            **({"accepts": accepts} if accepts else {})}
    c = ctx()
    _by_hand(c, name, "add a slot")
    d = _do(PS.slot_add, c, name, spec)
    emit(d, f"Added {d['slots'][-1]['id']} to {name}")


@projects_app.command("slot-edit")
def slot_edit(name: str, slot: str, label: Optional[str] = typer.Option(None, "--label"),
              role: Optional[str] = typer.Option(None, "--role"),
              accepts: list[str] = typer.Option([], "--accepts"),
              required: Optional[bool] = typer.Option(None, "--required/--optional"),
              why: Optional[str] = typer.Option(None, "--why")):
    """Rename a slot, or change what it takes and whether it's required."""
    ch = {k: v for k, v in (("label", label), ("role", role), ("required", required), ("why", why)) if v is not None}
    if accepts:
        ch["accepts"] = accepts
    if not ch:
        fail("change at least one of --label, --role, --accepts, --required/--optional, --why")
    c = ctx()
    _by_hand(c, name, "change a slot")
    _do(PS.slot_update, c, name, slot, ch)
    emit({"project": name, "slot": slot, "changed": ch}, f"Changed {slot}: {', '.join(ch)}")


@projects_app.command("slot-rm")
def slot_rm(name: str, slot: str):
    """Remove an optional slot, and what it pointed at. The datasets themselves stay."""
    c = ctx()
    _by_hand(c, name, "remove a slot")
    _do(PS.slot_remove, c, name, slot)
    emit({"project": name, "removed": slot}, f"Removed {slot} from {name}")


@projects_app.command("evaluation")
def evaluation(name: str, method: Optional[str] = typer.Argument(None, help="auto, holdout, logged-clicks, trainer-reported or none")):
    """Show how experiments are judged, or choose the method."""
    c = ctx()
    if method:
        _by_hand(c, name, "choose the evaluation")
        _do(PS.set_evaluation, c, name, method)
    ev = PS.evaluation_plan(c, _do(PS.get, c, name))
    emit(ev, f"{ev['label']} ({'ready' if ev['ready'] else 'not ready'}). {ev['text']}")


@projects_app.command("sources")
def sources():
    """What inputs can come from: connections, datasets, and local folders with table files."""
    from .. import project_inputs as PI

    d = PI.sources(ctx())
    lines = [f"connection {c['name']:20} {c['label']}: {', '.join(c['offers'])}" for c in d["connections"]]
    lines += [f"folder     {f['path']} ({f['files']} table files)" for f in d["folders"]]
    lines.append(f"{len(d['datasets'])} dataset(s): `cst data list`")
    emit(d, "\n".join(lines))


@projects_app.command("input")
def input_set(name: str, slot: str,
              mode: Optional[str] = typer.Option(None, "--mode", help="snapshot (copy the rows now) or live (read the source at each run). Required"),
              connection: Optional[str] = typer.Option(None, "--connection"),
              table: Optional[str] = typer.Option(None, "--table", help="schema.table on the connection, or a table in a .duckdb file"),
              sql: Optional[str] = typer.Option(None, "--sql"),
              event: Optional[str] = typer.Option(None, "--event", help="a PostHog event name"),
              days: int = typer.Option(30, "--days", help="with --event: how far back"),
              report: Optional[str] = typer.Option(None, "--report", help="a Google Analytics report, as JSON"),
              path: Optional[str] = typer.Option(None, "--path", help="a local file or folder"),
              dataset: Optional[str] = typer.Option(None, "--dataset", help="a dataset ref already in the workspace"),
              as_of: Optional[str] = typer.Option(None, "--as-of", help="the cutoff date this input stands for"),
              kind: Optional[str] = typer.Option(None, "--kind", help="dataset kind for a snapshot, when it can't be guessed")):
    """Point a slot at a connection, a local file or folder, or a dataset, as a snapshot or a live reference."""
    import json as _json

    from .. import project_inputs as PI

    if mode not in ("snapshot", "live"):
        fail("say --mode snapshot (copy the rows now) or --mode live (read the source at each run)")
    if connection:
        src = {"from": "connection", "connection": connection}
        if event:
            src["event"] = {"name": event, "days": days}
        elif report:
            try:
                src["report"] = _json.loads(report)
            except ValueError:
                fail("--report is JSON")
        elif sql:
            src["sql"] = sql
        elif table:
            src["table"] = table
    elif path:
        src = {"from": "local", "path": path, **({"table": table} if table else {})}
    elif dataset:
        src = {"from": "dataset", "ref": dataset}
    else:
        fail("name a source: --connection (with --table, --sql, --event or --report), --path or --dataset")
    c = ctx()
    _by_hand(c, name, "set an input")
    r = _do(PI.set_input, c, name, slot, src, mode, as_of, kind)
    i = r["input"]
    emit(r, f"{slot}: {i.get('ref') or 'live query'} ({mode}), {i.get('rows') if i.get('rows') is not None else 'unknown'} rows")


@projects_app.command("input-clear")
def input_clear(name: str, slot: str):
    """Point a slot at nothing again. What it pointed at stays in the workspace."""
    from .. import project_inputs as PI

    c = ctx()
    _by_hand(c, name, "clear an input")
    _do(PI.clear_input, c, name, slot)
    emit({"project": name, "slot": slot, "cleared": True}, f"Cleared {slot}")


@projects_app.command("delete")
def delete(name: str, yes: bool = typer.Option(False, "--yes", "-y", help="don't ask"),
           force: bool = typer.Option(False, "--force", help="delete even with a live handoff waiting for its deployment")):
    """Delete a project. Its datasets, pipelines, models and runs stay in the workspace."""
    from .. import projects as PR

    c = ctx()
    if S.dry_run:
        emit({"project": name, "dry_run": True, "pending_handoffs": PR.pending_handoffs(c, name)}, f"Would delete {name}.")
        return
    if not yes and not S.json:
        if not typer.confirm(f"Delete project {name}? Its datasets, models and runs stay.", default=False):
            fail("Not deleted.")
    elif not yes:
        fail("pass --yes to delete without asking")
    try:
        r = PR.delete(c, name, force=force)
    except (ValueError, NotFound, Conflict) as e:
        fail(str(e))
    k = r["kept"]
    emit(r, f"Deleted {name}. Kept {k['datasets']} dataset(s), {k['pipelines']} pipeline(s), {k['models']} model(s) "
            f"and {k['backtests']} backtest(s).")


@projects_app.command("assist")
def assist(name: str, ask: str = typer.Option("", "--ask", help="what you want help with")):
    """Print a prompt for a coding agent to propose this project's inputs and trainer. Nothing is changed."""
    from .. import project_assist as PA

    r = _do(PA.assist, ctx(), name, ask)
    emit(r, r["prompt"])


@projects_app.command("propose")
def propose(name: str, file: str = typer.Option(..., "--file", help="proposal.yaml (see `cst projects assist`)"),
            assist: Optional[str] = typer.Option(None, "--assist", help="the assist request this answers"),
            agent: Optional[str] = typer.Option(None, "--agent", help="your agent name. Default: detected")):
    """File a proposal for a project's inputs and trainer. It waits for a person to accept it in the app."""
    import os

    import yaml as _yaml

    from .. import project_assist as PA

    try:
        with open(file) as f:
            doc = _yaml.safe_load(f) or {}
    except (OSError, _yaml.YAMLError) as e:
        fail(f"can't read {file}: {e}")
    r = _do(PA.propose, ctx(), name, doc, assist, os.path.dirname(os.path.abspath(file)), agent)
    emit(r, f"Proposed {r['id']} for {name}: {len(r['slots'])} slot(s)" + (", a trainer" if r["trainer"] else "")
         + ". It shows in the app for a person to accept.")


@projects_app.command("proposals")
def proposals(name: str):
    from .. import project_assist as PA

    rows = _do(PA.listing, ctx(), name)
    emit(rows, "\n".join(f"{p['id']}  {p['status']:9} {p.get('agent') or p['by']}: {p['note'][:80]}" for p in rows)
         or "No proposals yet.")


@projects_app.command("accept")
def accept(name: str, proposal: str, only: list[str] = typer.Option([], "--only", help="apply only these slots (or trainer, evaluation). Repeat")):
    """Apply a proposal. Connection and local sources are listed for you to set with `cst projects input`."""
    from .. import project_assist as PA

    r = _do(PA.accept, ctx(), name, proposal, only or None)
    a = r["applied"]
    lines = [f"Accepted {proposal}: added {len(a['added'])}, changed {len(a['changed'])}, removed {len(a['removed'])}, pinned {len(a['pinned'])}."]
    if a.get("already"):
        lines.append(f"Already there, as proposed: {', '.join(a['already'])}.")
    lines += [f"Left out {x['slot']}: {x['reason']}" for x in a.get("conflicts") or []]
    lines += [f"Set {t['slot']} next: cst projects input {name} {t['slot']} --mode snapshot|live ..." for t in r["to_connect"]]
    emit(r, "\n".join(lines))


@projects_app.command("reject")
def reject(name: str, proposal: str, note: str = typer.Option("", "--note")):
    from .. import project_assist as PA

    r = _do(PA.reject, ctx(), name, proposal, note)
    emit(r, f"Rejected {proposal}")


@projects_app.command("stages")
def stages(name: str):
    """Where the project stands: each input, the experiments and the champion, with what to do next."""
    d = _do(PS.stages, ctx(), name)
    lines = []
    for r in d["inputs"]:
        lines.append(f"{r['slot']:8} {r['pinned'] or 'not pinned'}")
        lines += [f"         {i['text']}" for i in r["info"]]
    e = d["experiments"]
    lines.append(f"experiments: {e['count']} " + ", ".join(f"{k} {v}" for k, v in e["by_status"].items()))
    lines.append(f"champion: {d['champion']['pinned'] or 'none'}")
    lines += [n["text"] for n in d["notes"]]
    emit(d, "\n".join(lines))


@projects_app.command("pin")
def pin(name: str,
        catalog: Optional[str] = typer.Option(None, "--catalog", help="catalog dataset, name@vN"),
        events: Optional[str] = typer.Option(None, "--events", help="events or logs dataset"),
        queries: Optional[str] = typer.Option(None, "--queries", help="queries dataset"),
        labels: Optional[str] = typer.Option(None, "--labels", help="judgments dataset that grades the holdout"),
        holdout: Optional[str] = typer.Option(None, "--holdout", help="a sealed queries dataset"),
        arch: Optional[str] = typer.Option(None, "--arch", help="registry architecture for published versions"),
        brand: Optional[str] = typer.Option(None, "--brand", help="registry brand for published versions"),
        registry: Optional[str] = typer.Option(None, "--registry", help="model registry URI"),
        slot: list[str] = typer.Option([], "--slot", help="slot=name@vN for any slot, including your own. Repeat")):
    """Pin datasets to exact versions (a name alone pins its newest version). Pass "" to unpin one."""
    refs = {k: v for k, v in (("catalog", catalog), ("events", events), ("queries", queries), ("labels", labels),
                              ("holdout", holdout)) if v is not None}
    for p in slot:
        k, sep, v = p.partition("=")
        if not sep:
            fail(f"--slot takes slot=name@vN, got {p!r}")
        refs[k] = v
    if not refs and arch is None and brand is None and registry is None:
        fail("pin at least one of --slot, --catalog, --events, --queries, --labels, --holdout, --arch, --brand, --registry")
    c = ctx()
    out = {}
    if refs:
        if not S.dry_run:
            _by_hand(c, name, "pin an input")
        out = _do(PS.pin, c, name, refs, dry_run=S.dry_run)
    if arch is not None or brand is not None or registry is not None:
        out["registry"] = _do(PS.set_registry, c, name, arch, brand, registry, dry_run=S.dry_run)["registry"]
    verb = "Would pin" if S.dry_run else "Pinned"
    emit(out, "\n".join(f"{verb} {s}: {ch['to'] or 'nothing'} (was {ch['from'] or 'nothing'})"
                        for s, ch in (out.get("changed") or {}).items()) or "No pins changed.")


@projects_app.command("experiments")
def experiments(name: str):
    """Each experiment with its evaluation against the champion."""
    d = _do(PS.experiments, ctx(), name)
    lines = []
    for e in d["experiments"]:
        ev = e.get("evaluation") or {}
        star = "*" if e["is_champion"] else " "
        num = (f"{ev['metric']} {ev['value']:.4f} ({ev['delta']:+.4f} [{ev['ci_low']:+.4f}, {ev['ci_high']:+.4f}]) "
               f"{ev['verdict']}, {ev['label_independence']['status']} labels, {ev['online']}"
               if ev.get("delta") is not None else e["status"])
        lines.append(f"{star} {e['id']}  {e['pipeline']:32} {num}")
    emit(d, "\n".join(lines) or "No experiments yet.")


@projects_app.command("experiment-add")
def experiment_add(name: str, pipeline: str,
                   training_set: Optional[str] = typer.Option(None, "--training-set"),
                   labels: Optional[str] = typer.Option(None, "--labels", help="the labels it was trained on"),
                   trainer: Optional[str] = typer.Option(None, "--trainer"),
                   note: str = typer.Option("", "--note")):
    """Record a trained pipeline version as an experiment."""
    e = _do(PS.experiment_add, ctx(), name, pipeline, training_set, labels, trainer, note, dry_run=S.dry_run)
    emit(e, f"{'Would add' if S.dry_run else 'Added'} {e['id']}: {e['pipeline']}")


@projects_app.command("champion")
def champion(name: str, which: Optional[str] = typer.Argument(None, help="an experiment id or pipeline ref"),
             expect: Optional[str] = typer.Option(None, "--expect", help="the champion you saw; refuse if it changed"),
             clear: bool = typer.Option(False, "--clear")):
    """Show the champion, or make an experiment the champion."""
    c = ctx()
    if not which and not clear:
        d = _do(PS.get, c, name)
        emit({"project": name, "champion": d.get("champion")}, d.get("champion") or "No champion yet.")
        return
    r = _do(PS.set_champion, c, name, None if clear else which, "" if expect is None else expect, dry_run=S.dry_run)
    emit(r, f"{'Would make' if S.dry_run else 'Made'} {r['champion'] or 'nothing'} the champion "
            f"(was {r['previous'] or 'nothing'}).")


@projects_app.command("experiment-backtest")
def experiment_backtest(name: str, experiment: str,
                        compute: Optional[str] = typer.Option(None, "--compute", help="compute target; default local")):
    """Backtest an experiment against the champion on the project's sealed holdout."""
    r = _do(PS.experiment_backtest, ctx(), name, experiment, compute, dry_run=S.dry_run)
    ind = r["label_independence"]
    head = f"{'Would compare' if S.dry_run else 'Compared'} {r['arms'][1]} with the champion {r['arms'][0]} on {r['queries']}."
    tail = r.get("run") and f" Run {r['run']}." or (r.get("job") and f" Submitted job {r['job']}." or "")
    emit(r, head + tail + f"\nLabels: {ind['status']}. {ind['text']}")


@projects_app.command("set-icon")
def set_icon(name: str,
             glyph: Optional[str] = typer.Option(None, "--glyph", help="an icon from the app's set, like workflow or package"),
             color: Optional[str] = typer.Option(None, "--color", help="one of the palette's colors, like #0043CA"),
             brand: Optional[str] = typer.Option(None, "--brand", help="a brand mark the app ships, like snowflake"),
             logo: Optional[str] = typer.Option(None, "--logo", help="a PNG, JPEG or SVG file: stored in the workspace"),
             clear: bool = typer.Option(False, "--clear", help="back to the default icon")):
    """Set a project's icon: a glyph and color, a brand mark, or your organization's logo."""
    from .. import projects as PR

    picked = [x for x in (glyph, brand, logo) if x] + (["clear"] if clear else [])
    if len(picked) != 1:
        fail("pick one of --glyph, --brand, --logo or --clear")
    c = ctx()
    if S.dry_run:
        emit({"project": name, "dry_run": True}, f"Would set {name}'s icon.")
        return
    if logo:
        p = _do(PR.upload_logo, c, name, logo)
    else:
        icon = None if clear else {"kind": "glyph", "name": glyph, "color": color or PR.ICON_COLORS[0]} if glyph else {"kind": "brand", "slug": brand}
        p = _do(PR.set_icon, c, name, icon)
    ic = p.get("icon")
    emit(p, f"{p['name']}'s icon: " + ("the default" if not ic else ic.get("name") or ic.get("slug") or "your logo"))
