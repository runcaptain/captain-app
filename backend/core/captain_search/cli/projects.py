"""cst projects: a project's pinned inputs, its experiments, and the champion they are judged against.

Plural because `cst project` is the captain/ folder (cli/ext.py)."""
from __future__ import annotations

from typing import Optional

import typer

from .. import project_state as PS
from ..storage.backend import Conflict, NotFound
from .main import S, app, ctx, emit, fail

projects_app = typer.Typer(no_args_is_help=True, help="A project's pinned inputs, experiments and champion")
app.add_typer(projects_app, name="projects")


def _do(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (ValueError, NotFound, Conflict) as e:
        fail(str(e))


@projects_app.command("state")
def state(name: str):
    """The project's state doc: pins, experiments, champion and registry."""
    d = _do(PS.get, ctx(), name)
    emit(d, "\n".join([f"{s:8} {d['pins'].get(s) or '-'}" for s in PS.SLOTS]
                      + [f"champion {d.get('champion') or '-'}", f"{len(d['experiments'])} experiment(s)"]))


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
        registry: Optional[str] = typer.Option(None, "--registry", help="model registry URI")):
    """Pin inputs to exact versions (a name alone pins its newest version). Pass "" to unpin one."""
    refs = {k: v for k, v in (("catalog", catalog), ("events", events), ("queries", queries), ("labels", labels),
                              ("holdout", holdout)) if v is not None}
    if not refs and arch is None and brand is None and registry is None:
        fail("pin at least one of --catalog, --events, --queries, --labels, --holdout, --arch, --brand, --registry")
    c = ctx()
    out = {}
    if refs:
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
