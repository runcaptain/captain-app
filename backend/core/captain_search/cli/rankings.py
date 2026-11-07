"""cst rankings validate|import: bring a team's own ranked results into Captain and compare them."""
from __future__ import annotations

import json
import os
from typing import Optional

import typer

from .. import rankings as RK
from ..data import datasets as DS
from .main import app, ctx, emit, fail

rankings_app = typer.Typer(no_args_is_help=True, help="Your own system's ranked results, imported and compared")
app.add_typer(rankings_app, name="rankings")


def _mapping(m: Optional[str]) -> dict | None:
    """--map query_id=qid,arm=system"""
    if not m:
        return None
    try:
        return dict(x.split("=", 1) for x in m.split(","))
    except ValueError:
        fail("--map takes our_name=your_column pairs, e.g. query_id=qid,arm=system")


@rankings_app.command("validate")
def rk_validate(path: str, map: Optional[str] = typer.Option(None, "--map", help="query_id=qid,arm=system,..."),
                judgments: bool = typer.Option(False, "--has-judgments", help="you'll give a judgments dataset")):
    """Check a rankings file: columns query_id, arm, item_id, and rank or score (plus optional query, label,
    segment, weight)."""
    r = RK.validate(DS.read_file(os.path.expanduser(path)), _mapping(map), judgments)
    lines = ([f"ok: {r['stats']['rows']} rows, {r['stats']['queries']} queries, arms {', '.join(r['stats']['arms'])}"]
             if r["ok"] else []) + [f"error: {e}" for e in r["errors"]] + [f"note: {w}" for w in r["warnings"]]
    emit(r, "\n".join(lines))
    if not r["ok"]:
        raise typer.Exit(1)


@rankings_app.command("import")
def rk_import(name: str, path: str, map: Optional[str] = typer.Option(None, "--map"),
              baseline: Optional[str] = typer.Option(None, "--baseline", help="the arm to compare the others against"),
              labels_from: Optional[str] = typer.Option(None, "--labels-from", help="human, llm, clicks or heuristic"),
              judgments: Optional[str] = typer.Option(None, "--judgments", help="a judgments dataset instead of labels"),
              catalog: Optional[str] = typer.Option(None, "--catalog", help="a catalog dataset, for titles side by side"),
              primary: str = typer.Option("ndcg@10", "--primary"), mde: float = typer.Option(0.003, "--mde"),
              hypothesis: str = typer.Option("", "--hypothesis", "-m"), project: Optional[str] = typer.Option(None, "--project")):
    """Import ranked results as a comparison run: metrics with intervals, slices, and side by side."""
    try:
        r = RK.import_rankings(ctx(), name, path, mapping=_mapping(map), baseline=baseline, labels_from=labels_from,
                               judgments=judgments, catalog=catalog, primary_metric=primary, mde=mde,
                               hypothesis=hypothesis, project=project)
    except ValueError as e:
        fail(str(e))
    emit(r, f"imported {r['run']}: {r['validation']['stats']['queries']} queries, arms "
            f"{', '.join(r['validation']['stats']['arms'])}" + ("" if r["evidence"] else " (no labels: relevance metrics unavailable)"))


@app.command("explain")
def explain_cmd(pipeline: str, query: str = typer.Option(..., "--query", "-q"),
                vs: Optional[str] = typer.Option(None, "--vs", help="a second pipeline to compare with"),
                query_id: Optional[str] = typer.Option(None, "--query-id"),
                judgments: Optional[str] = typer.Option(None, "--judgments"), k: int = typer.Option(10, "-k")):
    """Why a pipeline ranked this: each stage, with score contributions. --vs shows where two pipelines diverge."""
    from .. import explain as EX

    c = ctx()
    out = EX.compare_traces(c, pipeline, vs, query, query_id, judgments, k) if vs else EX.trace(c, pipeline, query, query_id, judgments, k)
    traces = [out["a"], out["b"]] if vs else [out]
    lines = []
    for t in traces:
        lines.append(f"== {t['pipeline']}")
        for s in t["stages"]:
            lines.append(f"  {s['stage']}{'' if s['explained'] else ' (no explanation provided)'}: " + "; ".join(s["notes"]))
            for it in s["items"][:k] if s["kind"] in ("retrieve", "final") else []:
                top = ", ".join(f"{n} {v:.2f}" for n, v in sorted(it["score_parts"].items(), key=lambda kv: -kv[1])[:3])
                g = f" grade {it['grade']}" if it.get("grade") is not None else ""
                lines.append(f"    {it['rank']:>3}. {it['id']} {it.get('title') or ''}{g}" + (f"  [{top}]" if top else ""))
    if vs:
        fd = out["first_divergence"]
        lines.append(f"first divergence: {fd['stage']}: lost {len(fd['lost'])}, ranked lower {len(fd['ranked_lower'])}"
                     if fd else (out["note"] or "no relevant result was lost"))
    emit(out, "\n".join(lines))


@app.command("profile")
def profile_cmd(target: str = typer.Argument(..., help="a comparison run (name or name@vN), or a pipeline with --queries"),
                queries: Optional[str] = typer.Option(None, "--queries", help="a queries dataset (with :split) to profile a pipeline over"),
                max_queries: int = typer.Option(200, "--max-queries"), memory: bool = typer.Option(True, "--memory/--no-memory")):
    """Where the time goes, stage by stage: p50, p95 and p99 per stage, the slowest queries, and budget breaches."""
    from .. import ops
    from .. import profile as PF

    c = ctx()
    if queries:
        out = PF.profile_pipeline(c, target, ops.rows_of(c, queries)[1][:max_queries], memory=memory)
        arms = [out]
    else:
        out = PF.profile_run(c, target, max_queries, memory)
        arms = out["arms"]
    lines = []
    for a in arms:
        t = a["total"]
        lines.append(f"== {a['pipeline']}: p50 {t['p50_ms']} ms, p99 {t['p99_ms']} ms over {a['queries']} queries")
        for s in a["stages"]:
            lines.append(f"  {s['stage']:<28} p50 {s['p50_ms']:>8} ms  p99 {s['p99_ms']:>8} ms  {100 * (s['share'] or 0):5.1f}%")
        if a.get("budget"):
            lines += [f"  over budget: {b}" for b in a["budget"]["breaches"]] or ["  within budget"]
    lines += out.get("summary", []) if not queries else []
    lines.append(f"measured on {out['measured_on']['machine']}, {out['measured_on']['where']}")
    emit(out, "\n".join(lines))


evals_app = typer.Typer(no_args_is_help=True, help="Living evaluation sets: health and refresh")
app.add_typer(evals_app, name="evals")


@evals_app.command("health")
def evals_health_cmd(queries: str, judgments: str, logs: Optional[str] = typer.Option(None, "--logs"),
                     catalog: Optional[str] = typer.Option(None, "--catalog"),
                     lifetime_days: int = typer.Option(180, "--lifetime-days")):
    """Does this evaluation set still tell the truth? Traffic it covers, stale grades, coverage, agreement."""
    from .. import evals as EV

    h = EV.health(ctx(), queries, judgments, logs, catalog, lifetime_days)
    lines = [f"{h['queries']} with {h['judgments']}: {h['status']}"] + [f"  - {f}" for f in h["findings"]]
    if h.get("representativeness"):
        r = h["representativeness"]
        lines.append(f"  traffic covered {r['traffic_covered']:.0%} (head {r['by_band']['head']:.0%}, torso "
                     f"{r['by_band']['torso']:.0%}, tail {r['by_band']['tail']:.0%})")
    emit(h, "\n".join(lines))


@evals_app.command("refresh")
def evals_refresh_cmd(queries: str, judgments: str, logs: str = typer.Option(..., "--logs"),
                      catalog: Optional[str] = typer.Option(None, "--catalog"), add: int = typer.Option(50, "--add"),
                      seed: int = typer.Option(7, "--seed"), dry_run: bool = typer.Option(False, "--plan", help="show the plan only")):
    """Refresh as new versions: add queries sampled from traffic, drop grades on retired products, list what to grade."""
    from .. import evals as EV

    out = EV.refresh(ctx(), queries, judgments, logs, catalog, add, seed, dry_run)
    emit(out, "\n".join(out["changelog"]) + ("" if dry_run else f"\npublished {out['queries']} and {out['judgments']}"))


@app.command("sweep")
def sweep_cmd(pipeline: str, queries: str = typer.Option(..., "--queries"), judgments: str = typer.Option(..., "--judgments"),
              knob: list[str] = typer.Option([], "--knob", help="path=v1,v2,v3 or path=min:max:steps, e.g. k1=0.8:2.0:4"),
              sample: int = typer.Option(60, "--sample"), top: int = typer.Option(3, "--top")):
    """What-if: turn up to three knobs and see nDCG@10 move, on a sample first, then the best on every query."""
    from .. import sandbox as SB

    knobs = {}
    for k in knob:
        path, _, spec = k.partition("=")
        if ":" in spec:
            lo, hi, n = spec.split(":")
            knobs[path] = {"min": float(lo), "max": float(hi), "steps": int(n)}
        else:
            knobs[path] = {"values": [float(x) for x in spec.split(",")]}
    r = SB.sweep(ctx(), pipeline, queries, judgments, knobs or None, sample=sample, top=top)
    lines = [f"{r['pipeline']}, current {r['current']}, {r['metric']} {r['baseline_mean']} ({r['evidence']})",
             f"sample of {r['sample']['n']}:"]
    for c in sorted(r["sample"]["cells"], key=lambda c: -c["delta"])[:8]:
        lines.append(f"  {c['values']}  {c['mean']}  {c['delta']:+.4f}{'  (current)' if c['is_current'] else ''}")
    lines.append(f"every query ({r['full']['n']}):")
    for c in r["full"]["cells"]:
        flips = "; ".join(f"{f['slice']} {f['delta']:+.3f}" for f in c["slice_flips"])
        lines.append(f"  {c['values']}  {c['delta']:+.4f} [{c['ci'][0]:+.4f}, {c['ci'][1]:+.4f}] {c['verdict']}"
                     + (f"  slices: {flips}" if flips else ""))
    lines.append(r["note"])
    emit(r, "\n".join(lines))
