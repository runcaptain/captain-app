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
    """These commands change a project directly. An agent session may run them when the person told it to, and each
    one is written to the project's history (project_assist.note_agent_change). A person leaves no such line."""
    from .. import project_assist as PA

    if not S.dry_run:
        _do(PA.note_agent_change, c, name, what)


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


@projects_app.command("generate-events")
def generate_events(name: str, slot: str = typer.Argument("events"),
                    sessions: int = typer.Option(20000, "--sessions"), days: int = typer.Option(30, "--days"),
                    seed: int = typer.Option(7, "--seed"),
                    end: Optional[str] = typer.Option(None, "--end", help="last day of the range, YYYY-MM-DD (today when left out)")):
    """Generate SIMULATED shopper events from the project's catalog and set them as an events input."""
    from .. import project_inputs as PI

    c = ctx()
    _by_hand(c, name, "generate events")
    r = _do(PI.generate_events, c, name, slot, sessions, days, seed, end)
    emit(r, f"{slot}: {r['dataset']} [SIMULATED], {r['input'].get('rows'):,} rows. Results judged on them are simulated.")


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
def accept(name: str, proposal: str, only: list[str] = typer.Option([], "--only", help="apply only these slots (or trainer, evaluation). Repeat"),
           as_asked: Optional[str] = typer.Option(None, "--as-asked", help="For an agent: the person's own words telling you to apply it. Recorded with the change")):
    """Apply a proposal. Connection and local sources are listed for you to set with `cst projects input`.
    An agent session must pass --as-asked with the person's instruction; without it the accept is refused."""
    from .. import project_assist as PA

    r = _do(PA.accept, ctx(), name, proposal, only or None, as_asked)
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
        lines.append(f"{r['slot']:8} {r['pinned'] or 'not pinned'}" + (" [SIMULATED]" if r.get("simulated") else ""))
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


@projects_app.command("baseline-start")
def baseline_start(name: str,
                   title: Optional[str] = typer.Option(None, "--title", help="the catalog column that holds the product title, when it can't be guessed")):
    """Start with keyword search as the baseline: a version that needs no training, over the project's catalog."""
    from .. import starter as ST

    c = ctx()
    if not S.dry_run:
        _by_hand(c, name, "set the baseline")
    r = _do(ST.baseline_start, c, name, title, dry_run=S.dry_run)
    emit(r, (f"Would set {r['baseline']} as the baseline." if S.dry_run else r["text"]))


@projects_app.command("starter-plan")
def starter_plan(name: str):
    """What a starter ranker would train on, the cutoff dates it picked and why, and what is missing."""
    from .. import starter as ST

    p = _do(ST.plan, ctx(), name)
    lines = [p["text"]] + [f"  {u['text']}" for u in p["uses"]]
    if p.get("why"):
        lines.append(p["why"])
    if p.get("baseline"):
        lines.append(p["baseline"]["text"])
    lines += p["warnings"]
    if p["ready"] and p["setup"]["needs_setup"]:
        lines.append("The first run sets up the starter trainer, one time. It needs the internet.")
    emit(p, "\n".join(lines))


@projects_app.command("starter-train")
def starter_train(name: str,
                  param: list[str] = typer.Option([], "--param", help="trainer setting, name=value (rounds, learning_rate, num_leaves). Repeat"),
                  compute: Optional[str] = typer.Option(None, "--compute", help="compute target to train on; default this computer"),
                  wait: bool = typer.Option(False, "--wait", help="with --compute: wait for the job and print the candidate and its cost")):
    """Train a starter ranker with no code: build the training set, train LightGBM on it, record the candidate.
    With --compute the same work runs as a job on that target, reading the inputs from the workspace bucket."""
    import json as _json

    from .. import live as LV
    from .. import starter as ST
    from ..trainers import SetupFailed

    c = ctx()
    params = {}
    for kv in param:
        k, sep, v = kv.partition("=")
        if not sep:
            fail(f"--param takes name=value, got {kv!r}")
        try:
            params[k] = _json.loads(v)
        except ValueError:
            params[k] = v
    import os

    cloud = bool(compute) and compute != "local" and not os.environ.get("CST_JOB_ID")
    if cloud:
        if not S.dry_run:
            _by_hand(c, name, f"train a candidate on {compute}")
        r = _do(ST.train_on, c, name, compute, params=params or None, dry_run=S.dry_run, wait=wait)
        est = (r.get("estimate") or {}).get("text") or ""
        if S.dry_run:
            emit(r, f"Would train a starter ranker for {name} on {compute}. {est}".strip())
        elif r.get("pending"):
            emit(r, f"{r['text']}\n{r['detail']}")
        else:
            cost = (r.get("cost") or {}).get("text")
            emit(r, f"Trained {r['pipeline']} on {compute}. Candidate {r['experiment']}."
                    + (f"\nEstimate: {est}" if est else "") + (f"\n{cost}." if cost else ""))
        return
    if S.dry_run:
        p = _do(ST.plan, c, name)
        emit({**p, "dry_run": True}, p["text"] if not p["ready"] else f"Would train a starter ranker on {p['catalog']}"
             + (f" and {p['events']}" if p["events"] else "") + ".")
        return
    if not os.environ.get("CST_JOB_ID"):  # inside a submitted job the person's go-ahead was given at submit
        _by_hand(c, name, "train a candidate")
    lv = LV.for_job(c, "training", f"Starter ranker for {name}", of=name)
    try:
        r = ST.train(c, name, live=lv, params=params, progress=None if S.json else lambda t: typer.echo(t, err=True))
    except (ValueError, NotFound, Conflict, SetupFailed) as e:
        lv.end("failed", error=str(e))
        fail(str(e))
    except BaseException as e:
        lv.end("failed", error=str(e) or "stopped")
        raise
    lv.end("done", ref=r["pipeline"], summary=r["text"])
    m = r.get("metrics") or {}
    emit(r, f"{r['text']}\nCandidate {r['experiment']}: {r['pipeline']}"
            + (f", ndcg@10 {m['ndcg@10']:.4f} on its own validation queries" if m.get("ndcg@10") is not None else "")
            + f"\nBaseline: {r['baseline']}" + (" (set now: keyword search)" if r["baseline_set"] else "")
            + f"\nNext: cst projects experiment-backtest {name} {r['experiment']}")


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


# ---- staging ---------------------------------------------------------------------------------------


def _staging_errors():
    from ..compute_providers import ProviderError
    from ..staging import NeedsConfirm, NotServable

    return (ValueError, NotFound, Conflict, NotServable, NeedsConfirm, ProviderError)


@projects_app.command("stage")
def stage(name: str,
          candidate: Optional[str] = typer.Option(None, "--candidate", help="a candidate id or model version; default the newest judged candidate"),
          baseline: bool = typer.Option(False, "--baseline", help="stage the baseline itself"),
          env: str = typer.Option("staging", "--env"),
          target: Optional[str] = typer.Option(None, "--target", help="compute target, or `local` for this computer; default the workspace's serving target"),
          endpoint: Optional[str] = typer.Option(None, "--endpoint", help="an HTTP endpoint you already run (POST {query, k} -> {ids}); Captain wraps it"),
          endpoint_auth_secret: Optional[str] = typer.Option(None, "--endpoint-auth-secret", help="name of a secret sent to your endpoint as a Bearer token"),
          image: Optional[str] = typer.Option(None, "--image", help="a container image that serves the model (SageMaker contract), for a cloud target"),
          idle_minutes: Optional[float] = typer.Option(None, "--idle-minutes", help="stop by itself after this long with no requests; default 240"),
          allow_large: bool = typer.Option(False, "--allow-large", help="stage on this computer even when the model is over the size limit for it (Settings)"),
          yes: bool = typer.Option(False, "--yes", "-y", help="confirm the cost of a cloud deploy")):
    """Run a model version as a live staging endpoint: on your compute target, or on this computer with --target local."""
    from .. import staging as SG

    c = ctx()
    if S.dry_run:
        try:
            p = SG.plan(c, name, candidate, baseline, env, target, endpoint, image)
        except _staging_errors() as e:
            fail(str(e))
        emit({**p, "dry_run": True}, "Would: " + p["text"])
        return
    try:
        p = SG.plan(c, name, candidate, baseline, env, target, endpoint, image)
        if p["needs_confirm"] and not yes:
            if S.json:
                fail("Confirm the cost first with --yes. " + p["cost"]["text"])
            typer.echo(p["text"])
            if (p.get("preflight") or {}).get("note"):
                typer.echo(p["preflight"]["note"])
            if not typer.confirm("Start it?"):
                raise typer.Exit(1)
        _by_hand(c, name, "stage a model version")
        r = SG.stage(c, name, candidate, baseline, env, target, endpoint, endpoint_auth_secret, image, confirm=True,
                     idle_minutes=idle_minutes, progress=None if S.json else lambda t: typer.echo(t, err=True),
                     allow_large=allow_large)
    except _staging_errors() as e:
        fail(str(e))
    ep = r.get("endpoint") or {}
    emit(r, f"Staged {r['model_version']} " + ("on this computer" if r["kind"] == "local" else f"on {r['target']}")
            + f"\nEndpoint: {ep.get('url') or ep.get('name')}\n{r['cost']['text']}"
            + ("\nSleeps when nobody calls it, and wakes on the next request." if r.get("scale_to_zero")
               else f"\nStops by itself after {r['idle_minutes']:g} minutes with no requests.")
            + f"\nTry it: cst projects staging-query {name} \"<a query>\"")


@projects_app.command("staging-copies")
def staging_copies(name: str, fewest: Optional[int] = typer.Option(None, "--fewest", help="copies kept up while idle (0 lets it sleep)"),
                   most: Optional[int] = typer.Option(None, "--most", help="the most copies at once"),
                   idle_seconds: Optional[int] = typer.Option(None, "--idle-seconds", help="how long an idle copy stays up, where the host lets it be set"),
                   env: str = typer.Option("staging", "--env")):
    """How many copies the staged endpoint may run and how long an idle one stays up. With a value, change it."""
    from .. import staging as SG

    try:
        if S.dry_run and any(v is not None for v in (fewest, most, idle_seconds)):
            cur = SG.scale(ctx(), name, env)
            return emit({**cur, "dry_run": True, "would_set": {"min_copies": fewest, "max_copies": most, "idle_seconds": idle_seconds}},
                        f"Would change it. Now: {cur['text']}")
        r = SG.scale(ctx(), name, env, min_copies=fewest, max_copies=most, idle_seconds=idle_seconds)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, r["text"])


@projects_app.command("unstage")
def unstage(name: str, env: str = typer.Option("staging", "--env")):
    """Stop the staging endpoint and remove every resource Captain created for it."""
    from .. import staging as SG

    c = ctx()
    if S.dry_run:
        rec = SG.public(_do(SG.state, c, name, env))
        emit({"dry_run": True, "would_remove": (rec or {}).get("resources") or [], "staged": rec},
             "Would stop staging" + (" and remove " + ", ".join(f"{r['type']} {r['name']}" for r in rec["resources"]) if rec and rec.get("resources") else ""))
        return
    _by_hand(c, name, "stop staging")
    try:
        r = SG.unstage(c, name, env)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, "Staging stopped." if r["status"] == "stopped" else f"Staging stopped, but {r['error']}")


@projects_app.command("staging")
def staging_status(name: str, env: str = typer.Option("staging", "--env"),
                   no_sweep: bool = typer.Option(False, "--no-sweep", help="only look: don't stop an endpoint that is past its idle limit")):
    """Where staging stands: what is running, where, since when, and when it stops by itself."""
    from .. import staging as SG

    try:
        s = SG.status(ctx(), name, env, sweep=not no_sweep and not S.dry_run)
    except _staging_errors() as e:
        fail(str(e))
    lines = [s["text"]]
    st = s.get("staged") or {}
    if st.get("endpoint"):
        lines.append(f"Endpoint: {st['endpoint'].get('url') or st['endpoint'].get('name')}")
    if st.get("status") == "running" and st.get("cost"):
        lines.append(st["cost"]["text"])
    if st.get("check"):
        lines.append(st["check"]["text"])
    for r in st.get("resources") or []:
        lines.append(f"  {r['type']:16} {r['name']}")
    emit(s, "\n".join(lines))


@projects_app.command("staging-query")
def staging_query(name: str, query: str, k: int = typer.Option(10, "-k"), env: str = typer.Option("staging", "--env")):
    """Ask the staged endpoint for results, with the baseline's results beside them. Logged as test traffic."""
    from .. import staging as SG

    try:
        r = SG.query(ctx(), name, query, k, env)
    except _staging_errors() as e:
        fail(str(e))
    lines = []
    for side in (r["staged"], r["baseline"]):
        if not side:
            continue
        lines.append(f"{side['arm'].capitalize()} ({side['model_version']}), {side['ms']:.1f} ms")
        lines += [f"  {x['position']:>2}. {x['title'][:70]}" for x in side["results"]]
    emit(r, "\n".join(lines))


@projects_app.command("staging-check")
def staging_check(name: str, env: str = typer.Option("staging", "--env"),
                  sample: int = typer.Option(5000, "--sample", help="how many (query, product) pairs to compare at most"),
                  min_match: float = typer.Option(0.999, "--min-match", help="the share of pairs that must match exactly")):
    """Compare the features the staging endpoint served with the features training computes, for the same pairs."""
    from ..staging import consistency as SC

    try:
        r = SC.check(ctx(), name, env, sample, min_match, dry_run=S.dry_run)
    except _staging_errors() as e:
        fail(str(e))
    lines = [r["text"]]
    if r.get("checked"):
        lines.append(f"mismatch rate {r['mismatch_rate']:.4%}  missing rate {r['missing_rate']:.4%}  extra rate {r['extra_rate']:.4%}")
        for f in r.get("features") or []:
            lines.append(f"  {f['feature']:24} mismatch {f['mismatch_rate']:.2%}  relative error {f['relative_error']:.4f}")
        for x in (r.get("examples") or [])[:3]:
            lines.append(f"  e.g. request {x['request_id']} \"{x['query']}\" {x['product_id']} {x['feature']}: served {x['served']}, training {x['offline']}")
    emit(r, "\n".join(lines))
    if r["status"] == "fail":
        raise typer.Exit(1)


# ---- A/B tests: Captain runs none. The old commands say so. ----------------------------------------


NO_AB = "Captain no longer runs A/B tests. Give your own testing tool each model's address."


def _no_ab() -> None:
    """What every former `ab-*` command does now: say where A/B testing lives, and exit 2."""
    typer.echo(NO_AB, err=True)
    raise typer.Exit(2)


for _name in ("ab", "ab-start", "ab-readout", "ab-ramp", "ab-rollback", "ab-stop", "ab-simulate", "ab-outcomes", "ab-result", "ab-decide"):
    projects_app.command(_name, hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_no_ab)


@projects_app.command("staging-connect")
def staging_connect(name: str, env: str = typer.Option("staging", "--env"),
                    code: Optional[str] = typer.Option(None, "--code", help="print one snippet: curl, javascript, python (or `aws cli` in the cloud)")):
    """How a site calls the staged endpoint, whether it can reach it, and how many real requests have arrived."""
    from ..staging import traffic as TF

    try:
        r = TF.connect(ctx(), name, env)
    except _staging_errors() as e:
        fail(str(e))
    if code:
        if code not in r["snippets"]:
            fail(f"no {code} snippet. There is: {', '.join(r['snippets'])}")
        emit({"code": r["snippets"][code]}, r["snippets"][code])
        return
    lines = [r["text"], r["live"], r["key_note"], r["fallback"]]
    if r.get("blocked"):
        lines.append(r["blocked"])
    lines.append("Snippets: " + ", ".join(f"--code {k!r}" if " " in k else f"--code {k}" for k in r["snippets"]))
    emit(r, "\n".join(lines))


@projects_app.command("staging-test")
def staging_test(name: str, query: Optional[str] = typer.Option(None, "--query"), env: str = typer.Option("staging", "--env")):
    """Send one test request through the door a site uses, to prove the path. It never counts as a shopper."""
    from ..staging import traffic as TF

    try:
        r = TF.test_request(ctx(), name, query, env)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, r["text"])


def _shadow_text(r: dict) -> str:
    lines = [r["text"]]
    for q in r.get("queries") or []:
        lines.append(f"  {q['query'][:40]:40} {q['text']} ({q['requests']} request{'s' if q['requests'] != 1 else ''})")
    if r.get("ms"):
        lines.append(f"Response time, 95th percentile: {r['ms']['baseline']['p95']} ms baseline, {r['ms']['candidate']['p95']} ms candidate.")
    return "\n".join(lines)


@projects_app.command("shadow-start")
def shadow_start(name: str):
    """Shadow mode: shoppers keep getting the baseline's results while the candidate ranks the same requests silently."""
    from ..staging import traffic as TF

    c = ctx()
    _by_hand(c, name, "start shadow mode")
    try:
        r = TF.shadow_start(c, name)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, f"The quiet test is on. Shoppers see {r['arms']['baseline']}. {r['arms']['candidate']} ranks the same requests, and nobody sees it.")


@projects_app.command("shadow")
def shadow_readout(name: str, traffic: Optional[str] = typer.Option(None, "--traffic", help="real, simulated or test; default the strongest there is")):
    """The shadow readout: how often the candidate would change the results, on which queries, and its response time."""
    from ..staging import traffic as TF

    try:
        r = TF.shadow(ctx(), name, traffic)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, _shadow_text(r))


@projects_app.command("shadow-stop")
def shadow_stop(name: str):
    """End shadow mode. The endpoint keeps answering with the baseline."""
    from ..staging import traffic as TF

    try:
        r = TF.shadow_stop(ctx(), name)
    except _staging_errors() as e:
        fail(str(e))
    emit(r, "The quiet test is off.")


@projects_app.command("staging-serve", hidden=True)
def staging_serve(bundle: str, host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(8080, "--port")):
    """Run the staging runtime over a serving bundle. Staging starts this for you; the runner image runs it."""
    from ..staging import runtime as RT

    RT.serve(ctx(), bundle, host=host, port=port)
