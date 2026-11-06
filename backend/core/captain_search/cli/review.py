"""cst commands for proposals, human grading, label definitions, replay and scale benchmarks."""
from __future__ import annotations

from typing import Optional

import typer

from .. import bench as BE
from .. import grading as GR
from .. import labels as LB
from .. import proposals as PR
from .. import replay as RP
from .main import S, app, ctx, emit
from .more import _do, _out

propose_app = typer.Typer(no_args_is_help=True, help="Experiment proposals: an agent or a person proposes, a person approves")
grade_app = typer.Typer(no_args_is_help=True, help="Human grading queues that publish HUMAN_GRADED judgments")
label_app = typer.Typer(no_args_is_help=True, help="Labels defined as versioned SQL over event logs")
replay_app = typer.Typer(no_args_is_help=True, help="Replay a policy over randomized-holdout logs")
bench_app = typer.Typer(no_args_is_help=True, help="Measured scale benchmarks")
for n, t in [("propose", propose_app), ("grade", grade_app), ("label", label_app), ("replay", replay_app), ("bench", bench_app)]:
    app.add_typer(t, name=n)


# ---- proposals ----------------------------------------------------------------------------


@propose_app.command("new")
def prop_new(title: str = typer.Option(..., "--title"), hypothesis: str = typer.Option(..., "--hypothesis"),
             arms: str = typer.Option(..., "--arms", help="baseline first: a@v1,b@v2"),
             queries: str = typer.Option(..., "--queries", help="name@v2:holdout"),
             judgments: Optional[str] = typer.Option(None, "--judgments"), population: Optional[str] = typer.Option(None, "--population"),
             primary: str = typer.Option("ndcg@10", "--primary"), mde: float = typer.Option(0.003, "--mde"),
             rationale: str = typer.Option("", "--rationale", help="why this is worth running"),
             agent: Optional[str] = typer.Option(None, "--agent", help="the agent writing this (default: $CST_AGENT)")):
    """Propose an experiment. It waits for a person to approve it before anything runs."""
    d = _do(PR.create, ctx(), title, hypothesis, arms.split(","), queries, judgments, population, primary, mde, rationale, agent,
            dry_run=S.dry_run)
    emit(d, f"{'would propose' if S.dry_run else 'proposed'} {d['id']}: {d['title']}" + (f" (agent {d['agent']})" if d["agent"] else ""))


@propose_app.command("list")
def prop_list(status: Optional[str] = typer.Option(None, "--status", help="proposed, approved, rejected or ran")):
    rows = _do(PR.listing, ctx(), status)
    emit(rows, "\n".join(f"{d['id']}  {d['status']:9} {d['title']}" + (f"  [agent {d['agent']}]" if d["agent"] else "") for d in rows)
         or "no proposals")


@propose_app.command("show")
def prop_show(pid: str):
    d = _do(PR.get, ctx(), pid)
    emit(d, f"{d['title']} ({d['status']})\n{d['hypothesis']}\narms: {', '.join(d['arms'])}  queries: {d['queries']}")


@propose_app.command("approve")
def prop_approve(pid: str, note: str = typer.Option("", "--note")):
    """Approve a proposal so it can run. Refused in an agent session."""
    if S.dry_run:
        return emit({"would_approve": pid})
    d = _do(PR.review, ctx(), pid, "approved", note)
    emit(d, f"approved {pid}")


@propose_app.command("reject")
def prop_reject(pid: str, note: str = typer.Option("", "--note")):
    if S.dry_run:
        return emit({"would_reject": pid})
    d = _do(PR.review, ctx(), pid, "rejected", note)
    emit(d, f"rejected {pid}")


@propose_app.command("run")
def prop_run(pid: str):
    """Plan and run the backtest for an approved proposal."""
    if S.dry_run:
        return emit({"would_run": pid})
    d = _do(PR.run, ctx(), pid)
    r = d.get("result") or {}
    emit(d, f"ran {pid} as {d['run']}" + (f": {r['metric']} {r['delta']:+.4f} {r['verdict']}" if r else ""))


# ---- grading ------------------------------------------------------------------------------


@grade_app.command("queue")
def grade_queue(name: str, queries: str = typer.Option(..., "--queries"), pipelines: str = typer.Option(..., "--pipelines", help="a@v1,b@v2"),
                depth: int = typer.Option(10, "--depth"), sample: Optional[int] = typer.Option(None, "--sample", help="grade this many queries"),
                overlap: float = typer.Option(0.1, "--overlap", help="share of pairs every grader sees, for agreement")):
    """Build a grading queue from the products these pipelines return for these queries."""
    r = _do(GR.create, ctx(), name, queries, pipelines.split(","), depth, sample, overlap, dry_run=S.dry_run)
    emit(r, f"{'would queue' if S.dry_run else 'queued'} {r['pairs']} pairs in {name}")


@grade_app.command("list")
def grade_list():
    rows = _do(GR.listing, ctx())
    emit(rows, "\n".join(f"{r['name']:24} {r['graded']}/{r['pairs']} graded  graders {len(r['graders'])}  kappa {r['kappa']}" for r in rows)
         or "no grading queues")


@grade_app.command("next")
def grade_next(name: str):
    """The next pair for you to grade."""
    r = _do(GR.next_pair, ctx(), name)
    if r["done"]:
        return emit(r, f"{name}: nothing left for you")
    p, prod = r["pair"], r["product"]
    emit(r, f"[{p['id']}] query: {p['query']}\n  product {p['product_id']}: {prod.get('title', '')}  {prod.get('brand', '')}\n"
            f"  grade 0 irrelevant, 1 partial, 2 relevant, 3 exact   ({r['remaining']} left)")


@grade_app.command("set")
def grade_set(name: str, pair: str = typer.Option(..., "--pair"), grade: int = typer.Option(..., "--grade"),
              note: str = typer.Option("", "--note")):
    if S.dry_run:
        return emit({"would_grade": pair, "grade": grade})
    r = _do(GR.grade, ctx(), name, pair, grade, note)
    emit(r, f"graded {pair}: {grade}")


@grade_app.command("status")
def grade_status(name: str):
    r = _do(GR.status, ctx(), name)
    emit(r, f"{name}: {r['graded']}/{r['pairs']} graded by {', '.join(r['graders']) or 'nobody'}"
            + (f", agreement kappa {r['kappa']}" if r["kappa"] is not None else ""))


@grade_app.command("publish")
def grade_publish(name: str, into: str = typer.Option(..., "--into", help="judgments dataset name")):
    """Publish the grades as a HUMAN_GRADED judgments dataset."""
    m = _do(GR.publish, ctx(), name, into, dry_run=S.dry_run)
    _out(m, f"published {m.name}@v{m.version}: {m.row_count} human grades")


# ---- labels -------------------------------------------------------------------------------


@label_app.command("define")
def label_define(name: str, sql: str = typer.Option(..., "--sql", help="aggregate per group, e.g. max(case when clicked then 1 else 0 end)"),
                 grain: str = typer.Option("search", "--grain", help="search, session, user or impression"),
                 where: Optional[str] = typer.Option(None, "--where", help="row filter"),
                 dtype: str = typer.Option("binary", "--dtype", help="binary or number"),
                 description: str = typer.Option("", "--description")):
    """Publish a new version of a label definition."""
    m = _do(LB.define, ctx(), name, sql, grain, where, dtype, description, dry_run=S.dry_run)
    _out(m, f"defined {m.name}@v{m.version} ({m.grain})")


@label_app.command("list")
def label_list():
    c = ctx()
    rows = [c.registry.load("label", n).model_dump(mode="json") for n in c.registry.names("label")]
    emit(rows, "\n".join(f"{r['name']}@v{r['version']}  {r['grain']:10} {r['sql']}" for r in rows) or "no labels")


@label_app.command("preview")
def label_preview(label: str, logs: str = typer.Option(..., "--logs"), limit: int = typer.Option(20, "--limit")):
    """Run a label over logs where they live and show a page of results and the mean."""
    r = _do(LB.preview, ctx(), label, logs, limit)
    emit(r, f"{r['label']} over {r['logs']}: {r['groups']:,} {r['grain']} groups, mean {r['mean']}\n"
            + "\n".join("  " + "  ".join(str(v) for v in row) for row in r["rows"][:10]))


@label_app.command("compute")
def label_compute(label: str, logs: str = typer.Option(..., "--logs"), into: str = typer.Option(..., "--into")):
    """Write the label for every group as a dataset that pins the label and logs versions."""
    m = _do(LB.compute, ctx(), label, logs, into, dry_run=S.dry_run)
    _out(m, f"wrote {m.name}@v{m.version}: {m.row_count:,} rows")


# ---- replay -------------------------------------------------------------------------------


@replay_app.command("sweep")
def replay_sweep(name: str, logs: str = typer.Option(..., "--logs"), score: str = typer.Option(..., "--score", help="model score column"),
                 action: str = typer.Option(..., "--action", help="logged action column, 1 treated"),
                 outcome: list[str] = typer.Option(..., "--outcome", help="outcome column (repeatable)"),
                 propensity: Optional[str] = typer.Option(None, "--propensity", help="column with P(treated); default 0.5"),
                 direct: Optional[list[str]] = typer.Option(None, "--direct", help="outcomes the policy controls directly"),
                 points: int = typer.Option(20, "--points")):
    """Sweep a 'treat when score >= t' policy over randomized logs and forecast each outcome."""
    m = _do(RP.sweep, ctx(), name, logs, score, action, outcome, propensity, direct, points, dry_run=S.dry_run)
    lines = [f"{'threshold':>10} {'trigger':>8} " + " ".join(f"{o[:14]:>16}" for o in m.outcomes)]
    for p in m.curve:
        lines.append(f"{p['threshold']:10.4f} {p['trigger_rate']:8.1%} "
                     + " ".join(f"{(p['outcomes'][o]['delta_vs_never'] or 0):+16.4f}" for o in m.outcomes))
    _out(m, "\n".join(lines) + f"\n(change versus never treating, self-normalised) [{m.evidence.value}]\n{m.note}")


@replay_app.command("list")
def replay_list():
    c = ctx()
    rows = [c.registry.load("replay", n).model_dump(mode="json", exclude={"curve"}) for n in c.registry.names("replay")]
    emit(rows, "\n".join(f"{r['name']}@v{r['version']}  {r['logs']}  {r['description']}" for r in rows) or "no replays")


# ---- bench --------------------------------------------------------------------------------


@bench_app.command("rows")
def bench_rows(dataset: str, page: int = typer.Option(200, "--page")):
    """Time paging, counting and sorting a dataset where it lives. Keeps the result."""
    r = _do(BE.rows, ctx(), dataset, page, dry_run=S.dry_run)
    emit(r, f"{r['dataset']}: {r['rows']:,} rows on {r['where']}, worst {r['worst_ms']:,} ms\n"
            + "\n".join(f"  {t['step']:28} {t['ms']:>7,} ms" for t in r["timings"]))


@bench_app.command("list")
def bench_list():
    rows = _do(BE.listing, ctx())
    emit(rows, "\n".join(f"{r['at']}  {r['dataset']:36} {r['rows']:>12,} rows  worst {r['worst_ms']:,} ms  {r['where']}" for r in rows)
         or "no benchmarks")


# ---- handoff ------------------------------------------------------------------------------

from .. import handoff as HO  # noqa: E402

handoff_app = typer.Typer(no_args_is_help=True, help="Candidates handed to your own deployment process, and what happened to them")
app.add_typer(handoff_app, name="handoff")


@handoff_app.command("list")
def handoff_list(pipeline: Optional[str] = typer.Option(None, "--pipeline")):
    rows = _do(HO.listing, ctx(), pipeline)
    emit(rows, "\n".join(f"{b['id']}  {b['pipeline']:28} {b['environment']:10} {len(b['links'])} link(s)  {b['created_at']}" for b in rows)
         or "no handoffs. A promote to a live environment creates one.")


@handoff_app.command("show")
def handoff_show(hid: str):
    b = _do(HO.get, ctx(), hid)
    emit(b, f"{b['pipeline']} for {b['environment']}\nlimitations:\n" + "\n".join(f"  - {x}" for x in b["limitations"]) + f"\n{b['next']}")


@handoff_app.command("link")
def handoff_link(hid: str, system: str = typer.Option(..., "--system", help="your deployment system, e.g. argo"),
                 deployment_id: str = typer.Option(..., "--deployment-id"), url: Optional[str] = typer.Option(None, "--url"),
                 not_live: bool = typer.Option(False, "--not-live", help="recorded, but not what serves traffic now")):
    """Record that your deployment process deployed this candidate. This is what marks a live environment's current version."""
    if S.dry_run:
        return emit({"would_link": hid, "system": system, "deployment_id": deployment_id})
    b = _do(HO.link, ctx(), hid, system, deployment_id, url, not not_live)
    emit(b, f"linked {hid} to {system} {deployment_id}")


@handoff_app.command("outcome")
def handoff_outcome(hid: str, metric: str = typer.Option(..., "--metric"), value: float = typer.Option(..., "--value"),
                    source: str = typer.Option(..., "--source", help="the experiment system and report it comes from"),
                    experiment: Optional[str] = typer.Option(None, "--experiment"),
                    ci_low: Optional[float] = typer.Option(None, "--ci-low"), ci_high: Optional[float] = typer.Option(None, "--ci-high")):
    """Attach an online result from your experiment system (REAL_TRAFFIC)."""
    if S.dry_run:
        return emit({"would_record": hid, "metric": metric, "value": value})
    b = _do(HO.outcome, ctx(), hid, metric, value, source, experiment, ci_low, ci_high)
    emit(b, f"recorded {metric} {value:+g} on {hid} [REAL_TRAFFIC]")
