"""Experiment proposals: an agent (or a person) proposes, a person approves, then it runs.

Agents can propose features, run experiments and form hypotheses, with people
keeping validation. A proposal records the hypothesis, the arms to compare and the data to compare them on,
who wrote it, and whether an agent wrote it. Nothing runs until a person approves it.

Status: proposed, then approved or rejected, then ran (a backtest plan and run exist for it).
Agent sessions mark themselves with CST_AGENT=<name> (or --agent). An agent can propose, but it cannot
approve or reject, and it cannot approve its own work under a person's name: approving needs a session
without CST_AGENT. A live promote of a pipeline that came from an agent proposal needs that approval.

Stored as proposals/<id>.yaml, updated by compare-and-swap, with a history of every status change.
"""
from __future__ import annotations

import datetime as dt
import os

import yaml

from .model.entities import new_id
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

STATUSES = ("proposed", "approved", "rejected", "ran")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _key(pid: str) -> str:
    return f"proposals/{pid}.yaml"


def agent_name(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("CST_AGENT") or None


def _load(ctx: Context, pid: str):
    try:
        data, tag = ctx.store.get_with_tag(_key(pid))
    except NotFound:
        raise NotFound(f"no proposal {pid}") from None
    return yaml.safe_load(data), tag


def _save(ctx: Context, doc: dict, tag) -> dict:
    try:
        ctx.store.put_if(_key(doc["id"]), dump_yaml(doc), tag)
    except Conflict:
        raise Conflict("someone changed this proposal at the same moment. Reload and try again.") from None
    return doc


def create(ctx: Context, title: str, hypothesis: str, arms: list[str], queries: str, judgments: str | None = None,
           population: str | None = None, primary: str = "ndcg@10", mde: float = 0.003, rationale: str = "",
           agent: str | None = None, dry_run: bool = False) -> dict:
    if not title.strip() or not hypothesis.strip():
        raise ValueError("a proposal needs a title and a hypothesis")
    if len(arms) < 2:
        raise ValueError("a proposal compares at least two arms. The first is the baseline.")
    pinned = []
    for a in arms:
        pm, _ = ctx.registry.resolve("pipeline", a)
        pinned.append(f"{pm.name}@v{pm.version}")
    ctx.registry.resolve("dataset", queries)
    if judgments:
        ctx.registry.resolve("dataset", judgments)
    ag = agent_name(agent)
    doc = {"id": new_id().lower(), "title": title.strip(), "hypothesis": hypothesis.strip(), "rationale": rationale,
           "arms": pinned, "queries": queries, "judgments": judgments, "population": population, "primary": primary,
           "mde": mde, "author": whoami(), "agent": ag, "status": "proposed", "created_at": _now(),
           "review": None, "backtest": None, "run": None,
           "history": [{"at": _now(), "by": whoami(), "agent": ag, "to": "proposed"}]}
    if dry_run:
        return doc
    ctx.store.put_new(_key(doc["id"]), dump_yaml(doc))
    return doc


def get(ctx: Context, pid: str) -> dict:
    return _load(ctx, pid)[0]


def listing(ctx: Context, status: str | None = None) -> list[dict]:
    out = []
    for k in ctx.store.list("proposals/"):
        if k.endswith(".yaml"):
            d = yaml.safe_load(ctx.store.get(k))
            if status is None or d["status"] == status:
                out.append(d)
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def review(ctx: Context, pid: str, decision: str, note: str = "", agent: str | None = None) -> dict:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision is approved or rejected")
    if agent_name(agent):
        raise ValueError("an agent session cannot approve or reject a proposal. A person reviews it from a session "
                         "without CST_AGENT, or in the app.")
    doc, tag = _load(ctx, pid)
    if doc["status"] != "proposed":
        raise ValueError(f"proposal {pid} is {doc['status']}, so it cannot be {decision}")
    doc["status"] = decision
    doc["review"] = {"by": whoami(), "at": _now(), "decision": decision, "note": note}
    doc["history"].append({"at": _now(), "by": whoami(), "agent": None, "to": decision, "note": note})
    return _save(ctx, doc, tag)


def run(ctx: Context, pid: str, progress=None) -> dict:
    from . import ops

    doc, tag = _load(ctx, pid)
    if doc["status"] != "approved":
        raise ValueError(f"proposal {pid} is {doc['status']}. Only an approved proposal runs.")
    name = f"proposals/{pid[:10]}"
    hyp = f"{doc['hypothesis']} (proposal {pid}{', written by agent ' + doc['agent'] if doc['agent'] else ''})"
    bt = ops.backtest_plan(ctx, name, doc["arms"], doc["queries"], doc["judgments"], doc["population"], hyp,
                           doc["primary"], doc["mde"])
    r = ops.backtest_run(ctx, name, progress=progress)
    doc, tag = _load(ctx, pid)
    doc["status"], doc["backtest"], doc["run"] = "ran", f"{bt.name}@v{bt.version}", f"{r.name}@v{r.version}"
    prim = next((c for c in r.comparisons if c.get("primary")), None)
    doc["result"] = {k: prim[k] for k in ("metric", "a", "b", "delta", "ci_low", "ci_high", "verdict")} if prim else None
    doc["history"].append({"at": _now(), "by": whoami(), "agent": agent_name(), "to": "ran", "run": doc["run"]})
    return _save(ctx, doc, tag)


def approval_for(ctx: Context, pipeline_ref: str) -> dict | None:
    """The agent-written proposal that introduced this pipeline as a challenger, if any."""
    for d in listing(ctx):
        if d.get("agent") and pipeline_ref in d["arms"][1:]:
            return d
    return None
