"""cst investigate and cst signal: the investigation-to-decision workflow (spec stories S1, S3, S6, S7)."""
from __future__ import annotations

import json
from typing import Optional

import typer

from .. import investigations as INV
from .main import S, app, ctx, emit
from .more import _do

inv_app = typer.Typer(no_args_is_help=True, help="Investigations: a question, its evidence, its runs, and the decision")
sig_app = typer.Typer(no_args_is_help=True, help="Candidate signals with their source, definition, availability and leakage checks")
app.add_typer(inv_app, name="investigate")
app.add_typer(sig_app, name="signal")

AGENT = typer.Option(None, "--agent", help="the agent doing this (default: $CST_AGENT)")
IID = typer.Argument(None, help="investigation id (default: $CST_INVESTIGATION, set in a Captain space linked to one)")


def _iid(iid: Optional[str]) -> str:
    import os

    from .main import fail

    v = iid or os.environ.get("CST_INVESTIGATION")
    if not v:
        fail("give an investigation id, or run this in a Captain space linked to one (CST_INVESTIGATION)")
    return v


def _plan(baseline, candidates, queries, judgments, population, primary, mde):
    return {"baseline": baseline, "candidates": candidates.split(",") if candidates else None, "queries": queries,
            "judgments": judgments, "population": population, "primary_metric": primary, "mde": mde}


def _show(d: dict) -> str:
    p = d.get("plan") or {}
    lines = [f"{d['id']}  [{d['state']}]  {d['title']}", f"  hypothesis r{p.get('rev')}: {p.get('hypothesis') or '-'}",
             f"  baseline {p.get('baseline') or '-'}  candidates {', '.join(p.get('candidates') or []) or '-'}  "
             f"queries {p.get('queries') or '-'}  judgments {p.get('judgments') or '-'}"]
    for r in d.get("runs", []):
        lines.append(f"  run r{r['rev']}: {r['status']} {r.get('run') or ''} {r.get('error') or ''}")
    for e in d.get("evidence", []):
        lines.append(f"  evidence {e['kind']} {e['ref']} {e.get('note') or ''}")
    if d.get("missing"):
        lines.append("  missing to run: " + "; ".join(d["missing"]))
    if d.get("decision"):
        lines.append(f"  decided: {d['decision']['outcome']} ({d['decision']['claim']}) by {d['decision']['by']}")
    return "\n".join(lines)


@inv_app.command("new")
def inv_new(title: str = typer.Option(..., "--title", help="the question"), hypothesis: str = typer.Option("", "--hypothesis"),
            baseline: Optional[str] = typer.Option(None, "--baseline"), candidates: Optional[str] = typer.Option(None, "--candidates", help="a@v2,b@v1"),
            queries: Optional[str] = typer.Option(None, "--queries"), judgments: Optional[str] = typer.Option(None, "--judgments"),
            population: Optional[str] = typer.Option(None, "--population"), primary: Optional[str] = typer.Option(None, "--primary"),
            mde: Optional[float] = typer.Option(None, "--mde"), agent: Optional[str] = AGENT):
    """Open an investigation. Only the question is required. The plan can be filled in later."""
    d = _do(INV.create, ctx(), title, hypothesis, agent, dry_run=S.dry_run, **_plan(baseline, candidates, queries, judgments, population, primary, mde))
    emit(d, f"{'would open' if S.dry_run else 'opened'} {d['id']}: {d['title']}" + (f"\nmissing to run: {'; '.join(d['missing'])}" if d["missing"] else ""))


@inv_app.command("list")
def inv_list(state: Optional[str] = typer.Option(None, "--state"), q: Optional[str] = typer.Option(None, "--search", "-q")):
    rows = _do(INV.listing, ctx(), state, q)
    emit(rows, "\n".join(f"{r['id']}  {r['state']:16} {r['title']}" for r in rows) or "no investigations")


@inv_app.command("show")
def inv_show(iid: Optional[str] = IID):
    d = _do(INV.get, ctx(), _iid(iid))
    emit(d, _show(d))


@inv_app.command("revise")
def inv_revise(iid: Optional[str] = IID, hypothesis: Optional[str] = typer.Option(None, "--hypothesis"), note: str = typer.Option("", "--note"),
               baseline: Optional[str] = typer.Option(None, "--baseline"), candidates: Optional[str] = typer.Option(None, "--candidates"),
               queries: Optional[str] = typer.Option(None, "--queries"), judgments: Optional[str] = typer.Option(None, "--judgments"),
               population: Optional[str] = typer.Option(None, "--population"), primary: Optional[str] = typer.Option(None, "--primary"),
               mde: Optional[float] = typer.Option(None, "--mde"),
               expected_revision: Optional[int] = typer.Option(None, "--expected-revision", help="refuse if someone revised it since"),
               agent: Optional[str] = AGENT):
    """Add a revision. Earlier runs keep the revision they tested."""
    if S.dry_run:
        return emit({"would_revise": _iid(iid)})
    d = _do(INV.revise, ctx(), _iid(iid), hypothesis, note, agent, expected_revision,
            **_plan(baseline, candidates, queries, judgments, population, primary, mde))
    emit(d, _show(d))


@inv_app.command("evidence")
def inv_evidence(iid: Optional[str] = IID, kind: str = typer.Option(..., "--kind", help=", ".join(INV.EVIDENCE_KINDS)),
                 ref: str = typer.Option(..., "--ref"), note: str = typer.Option("", "--note"), agent: Optional[str] = AGENT):
    """Attach evidence: a dataset, run, slice, query, signal, label, replay, handoff or a note."""
    if S.dry_run:
        return emit({"would_attach": {"kind": kind, "ref": ref}})
    d = _do(INV.add_evidence, ctx(), _iid(iid), kind, ref, note, agent)
    emit(d, _show(d))


@inv_app.command("run")
def inv_run(iid: Optional[str] = IID, idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key", help="a retry with the same key returns the first run"),
            agent: Optional[str] = AGENT):
    """Run the comparison the current revision describes, within the execution policy."""
    if S.dry_run:
        return emit({"would_run": _iid(iid)})
    d = _do(INV.run, ctx(), _iid(iid), agent, idempotency_key)
    emit(d, _show(d))


@inv_app.command("decide")
def inv_decide(iid: Optional[str] = IID, outcome: str = typer.Option(..., "--outcome", help="continue, online-test, reject or inconclusive"),
               interpretation: str = typer.Option(..., "--interpretation"), next_step: str = typer.Option(..., "--next"),
               limitation: list[str] = typer.Option([], "--limitation", help="repeatable"),
               claim: str = typer.Option("none", "--claim", help=", ".join(INV.CLAIMS)),
               run: list[str] = typer.Option([], "--run", help="runs this rests on (default: all)"),
               slice_: list[str] = typer.Option([], "--slice", help="slices that matter (repeatable)")):
    """Record the decision. People decide: refused in an agent session."""
    if S.dry_run:
        return emit({"would_decide": _iid(iid), "outcome": outcome})
    d = _do(INV.decide, ctx(), _iid(iid), outcome, interpretation, limitation, next_step, run or None, slice_ or None, claim)
    emit(d, f"decided {d['investigation']}: {d['outcome']} ({d['claim']}, {d['evidence_label']})")


@inv_app.command("export")
def inv_export(iid: Optional[str] = IID):
    """The investigation, decision, runs and signals as one JSON document (captain.investigation/v1)."""
    d = _do(INV.export, ctx(), _iid(iid))
    typer.echo(json.dumps(d, indent=2, default=str))


@inv_app.command("prompt")
def inv_prompt(iid: Optional[str] = IID, ask: str = typer.Option("investigate", "--ask", help="investigate, run, explain or panel")):
    """Print an editable prompt for any coding agent, built from this investigation."""
    t = _do(INV.prompt, ctx(), _iid(iid), ask)
    emit({"prompt": t}, t)


@inv_app.command("policy")
def inv_policy(agent_runs: Optional[bool] = typer.Option(None, "--agent-runs/--no-agent-runs"),
               max_queries: Optional[int] = typer.Option(None, "--max-queries"),
               max_runs_per_day: Optional[int] = typer.Option(None, "--max-runs-per-day")):
    """Show or set what agents may run (a person sets it)."""
    vals = {k: v for k, v in {"agent_runs": agent_runs, "max_queries": max_queries, "max_runs_per_day": max_runs_per_day}.items() if v is not None}
    c = ctx()
    p = _do(INV.set_policy, c, vals) if vals and not S.dry_run else INV.policy(c)
    emit(p, "\n".join(f"{k}: {v}" for k, v in p.items()))


@sig_app.command("new")
def sig_new(name: str = typer.Option(..., "--name"), source: str = typer.Option(..., "--source", help="where the data comes from"),
            definition: str = typer.Option(..., "--definition", help="SQL, a label ref like deep-search@v2, or a code path"),
            rationale: str = typer.Option("", "--rationale"), available_at: str = typer.Option("", "--available-at", help="when the value exists, e.g. 'at query time', 'next day'"),
            investigation: Optional[str] = typer.Option(None, "--investigation"), agent: Optional[str] = AGENT):
    """Record a candidate signal. Availability and leakage start as unchecked."""
    s = _do(INV.signal_create, ctx(), name, source, definition, rationale, available_at, investigation, agent)
    emit(s, f"recorded signal {s['id']}: {s['name']} (availability unchecked, leakage unchecked)")


@sig_app.command("list")
def sig_list():
    rows = _do(INV.signals, ctx())
    emit(rows, "\n".join(f"{s['id']}  {s['name']:28} availability {s['availability']['status']:9} leakage {s['leakage']['status']}" for s in rows)
         or "no signals")


@sig_app.command("check")
def sig_check(sid: str, what: str = typer.Option(..., "--what", help="availability or leakage"),
              status: str = typer.Option(..., "--status", help="ok, problem or unchecked"), note: str = typer.Option("", "--note"),
              agent: Optional[str] = AGENT):
    """Record how a signal's availability or leakage was checked."""
    s = _do(INV.signal_check, ctx(), sid, what, status, note, agent)
    emit(s, f"{s['name']}: {what} {status}")
