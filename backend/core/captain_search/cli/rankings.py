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
