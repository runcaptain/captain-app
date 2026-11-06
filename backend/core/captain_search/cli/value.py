"""cst commands for cost against value, live monitoring and retraining."""
from __future__ import annotations

from typing import Optional

import typer

from .. import costs as COST
from .. import monitor as MON
from .. import retrain as RT
from .main import S, app, ctx, emit
from .more import _do, pipeline_app

cost_app = typer.Typer(no_args_is_help=True, help="What each pipeline version costs, next to what it gains")
pcost_app = typer.Typer(no_args_is_help=True, help="Record a pipeline version's training and serving cost")
monitor_app = typer.Typer(no_args_is_help=True, help="Live metrics of what is deployed, from search logs")
app.add_typer(cost_app, name="cost")
app.add_typer(monitor_app, name="monitor")
pipeline_app.add_typer(pcost_app, name="cost")


def _money(v, fmt=",.2f"):
    return "-" if v is None else f"${v:{fmt}}"


# ---- cost ---------------------------------------------------------------------------------


@pcost_app.command("set")
def pcost_set(pipeline: str, train_usd: Optional[float] = typer.Option(None, "--train-usd", help="what one training run cost"),
              train_gpu_hours: Optional[float] = typer.Option(None, "--train-gpu-hours"),
              gpu_type: Optional[str] = typer.Option(None, "--gpu-type", help="for example H100 or A10G"),
              serve_usd_per_1k: Optional[float] = typer.Option(None, "--serve-usd-per-1k", help="serving cost per 1,000 queries"),
              note: Optional[str] = typer.Option(None, "--note")):
    """Record what a pipeline version costs. Kept beside the version with a history of every change."""
    r = _do(COST.set_cost, ctx(), pipeline, {"train_usd": train_usd, "train_gpu_hours": train_gpu_hours, "gpu_type": gpu_type,
                                              "serve_usd_per_1k": serve_usd_per_1k, "note": note}, dry_run=S.dry_run)
    emit(r, f"{'would set' if S.dry_run else 'set'} cost for {r['pipeline']}")


@pcost_app.command("show")
def pcost_show(pipeline: str):
    """A pipeline version's recorded cost and its history."""
    r = _do(COST.get, ctx(), pipeline)
    emit(r, f"{r['pipeline']}: train {_money(r['train_usd'])} ({r['train_gpu_hours'] or '-'} GPU h {r['gpu_type'] or ''}), "
            f"serve {_money(r['serve_usd_per_1k'], '.4f')} per 1k queries" + (f"\nnote: {r['note']}" if r['note'] else ""))


@cost_app.command("ledger")
def cost_ledger():
    """Every pipeline version with its cost and its latest backtest gain. Flags versions to retire."""
    rows = _do(COST.ledger, ctx())
    lines = []
    for r in rows:
        roi = r["roi"] or {}
        lines.append(f"{r['pipeline']:34} train {_money(r['train_usd']):>10}  serve/1k {_money(r['serve_usd_per_1k'], '.4f'):>9}  "
                     f"p99 {('-' if r['p99_ms'] is None else f'{r['p99_ms']:.1f} ms'):>9}  "
                     + (f"{roi['metric']} {roi['delta']:+.4f} {roi['verdict']}  {(roi.get('flag') or 'no cost').upper()}" if roi else "no backtest"))
    emit(rows, "\n".join(lines) or "no pipelines")


@cost_app.command("settings")
def cost_settings(queries_per_day: Optional[int] = typer.Option(None, "--queries-per-day",
                                                                help="traffic used to price serving cost per month")):
    """Show or set the traffic the ledger uses to turn serving cost per 1k queries into dollars a month."""
    c = ctx()
    r = _do(COST.set_settings, c, queries_per_day) if queries_per_day is not None and not S.dry_run else COST.settings(c)
    emit(r, f"queries per day: {r['queries_per_day'] or 'not set'}")


# ---- monitor ------------------------------------------------------------------------------


@monitor_app.command("record")
def mon_record(env: str = typer.Option(..., "--env"), logs: str = typer.Option(..., "--logs", help="events dataset: query, product_id, position, clicked"),
               since: Optional[str] = typer.Option(None, "--since", help="only rows with ts at or after this, e.g. 2026-11-01"),
               where: Optional[str] = typer.Option(None, "--where", help="SQL condition on the log rows"),
               pipeline: Optional[str] = typer.Option(None, "--pipeline", help="the pipeline these logs came from (default: what is deployed)")):
    """Measure live metrics from search logs and add a snapshot for the environment's pipeline."""
    r = _do(MON.record, ctx(), env, logs, since, where, pipeline, dry_run=S.dry_run)
    m = (r.get("would_record") or r)["metrics"]
    emit(r, f"{env}: {m['searches']:,} searches, CTR {m['ctr']:.4f}, MRR of clicks {m['mrr_click']:.4f}"
            + (f", conversion {m['conversion']:.4f}" if m.get("conversion") is not None else ""))


@monitor_app.command("check")
def mon_check(env: str = typer.Option(..., "--env"), metric: str = typer.Option("ctr", "--metric", help="ctr, conversion, mrr_click or clicks_at_3"),
              drop: float = typer.Option(0.05, "--drop", help="relative fall that counts as degraded, e.g. 0.05"),
              baseline: str = typer.Option("first", "--baseline", help="first, best or previous snapshot of this pipeline")):
    """Say whether the deployed pipeline's metric fell past a threshold. Exits 0 either way; read `degraded`."""
    r = _do(MON.check, ctx(), env, metric, drop, baseline)
    emit(r, ("DEGRADED: " if r["degraded"] else "ok: ") + r["reason"])


@monitor_app.command("show")
def mon_show(env: str = typer.Option(..., "--env")):
    """Every snapshot recorded for an environment."""
    r = _do(MON.show, ctx(), env)
    emit(r, "\n".join(f"{s['at']}  {s['pipeline']:28} CTR {s['metrics']['ctr']:.4f}  searches {s['metrics']['searches']:,}"
                      for s in r["snapshots"]) or f"no snapshots for {env}")


# ---- retrain ------------------------------------------------------------------------------


@pipeline_app.command("retrain")
def pipeline_retrain(name: str, compute: Optional[str] = typer.Option(None, "--compute", help="compute target (default: spec.train.compute or local)"),
                     local_exec: bool = typer.Option(False, "--local-exec", help="allow running the pipeline's training command on this machine")):
    """Run the pipeline's declared training command and publish the result as a new version."""
    r = _do(RT.retrain, ctx(), name, compute, local_exec, dry_run=S.dry_run)
    emit(r, f"published {r['pipeline']} from {r['parent']} in {r['wall_s']:.0f} s" if "wall_s" in r
         else (f"would run: {r['would_run']}" if "would_run" in r else f"submitted retrain of {r['pipeline']} to {r['compute']}"))
