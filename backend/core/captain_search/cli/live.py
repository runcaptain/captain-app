"""cst live: runs as they happen, for Runs in the app. Anything can stream here: Captain's backtests and retrains do,
and so can a team's own simulator or trainer, by printing JSON lines through `cst live pipe` or calling emit."""
from __future__ import annotations

import json
import time
from typing import Optional

import typer

from .. import live as LV
from .main import S, app, emit
from .more import _do

live_app = typer.Typer(no_args_is_help=True, help="Live runs: stream a backtest, training or your own tool into Runs")
app.add_typer(live_app, name="live")


def _line(h: dict) -> str:
    pct = f"{h['progress'] * 100:3.0f}%" if isinstance(h.get("progress"), (int, float)) else "   "
    return f"{h['id']}  {h['status']:8} {pct}  {h['kind']:9} {h['title']}" + (f"  ({h['stage']})" if h.get("stage") else "")


@live_app.command("list")
def live_list(active: bool = typer.Option(False, "--active", help="only runs still going"), limit: int = typer.Option(30, "--limit")):
    rows = LV.listing(limit, active)
    emit(rows, "\n".join(_line(h) for h in rows) or "no live runs")


@live_app.command("start")
def live_start(title: str = typer.Option(..., "--title"), kind: str = typer.Option("custom", "--kind", help="backtest, training, sweep or custom"),
               of: Optional[str] = typer.Option(None, "--of", help="what it's a run of: a pipeline, a backtest"),
               arms: str = typer.Option("", "--arms", help="the versions compared, comma-separated")):
    """Begin a run another program reports to. Prints its id. The run stays open until `cst live end`."""
    import os

    # The run belongs to the shell that started it, so it reads as stopped if that shell goes away without ending it.
    r = _do(LV.start, kind, title, of=of, arms=[a for a in arms.split(",") if a], source="cli", pid=os.getppid())
    emit({"id": r.id}, r.id)


@live_app.command("emit")
def live_emit(rid: str, event: str = typer.Argument(..., help="stage, progress, metric, shopper, log or result"),
              data: str = typer.Option("{}", "--data", "-d", help="the event's fields as JSON, e.g. '{\"name\": \"loss\", \"value\": 0.3, \"step\": 100}'")):
    """Add one event to a run. See `cst live --help` for the fields each event takes."""
    try:
        fields = json.loads(data)
    except ValueError as e:
        raise typer.BadParameter(f"--data isn't JSON: {e}") from None
    if not isinstance(fields, dict):
        raise typer.BadParameter("--data is a JSON object")
    r = _do(LV.attach, rid)
    r.emit(event, **{k: v for k, v in fields.items() if k not in ("type", "at", "n")})
    emit({"id": rid, "event": event}, "")


@live_app.command("end")
def live_end(rid: str, failed: bool = typer.Option(False, "--failed"), summary: Optional[str] = typer.Option(None, "--summary"),
             ref: Optional[str] = typer.Option(None, "--ref", help="what the run made, e.g. ranker@v7")):
    r = _do(LV.attach, rid)
    r.end("failed" if failed else "done", ref=ref, summary=summary)
    emit(LV.header(rid), f"ended {rid}")


@live_app.command("pipe", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def live_pipe(command: list[str] = typer.Argument(..., help="the command to run, after --"),
              title: str = typer.Option(..., "--title"), kind: str = typer.Option("custom", "--kind"),
              of: Optional[str] = typer.Option(None, "--of")):
    """Run a command and stream it into Runs: a printed JSON object with a "type" is an event, any other line a log
    line. For example a simulator printing {"type": "shopper", "arm": "a", "query": "...", "bought": true}."""
    out = _do(LV.pipe, command, kind, title, of)
    emit(out, f"{out['id']}: exit code {out['exit_code']}")
    if out["exit_code"]:
        raise typer.Exit(out["exit_code"])


@live_app.command("watch")
def live_watch(rid: str):
    """Print a run's events as they come, until it ends."""
    n = 0
    while True:
        got = LV.read(rid, since=n)
        for e in got["events"]:
            typer.echo(json.dumps(e) if S.json else f"{e['at']:8.1f}s  {e['type']:8} " + json.dumps({k: v for k, v in e.items() if k not in ('type', 'at', 'n')}))
        n = got["next"]
        if got["run"]["status"] != "running":
            break
        time.sleep(1)
