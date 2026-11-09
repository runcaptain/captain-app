"""Workflows: saved chains of cst commands, built on the app's canvas (or in YAML) and run by hand
or on a cron schedule while a daemon is running.

A step is {id, command, args, needs}. Args can read earlier outputs and workflow params:
  "${params.catalog}"                 a workflow parameter
  "${steps.gen.name}@v${steps.gen.version}"   fields of step `gen`'s JSON output
Each run is a write-once WorkflowRun with every step's command line, exit code, output and timing.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

import yaml

from . import commands as CMD
from .model.entities import Workflow, WorkflowRun, WorkflowStep
from .storage.backend import NotFound
from .storage.registry import check_name
from .workspace import Context, whoami

FALSY = {"", "false", "0", "none", "null", "no"}
TPL = re.compile(r"\$\{(params|steps)\.([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_.\-\[\]]+))?\}")

TEMPLATES = {
    "simulated-backtest": {
        "description": "Generate and seal queries from a catalog, grade two pipelines, simulate shoppers, backtest.",
        "params": {"catalog": "demo-catalog@v1", "baseline": "demo-bm25@v1", "challenger": "demo-tuned@v1", "name": "wf-backtest"},
        "steps": [
            {"id": "gen", "command": ["queries", "gen"], "args": {"catalog": "${params.catalog}", "name": "${params.name}/queries", "n": 300}},
            {"id": "seal", "command": ["queries", "seal"], "args": {"ref": "${steps.gen.name}@v${steps.gen.version}", "holdout": 0.5}, "needs": ["gen"]},
            {"id": "judge", "command": ["judge", "run"], "args": {"queries": "${steps.seal.name}@v${steps.seal.version}:holdout",
                                                                 "pipelines": "${params.baseline},${params.challenger}", "judge": "heuristic",
                                                                 "into": "${params.name}/judgments"}, "needs": ["seal"]},
            {"id": "shoppers", "command": ["sim", "population", "create"], "args": {"name": "${params.name}/shoppers", "size": 4000}},
            {"id": "plan", "command": ["backtest", "plan"], "args": {"name": "${params.name}", "arms": "${params.baseline},${params.challenger}",
                                                                    "queries": "${steps.seal.name}@v${steps.seal.version}:holdout",
                                                                    "judgments": "${steps.judge.name}@v${steps.judge.version}",
                                                                    "population": "${steps.shoppers.name}@v${steps.shoppers.version}",
                                                                    "hypothesis": "The challenger beats the baseline on nDCG@10."},
             "needs": ["judge", "shoppers"]},
            {"id": "run", "command": ["backtest", "run"], "args": {"name": "${params.name}"}, "needs": ["plan"]},
        ],
    },
    "historic-replay": {
        "description": "Replay a logs dataset: grades from clicks, logged query mix, traffic-weighted nDCG.",
        "params": {"logs": "logs/demo-search-sessions@v1", "baseline": "demo-bm25@v1", "challenger": "demo-tuned@v1", "name": "wf-replay"},
        "steps": [
            {"id": "plan", "command": ["backtest", "plan"], "args": {"name": "${params.name}", "mode": "historic", "logs": "${params.logs}",
                                                                    "arms": "${params.baseline},${params.challenger}", "primary": "ndcg@10_tw",
                                                                    "hypothesis": "The challenger beats the baseline on logged traffic."}},
            {"id": "run", "command": ["backtest", "run"], "args": {"name": "${params.name}"}, "needs": ["plan"]},
        ],
    },
    "retrain-and-review": {
        "description": "Record live metrics for an environment. If the metric fell past the threshold, retrain the deployed "
                       "pipeline and backtest it against what is live. It stops at a candidate ready for review: Captain "
                       "never promotes a model. Promotion is the customer's own deployment process.",
        "params": {"env": "demo-staging", "logs": "logs/demo-search-sessions@v1", "metric": "ctr", "drop": 0.05,
                   "pipeline": "demo-tuned", "queries": "demo-queries@v2:holdout", "judgments": "demo-judgments@v1",
                   "name": "wf-retrain"},
        "schedule": "0 7 * * 1",
        "steps": [
            {"id": "live", "command": ["deploy", "status"], "args": {"env": "${params.env}"}},
            {"id": "record", "command": ["monitor", "record"], "args": {"env": "${params.env}", "logs": "${params.logs}"}},
            {"id": "check", "command": ["monitor", "check"], "args": {"env": "${params.env}", "metric": "${params.metric}",
                                                                     "drop": "${params.drop}"}, "needs": ["record"]},
            # wait: on a compute target the step blocks until the job records the new version, so `plan` below
            # compares against the retrained version, never the one it was retrained from.
            {"id": "retrain", "command": ["pipeline", "retrain"], "args": {"name": "${params.pipeline}", "local_exec": True, "wait": True},
             "needs": ["check"], "when": "${steps.check.degraded}"},
            {"id": "plan", "command": ["backtest", "plan"], "args": {
                "name": "${params.name}", "arms": "${steps.live.pipeline},${steps.retrain.pipeline}",
                "queries": "${params.queries}", "judgments": "${params.judgments}",
                "hypothesis": "The retrained pipeline beats the one that is live."}, "needs": ["live", "retrain"]},
            {"id": "run", "command": ["backtest", "run"], "args": {"name": "${params.name}"}, "needs": ["plan"]},
        ],
    },
    "nightly-zero-results": {
        "description": "Snapshot last day's zero-result searches into a dated dataset.",
        "params": {"conn": "demo-store"},
        "schedule": "0 6 * * *",
        "steps": [{"id": "mine", "command": ["sql"], "args": {"conn_name": "${params.conn}",
                   "execute": "select query, count(*) n from search_log where results = 0 group by 1 order by n desc",
                   "save": "mining/zero-results", "kind": "queries"}}],
    },
}


def save(ctx: Context, name: str, steps: list[dict], params: dict | None = None, schedule: str | None = None,
         description: str = "") -> Workflow:
    check_name(name)
    st = [WorkflowStep.model_validate(s) for s in steps]
    ids = [s.id for s in st]
    if len(set(ids)) != len(ids):
        raise ValueError("step ids must be unique")
    for s in st:
        for n in s.needs:
            if n not in ids:
                raise ValueError(f"step {s.id} needs unknown step {n}")
        CMD.argv(s.command, {k: v for k, v in s.args.items() if not (isinstance(v, str) and "${" in v)})  # validates names
    order(st)
    if schedule:
        _cron_fields(schedule)
    w = Workflow(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
                 steps=st, params=params or {}, schedule=schedule or None, description=description)
    return ctx.registry.publish(w)


def order(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    done, out, left = set(), [], list(steps)
    while left:
        ready = [s for s in left if set(s.needs) <= done]
        if not ready:
            raise ValueError("the steps form a cycle")
        for s in ready:
            out.append(s)
            done.add(s.id)
            left.remove(s)
    return out


def _resolve(v, params, outputs):
    if not isinstance(v, str):
        return v

    def sub(m):
        src, key, path = m.group(1), m.group(2), m.group(3)
        if src == "params":
            if key not in params:
                raise ValueError(f"unknown param {key}")
            return str(params[key])
        if key not in outputs:
            raise ValueError(f"step {key} has no output yet")
        cur = outputs[key]
        for part in (path or "").split(".") if path else []:
            cur = cur[int(part)] if isinstance(cur, list) else cur[part]
        return str(cur)

    whole = TPL.fullmatch(v)
    if whole and whole.group(1) == "params":
        return params.get(whole.group(2))
    return TPL.sub(sub, v)


def run(ctx: Context, name: str, version: int | None = None, params: dict | None = None, trigger: str = "manual",
        on_step=None) -> WorkflowRun:
    w = ctx.registry.load("workflow", name, version)
    p = {**w.params, **(params or {})}
    outputs: dict = {}
    results, failed, skipped = [], False, set()
    for s in order(w.steps):
        if failed:
            results.append({"id": s.id, "status": "skipped", "reason": "an earlier step failed"})
            continue
        if skipped & set(s.needs):
            skipped.add(s.id)
            res = {"id": s.id, "status": "skipped", "reason": f"needs skipped step {sorted(skipped & set(s.needs))[0]}"}
            results.append(res)
            if on_step:
                on_step(res)
            continue
        if s.when is not None:
            try:
                cond = _resolve(s.when, p, outputs)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                cond, err = None, str(e)
            else:
                err = None
            if err or str(cond).strip().lower() in FALSY:
                skipped.add(s.id)
                res = {"id": s.id, "status": "skipped", "reason": f"condition could not be read: {err}" if err else "condition false",
                       "when": s.when}
                results.append(res)
                if on_step:
                    on_step(res)
                continue
        t0 = time.time()
        try:
            args = {k: _resolve(v, p, outputs) for k, v in s.args.items()}
            r = CMD.run(s.command, args, workspace=ctx.workspace.name)
        except Exception as e:  # noqa: BLE001
            r = {"command": "cst " + " ".join(s.command), "exit_code": 1, "output": None, "error": str(e)}
        ok = r["exit_code"] == 0
        outputs[s.id] = r["output"] if isinstance(r["output"], (dict, list)) else {"text": r["output"]}
        res = {"id": s.id, "status": "done" if ok else "failed", "command": r["command"], "exit_code": r["exit_code"],
               "error": r["error"], "ms": int((time.time() - t0) * 1000),
               "output": r["output"] if isinstance(r["output"], (dict, list)) and len(str(r["output"])) < 20000 else str(r["output"])[:4000]}
        results.append(res)
        if on_step:
            on_step(res)
        failed = not ok
    wr = WorkflowRun(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
                     workflow=f"{w.name}@v{w.version}", status="failed" if failed else "done", steps=results, trigger=trigger)
    return ctx.registry.publish(wr)


def listing(ctx: Context) -> list[dict]:
    out = []
    for n in ctx.registry.names("workflow"):
        w = ctx.registry.load("workflow", n)
        runs = ctx.registry.versions("workflow-run", n)
        last = ctx.registry.load("workflow-run", n, runs[-1]) if runs else None
        out.append({"name": n, "latest": w.version, "steps": len(w.steps), "schedule": w.schedule, "description": w.description,
                    "runs": len(runs), "last_status": last.status if last else None,
                    "last_run": last.created_at.isoformat() if last else None})
    return out


def to_yaml(w: Workflow) -> str:
    return yaml.safe_dump({"name": w.name, "description": w.description, "schedule": w.schedule, "params": w.params,
                           "steps": [s.model_dump(exclude_none=True) for s in w.steps]}, sort_keys=False)


# ---- schedule ------------------------------------------------------------------------------


def _cron_fields(expr: str) -> list[set[int]]:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("a schedule is five cron fields: minute hour day month weekday (UTC)")
    rng = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    out = []
    for f, (lo, hi) in zip(parts, rng):
        vals = set()
        for piece in f.split(","):
            step = 1
            if "/" in piece:
                piece, st = piece.split("/")
                step = int(st)
            if piece == "*":
                a, b = lo, hi
            elif "-" in piece:
                a, b = map(int, piece.split("-"))
            else:
                a = b = int(piece)
            if a < lo or b > hi:
                raise ValueError(f"cron value out of range in {expr!r}")
            vals |= set(range(a, b + 1, step))
        out.append(vals)
    return out


def due(expr: str, t: dt.datetime) -> bool:
    m, h, d, mo, wd = _cron_fields(expr)
    return t.minute in m and t.hour in h and t.day in d and t.month in mo and (t.weekday() + 1) % 7 in wd


def start_scheduler(get_ctx) -> threading.Thread:
    """Checks every 30 s and runs workflows whose schedule matches the current UTC minute, once."""
    fired: set = set()

    def loop():
        while True:
            try:
                ctx = get_ctx()
                now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
                for item in listing(ctx):
                    key = (item["name"], now)
                    if item["schedule"] and key not in fired and due(item["schedule"], now):
                        fired.add(key)
                        threading.Thread(target=run, args=(ctx, item["name"]), kwargs={"trigger": "schedule"}, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(30)

    th = threading.Thread(target=loop, daemon=True, name="cst-scheduler")
    th.start()
    return th
