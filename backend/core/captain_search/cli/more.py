"""cst commands for evaluation, pipelines, backtests, deploys, notebooks, skills, the agent, the daemon
and the desktop app. Registered onto the main app in main.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

import typer
import yaml

from .. import ops
from ..storage.backend import NotFound
from .main import S, app, ctx, emit, fail

queries_app = typer.Typer(no_args_is_help=True, help="Query sets: generate and seal")
judge_app = typer.Typer(no_args_is_help=True, help="Relevance judgments")
pipeline_app = typer.Typer(no_args_is_help=True, help="Search pipelines (write-once versions)")
sim_app = typer.Typer(no_args_is_help=True, help="Simulated shoppers")
pop_app = typer.Typer(no_args_is_help=True, help="Shopper populations")
backtest_app = typer.Typer(no_args_is_help=True, help="Backtests: offline nDCG and simulated A/B")
deploy_app = typer.Typer(no_args_is_help=True, help="Deploys with gates and rollback")
env_app = typer.Typer(no_args_is_help=True, help="Deploy environments")
notebook_app = typer.Typer(no_args_is_help=True, help="Notebooks")
notebook_cell_app = typer.Typer(no_args_is_help=True, help="Add, edit and remove one cell")
notebook_app.add_typer(notebook_cell_app, name="cell")
skill_app = typer.Typer(no_args_is_help=True, help="Agent skills")
demo_app = typer.Typer(no_args_is_help=True, help="A built-in demo store")
sim_app.add_typer(pop_app, name="population")
deploy_app.add_typer(env_app, name="env")
for name, t in [("queries", queries_app), ("judge", judge_app), ("pipeline", pipeline_app), ("sim", sim_app),
                ("backtest", backtest_app), ("deploy", deploy_app), ("notebook", notebook_app), ("skill", skill_app),
                ("demo", demo_app)]:
    app.add_typer(t, name=name)


def _do(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ops.GateFailed as e:
        if S.json:
            typer.echo(json.dumps({"error": str(e), "gate_results": e.results}, default=str))
        else:
            typer.secho(f"refused: {e}", fg="red", err=True)
            for r in e.results:
                typer.echo(f"  [{'ok ' if r['ok'] else 'FAIL'}] {r['gate']}: {r['detail']}", err=True)
        raise typer.Exit(3)
    except (ValueError, NotFound, KeyError) as e:
        fail(str(e).strip("'\""))


def _out(m, human: str):
    emit(m.model_dump(mode="json") if hasattr(m, "model_dump") else m, human)


# ---- queries ------------------------------------------------------------------------------


@queries_app.command("gen")
def queries_gen(catalog: str = typer.Option(..., "--catalog"), name: str = typer.Option(..., "--name"),
                classes: str = typer.Option("head,brand,multi-attr,numeric,model-code,typo,zero-result", "--classes"),
                n: int = typer.Option(1000, "--n"), seed: int = typer.Option(7, "--seed")):
    """Generate queries from a catalog dataset, by failure class."""
    m = _do(ops.queries_gen, ctx(), catalog, name, classes.split(","), n, seed, dry_run=S.dry_run)
    _out(m, f"{'would save' if S.dry_run else 'saved'} {m.name}@v{m.version}: {m.row_count} queries")


@queries_app.command("seal")
def queries_seal(ref: str, holdout: float = typer.Option(0.3, "--holdout")):
    """Split into train and holdout and seal the holdout with a sha256. Writes a new version."""
    m = _do(ops.queries_seal, ctx(), ref, holdout, dry_run=S.dry_run)
    _out(m, f"sealed {m.name}@v{m.version}: train {len(m.splits['train'])}, holdout {len(m.splits['holdout'])}\n"
            f"seal {m.holdout_seal}\nTune on {m.name}@v{m.version}:train. Report on :holdout.")


@queries_app.command("check")
def queries_check(ref: str):
    """Check that a sealed holdout has not changed."""
    r = _do(ops.check_seal, ctx(), ref)
    emit(r, f"{'ok' if r['ok'] else 'BROKEN'}: {r['detail']}")
    if not r["ok"]:
        raise typer.Exit(2)


# ---- judge --------------------------------------------------------------------------------


@judge_app.command("run")
def judge_run(queries: str = typer.Option(..., "--queries"), pipelines: str = typer.Option(..., "--pipelines"),
              judge: str = typer.Option("llm", "--judge", help="llm, llm:<model> or heuristic"),
              into: str = typer.Option(..., "--into", help="judgment dataset name"),
              depth: int = typer.Option(10, "--depth", help="grade the top N of each pipeline"),
              rubric: Optional[str] = typer.Option(None, "--rubric", help="a rubric name in captain/rubrics/, or a file")):
    """Grade the pooled top results of each pipeline. Reuses grades already in --into."""
    def prog(f):
        if not S.json:
            typer.echo(f"\r  judging {f:.0%}", nl=False, err=True)
    m = _do(ops.judge_run, ctx(), queries, pipelines.split(","), judge, into, depth, progress=prog, dry_run=S.dry_run,
            rubric=rubric)
    p = m.provenance.params
    _out(m, f"\n{'would save' if S.dry_run else 'saved'} {m.name}@v{m.version}: {m.row_count} grades "
            f"({p.get('new_pairs', 0)} new, {p.get('reused_pairs', 0)} reused) by {judge}")


@judge_app.command("from-logs")
def judge_from_logs(logs: str, into: str = typer.Option(..., "--into"), min_impressions: int = typer.Option(3, "--min-impressions")):
    """Grades from historic clicks, corrected for position bias (clicks over expected clicks)."""
    from .. import historic as H

    m = _do(H.judgments_from_logs, ctx(), logs, into, min_impressions, dry_run=S.dry_run)
    ev = "SIMULATED (the logs are synthetic)" if (m.tags or {}).get("synthetic") else "LOGGED_CLICKS"
    _out(m, f"saved {m.name}@v{m.version}: {m.row_count} grades from logged clicks, evidence {ev}")


@judge_app.command("import")
def judge_import(path: str, name: str = typer.Option(..., "--name")):
    """Import human labels (columns query_id, product_id, grade 0-3)."""
    m = _do(ops.judge_import, ctx(), path, name, dry_run=S.dry_run)
    _out(m, f"saved {m.name}@v{m.version}: {m.row_count} human grades")


@judge_app.command("calibrate")
def judge_calibrate(llm: str = typer.Option(..., "--llm"), human: str = typer.Option(..., "--human")):
    """Agreement between an LLM judgment set and human labels on the same pairs."""
    r = _do(ops.judge_calibrate, ctx(), llm, human)
    emit(r, f"{r['n']} shared pairs. kappa {r['kappa']}, exact agreement {r.get('exact_agreement')}, "
            f"mean grade bias {r['mean_grade_bias']:+}" if r["n"] else "no shared pairs")


# ---- pipelines ----------------------------------------------------------------------------


@pipeline_app.command("new")
def pipeline_new(name: str, engine: str = typer.Option("local-bm25", "--engine"),
                 catalog: Optional[str] = typer.Option(None, "--catalog"), out: Optional[str] = typer.Option(None, "-o")):
    """Write a draft spec file to edit, then publish it."""
    text = _do(ops.pipeline_template, name, engine, catalog)
    path = out or f"{name.replace('/', '_')}.pipeline.yaml"
    if os.path.exists(path):
        fail(f"{path} exists")
    if not S.dry_run:
        open(path, "w").write(text)
    emit({"draft": path}, f"wrote {path}. Edit it, then `cst pipeline publish {path}`.")


@pipeline_app.command("publish")
def pipeline_publish(path: str):
    """Publish a draft spec as the next write-once version."""
    draft = yaml.safe_load(open(path))
    m = _do(ops.pipeline_publish, ctx(), draft, dry_run=S.dry_run)
    fp = m.index_fingerprint
    _out(m, f"{'would publish' if S.dry_run else 'published'} {m.name}@v{m.version} ({m.engine})"
            + (f", index fingerprint {fp.doc_count} docs" if fp else ", no index fingerprint (engine cannot report one)"))


@pipeline_app.command("list")
def pipeline_list():
    c = ctx()
    rows = []
    for n in c.registry.names("pipeline"):
        m = c.registry.load("pipeline", n)
        rows.append({"name": n, "latest": m.version, "engine": m.engine, "repro": m.repro_status.value, "by": m.created_by})
    emit(rows, "\n".join(f"{r['name']}@v{r['latest']}  {r['engine']}  {r['repro']}  {r['by']}" for r in rows) or "no pipelines")


@pipeline_app.command("show")
def pipeline_show(ref: str):
    m, _ = _do(ctx().registry.resolve, "pipeline", ref)
    _out(m, yaml.safe_dump(m.model_dump(mode="json"), sort_keys=False))


@pipeline_app.command("diff")
def pipeline_diff(a: str, b: str):
    d = _do(ops.pipeline_diff, ctx(), a, b)
    emit(d, "\n".join(d) or "no differences")


@pipeline_app.command("link")
def pipeline_link(name: str, registry: str = typer.Option(..., "--registry", help="s3://<bucket>/<arch>/<brand>/<ver>"),
                  url: str = typer.Option("https://set-me/v1/rank", "--url", help="the serving endpoint for this model")):
    """Publish a pipeline that points at a model already in the model registry."""
    m = _do(ops.pipeline_link, ctx(), name, registry, "captain", {"url": url, "api_key_secret": "CAPTAIN_API_KEY"}, dry_run=S.dry_run)
    _out(m, f"published {m.name}@v{m.version} linked to {registry} (dvc alias {m.dvc_alias}, repro {m.repro_status.value})")


# ---- populations --------------------------------------------------------------------------


@pop_app.command("create")
def pop_create(name: str, simulator: str = typer.Option("cascade-v1", "--simulator"), size: int = typer.Option(10000, "--size"),
               seed: int = typer.Option(1, "--seed"),
               click_judgments: Optional[str] = typer.Option(None, "--click-judgments",
                                                             help="let the click model read these grades (makes runs scored on them not independent)"),
               from_logs: Optional[str] = typer.Option(None, "--from-logs", help="calibrate looking-by-rank from historic logs")):
    m = _do(ops.population_create, ctx(), name, simulator, size, seed, click_judgments, from_logs=from_logs, dry_run=S.dry_run)
    _out(m, f"saved {m.name}@v{m.version}: {m.size} shoppers, {m.simulator} {m.simulator_version}")


# ---- backtests ----------------------------------------------------------------------------


@backtest_app.command("plan")
def bt_plan(name: str, arms: str = typer.Option(..., "--arms", help="baseline first: a@v1,b@v2"),
            queries: Optional[str] = typer.Option(None, "--queries", help="name@v2:holdout"),
            judgments: Optional[str] = typer.Option(None, "--judgments"), population: Optional[str] = typer.Option(None, "--population"),
            hypothesis: str = typer.Option(..., "--hypothesis"), primary: str = typer.Option("ndcg@10", "--primary"),
            mde: float = typer.Option(0.003, "--mde"), mode: str = typer.Option("simulated", "--mode", help="simulated or historic"),
            logs: Optional[str] = typer.Option(None, "--logs", help="historic mode: events dataset to replay"),
            compute: Optional[str] = typer.Option(None, "--compute", help="compute target name (default: local)"),
            surface: Optional[list[str]] = typer.Option(None, "--surface",
                help="another surface to score: name=related,queries=q@v1:holdout,judgments=j@v1 (repeatable)"),
            guardrail: Optional[list[str]] = typer.Option(None, "--guardrail",
                help="SURFACE METRIC min|max_drop VALUE, for example 'related ndcg@10 max_drop 0.01' (repeatable)")):
    """Pre-register a backtest. It is written before anything runs. Historic mode builds queries, grades and
    shoppers from the logs when you don't give them. Extra surfaces are scored with their own queries and
    judgments, and their guardrails gate a live promote."""
    sfs = _do(_parse_surfaces, surface or [], guardrail or [])
    m = _do(ops.backtest_plan, ctx(), name, arms.split(","), queries, judgments, population, hypothesis, primary, mde,
            dry_run=S.dry_run, mode=mode, logs=logs, compute=compute, surfaces=sfs)
    _out(m, f"planned {m.name}@v{m.version}: {len(m.arms)} arms, primary {primary}, MDE {mde}")


def _parse_surfaces(surfaces: list[str], guardrails: list[str]) -> list[dict]:
    out = {}
    for raw in surfaces:
        d = {}
        for part in raw.split(","):
            if "=" not in part:
                raise ValueError(f"--surface takes name=...,queries=...,judgments=... (got {raw!r})")
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
        if set(d) - {"name", "queries", "judgments"} or not {"name", "queries", "judgments"} <= set(d):
            raise ValueError(f"--surface needs exactly name, queries and judgments (got {raw!r})")
        out[d["name"]] = {**d, "guardrails": []}
    for raw in guardrails:
        parts = raw.split()
        if len(parts) != 4 or parts[2] not in ("min", "max_drop"):
            raise ValueError(f"--guardrail is 'SURFACE METRIC min|max_drop VALUE' (got {raw!r})")
        sname, metric, rule, val = parts
        if sname not in out:
            raise ValueError(f"--guardrail names surface {sname}, which no --surface defines")
        try:
            out[sname]["guardrails"].append({"metric": metric, rule: float(val)})
        except ValueError:
            raise ValueError(f"guardrail value {val!r} is not a number") from None
    return list(out.values())


@backtest_app.command("run")
def bt_run(name: str, compute: Optional[str] = typer.Option(None, "--compute")):
    """Run the latest plan, here or on a compute target. Stores rankings and shopper logs with the run."""
    def prog(f):
        if not S.json:
            typer.echo(f"\r  running {f:.0%}", nl=False, err=True)
    c = ctx()
    from .. import live as LV

    import os

    from .. import compute as C
    from .. import settings as ST

    bt = c.registry.load("backtest", name)
    lv, live_id = None, None
    if os.environ.get("CST_LIVE_ID") and os.environ.get("CST_JOB_ID"):
        lv = LV.attach_store(c, os.environ["CST_LIVE_ID"])  # in a job's container: stream through the bucket
    elif not S.dry_run:
        where = compute or bt.compute or ST.default_compute(c, "backtest") or "local"
        if os.environ.get("CST_JOB_ID") or C.get(c, where)["kind"] == "local":
            lv = LV.start("backtest", name, of=name, arms=list(bt.arms), source="cli")
        else:
            live_id = LV.start_remote(c, "backtest", name, of=name, arms=list(bt.arms), where=where)
    try:
        r = _do(ops.backtest_launch, c, name, compute, progress=prog, dry_run=S.dry_run, live=lv, live_id=live_id)
    except BaseException as e:
        if lv:
            lv.end("failed", error=str(e) or "stopped")
        if live_id:
            LV.note_remote(c, live_id, status="failed", error=str(e) or "stopped")
        raise
    if lv:
        lv.end("done" if "run" in r else "failed", ref=r.get("run"), summary=f"Finished {name}")
    if live_id and r.get("job"):
        LV.note_remote(c, live_id, job=r["job"])
    if "run" in r:
        run = c.registry.load("backtest-run", name)
        return _out(run, "\n" + ops.backtest_report(run, c.registry.load("backtest", name)))
    emit(r, yaml.safe_dump(r, sort_keys=False))


@backtest_app.command("report")
def bt_report(name: str, version: Optional[int] = typer.Option(None, "--version", "-v")):
    c = ctx()
    r = _do(c.registry.load, "backtest-run", name, version)
    _out(r, ops.backtest_report(r, c.registry.load("backtest", name)))


@backtest_app.command("verify")
def bt_verify(name: str, version: Optional[int] = typer.Option(None, "--version", "-v")):
    """Rerun the simulator and report how many shoppers' logs differ from the stored ones."""
    r = _do(ops.backtest_verify, ctx(), name, version)
    emit(r, "\n".join(f"{a['arm']}: {a['differ']} of {a['shoppers']} shoppers differ ({a['share_differ']:.2%})" for a in r.get("arms", []))
         or r.get("detail", ""))


@backtest_app.command("list")
def bt_list():
    c = ctx()
    rows = [{"name": n, "latest": c.registry.load("backtest", n).version, "runs": len(c.registry.versions("backtest-run", n))}
            for n in c.registry.names("backtest")]
    emit(rows, "\n".join(f"{r['name']}@v{r['latest']}  {r['runs']} runs" for r in rows) or "no backtests")


# ---- deploys ------------------------------------------------------------------------------


@env_app.command("add")
def env_add(name: str, tier: str = typer.Option("demo", "--tier", help="demo (no gates) or live (gated)"),
            target: str = typer.Option("record", "--target", help="record, webhook or captain"),
            url: Optional[str] = typer.Option(None, "--url"), token_secret: Optional[str] = typer.Option(None, "--token-secret"),
            p99_ms: float = typer.Option(50.0, "--p99-ms"),
            compute: Optional[str] = typer.Option(None, "--compute", help="compute target that serves this environment")):
    t = {"type": target}
    if url:
        t["url"] = url
    if token_secret:
        t["token_secret"] = token_secret
    gates = {"p99_ms": p99_ms, "recall_floor": 0.98, "min_evidence": "HUMAN_GRADED"} if tier == "live" else {}
    if S.dry_run:
        return emit({"would_create": name, "tier": tier, "target": t})
    e = _do(ops.env_create, ctx(), name, tier, t, gates, compute)
    _out(e, f"created {name} ({tier}{', gated' if tier == 'live' else ', no gates'}), target {target}")


@env_app.command("list")
def env_list():
    rows = _do(ops.env_list, ctx())
    emit(rows, "\n".join(f"{r['name']}  {r['tier']}  current: {r['current'] or '-'}" for r in rows) or "no environments")


@deploy_app.command("promote")
def deploy_promote(pipeline: str, env: str = typer.Option(..., "--env"),
                   override_reason: Optional[str] = typer.Option(None, "--override-reason"),
                   confirm: bool = typer.Option(False, "--confirm", help="required for live environments")):
    d = _do(ops.promote, ctx(), pipeline, env, override_reason, confirm, dry_run=S.dry_run)
    g = d.gate_results.get("gates", [])
    lines = [f"{'would promote' if S.dry_run else 'promoted'} {d.pipeline} to {env}"]
    lines += [f"  [{'ok ' if r['ok'] else 'OVERRIDDEN'}] {r['gate']}: {r['detail']}" for r in g]
    if d.override_reason:
        lines.append(f"  override reason recorded: {d.override_reason}")
    _out(d, "\n".join(lines))


@deploy_app.command("rollback")
def deploy_rollback(env: str = typer.Option(..., "--env"), confirm: bool = typer.Option(False, "--confirm")):
    d = _do(ops.rollback, ctx(), env, confirm, dry_run=S.dry_run)
    _out(d, f"rolled {env} back to {d.pipeline}")


@deploy_app.command("status")
def deploy_status(env: str = typer.Option(..., "--env")):
    cur = ops.current(ctx(), env)
    emit(cur or {}, f"{env}: {cur['pipeline'] if cur else 'nothing deployed'}")


@deploy_app.command("history")
def deploy_history(env: str = typer.Option(..., "--env")):
    h = ops.history(ctx(), env)
    emit([d.model_dump(mode="json") for d in h],
         "\n".join(f"{(d.occurred_at or d.created_at):%Y-%m-%d %H:%M}{'*' if d.backfilled else ' '} {d.action:8} {d.pipeline}  {d.created_by}"
                   + (f"  override: {d.override_reason}" if d.override_reason else "") for d in h) or "no deployments")


# ---- notebooks ----------------------------------------------------------------------------


@notebook_app.command("list")
def nb_list():
    from .. import notebook as NB

    rows = NB.listing(ctx())
    emit(rows, "\n".join(f"{r['name']}  v{r['latest'] or '-'}{'  (draft)' if r['has_draft'] else ''}" for r in rows) or "no notebooks")


@notebook_app.command("new")
def nb_new(name: str):
    from .. import notebook as NB

    c = ctx()
    if NB.listing(c) and any(r["name"] == name for r in NB.listing(c)):
        fail(f"notebook {name} exists")
    nb = NB.new_notebook()
    nb["cells"] = [{"cell_type": "code", "metadata": {"captain": {"kind": "sql", "conn": None, "var": "df"}}, "outputs": [],
                    "execution_count": None, "source": "select 1 as ok"}]
    if not S.dry_run:
        NB.save_draft(c, name, nb)
    emit({"name": name}, f"created draft {name}. Open it with `cst notebook open {name}`.")


@notebook_app.command("run")
def nb_run(name: str, param: list[str] = typer.Option([], "-p", help="key=value, set before the first cell"),
           save_rev: bool = typer.Option(False, "--save-rev", help="save the executed notebook as a new revision"),
           upto: Optional[int] = typer.Option(None, "--upto", help="run cells 1 to N only"),
           keep: bool = typer.Option(False, "--keep", help="keep the outputs in the draft, so the app shows them")):
    """Run the cells headless, papermill-style: all of them, or 1 to N with --upto."""
    from .. import notebook as NB

    c = ctx()
    _, nb, _ = _do(NB.load, c, name)
    params = {}
    for p in param:
        k, _, v = p.partition("=")
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    if upto is not None:
        if not 1 <= upto <= len(nb["cells"]):
            fail(f"there's no cell {upto}: the notebook has {len(nb['cells'])} cells")
        rest, nb["cells"] = nb["cells"][upto:], nb["cells"][:upto]
    else:
        rest = []
    nb, errors = NB.run_all(c, nb, params, progress=None if S.json else (lambda s: typer.echo(f"  {s}", err=True)))
    nb["cells"] += rest
    if keep and not S.dry_run:
        NB.save_draft(c, name, nb)
    out = {"errors": errors}
    if save_rev and not S.dry_run:
        out["version"] = NB.save_revision(c, name, nb, "headless run").version
    emit(out, ("ok" if not errors else "\n".join(errors)) + (f"\nsaved {name}@v{out['version']}" if "version" in out else ""))
    if errors:
        raise typer.Exit(2)


def _nb_text(cell: dict, outputs: bool) -> str:
    meta = (cell.get("metadata") or {}).get("captain") or {}
    kind = "markdown" if cell.get("cell_type") == "markdown" else meta.get("kind") or "python"
    head = kind + (f" on {meta['conn']}" if kind == "sql" and meta.get("conn") else "") + (
        f" into {meta['var']}" if kind == "sql" and meta.get("var") else "")
    lines = [head, *("    " + ln for ln in str(cell.get("source") or "").splitlines())]
    if outputs:
        for o in cell.get("outputs") or []:
            txt = o.get("text") or (o.get("data") or {}).get("text/plain") or o.get("evalue") or ""
            txt = "".join(txt) if isinstance(txt, list) else str(txt)
            lines += ["    > " + ln for ln in txt.splitlines()[:20]]
    return "\n".join(lines)


def _nb_edit(name: str, change, message: str):
    """Load a notebook (draft first), change its cells, and save it back as the draft the app opens."""
    from .. import notebook as NB

    c = ctx()
    _, nb, _ = _do(NB.load, c, name)
    out = change(nb)
    NB.normalize(nb)
    if not S.dry_run:
        NB.save_draft(c, name, nb)
    emit({"name": name, "cells": len(nb["cells"]), **(out or {})}, message)


def _nb_index(nb: dict, n: int) -> int:
    if not 1 <= n <= len(nb["cells"]):
        fail(f"there's no cell {n}: the notebook has {len(nb['cells'])} cells, numbered from 1")
    return n - 1


def _nb_source(source: Optional[str], file: Optional[str]) -> str:
    if file:
        return sys.stdin.read() if file == "-" else open(os.path.expanduser(file)).read()
    if source is None:
        fail("give the cell's text with --source, or a file with -f (- reads standard input)")
    return source


@notebook_app.command("show")
def nb_show(name: str, outputs: bool = typer.Option(False, "--outputs", help="include each cell's last output")):
    """Print a notebook's cells, numbered, the way the cell commands count them."""
    from .. import notebook as NB

    version, nb, draft = _do(NB.load, ctx(), name)
    cells = nb["cells"]
    emit({"name": name, "version": version, "draft": draft, "cells": cells},
         f"{name} ({'draft' if draft else f'v{version}'}), {len(cells)} cells\n"
         + "\n".join(f"[{i + 1}] {_nb_text(cl, outputs)}" for i, cl in enumerate(cells)))


@notebook_cell_app.command("add")
def nb_cell_add(name: str, kind: str = typer.Option("python", "--kind", help="python, sql or markdown"),
                source: Optional[str] = typer.Option(None, "--source"), file: Optional[str] = typer.Option(None, "-f"),
                conn: Optional[str] = typer.Option(None, "--conn", help="sql: the connection to run on"),
                var: Optional[str] = typer.Option(None, "--var", help="sql: the DataFrame name for the result"),
                at: Optional[int] = typer.Option(None, "--at", help="insert as cell N (default: the end)")):
    """Add a cell. SQL cells run on a connection and leave their result as a DataFrame."""
    if kind not in ("python", "sql", "markdown"):
        fail("a cell is python, sql or markdown")
    text = _nb_source(source, file)
    if kind == "markdown":
        cell = {"cell_type": "markdown", "metadata": {}, "source": text}
    else:
        meta = {"kind": kind, **({"conn": conn, "var": var or "df"} if kind == "sql" else {})}
        cell = {"cell_type": "code", "metadata": {"captain": meta}, "outputs": [], "execution_count": None, "source": text}

    def ch(nb):
        i = len(nb["cells"]) if at is None else max(0, min(len(nb["cells"]), at - 1))
        nb["cells"].insert(i, cell)
        return {"cell": i + 1}

    _nb_edit(name, ch, f"added a {kind} cell to {name}")


@notebook_cell_app.command("edit")
def nb_cell_edit(name: str, n: int, source: Optional[str] = typer.Option(None, "--source"),
                 file: Optional[str] = typer.Option(None, "-f"), conn: Optional[str] = typer.Option(None, "--conn"),
                 var: Optional[str] = typer.Option(None, "--var")):
    """Replace cell N's text (and, for SQL, its connection or result name). Its old output is cleared."""
    text = _nb_source(source, file) if (source is not None or file) else None

    def ch(nb):
        cell = nb["cells"][_nb_index(nb, n)]
        if text is not None:
            cell["source"] = text
        meta = cell.setdefault("metadata", {}).setdefault("captain", {})
        if conn is not None:
            meta["conn"] = conn
        if var is not None:
            meta["var"] = var
        if cell.get("cell_type") == "code":
            cell["outputs"], cell["execution_count"] = [], None
        return {"cell": n}

    _nb_edit(name, ch, f"edited cell {n} of {name}")


@notebook_cell_app.command("rm")
def nb_cell_rm(name: str, n: int):
    """Remove cell N."""
    def ch(nb):
        nb["cells"].pop(_nb_index(nb, n))
        return {"removed": n}

    _nb_edit(name, ch, f"removed cell {n} from {name}")


@notebook_app.command("import")
def nb_import(path: str, name: Optional[str] = typer.Option(None, "--name", help="defaults to the file name")):
    """Import a Jupyter .ipynb file as a new notebook revision."""
    from .. import notebook as NB

    m = _do(NB.import_file, ctx(), path, name)
    _out(m, f"imported {path} as {m.name}@v{m.version} ({m.description})")


@notebook_app.command("export")
def nb_export(name: str, out: Optional[str] = typer.Option(None, "-o"),
              fmt: str = typer.Option("ipynb", "--format", help="ipynb, jupytext, databricks, marimo, quarto, rmarkdown, myst, zeppelin")):
    """Write a notebook in another notebook format."""
    from .. import notebook as NB
    from ..notebook import formats as F

    _, nb, _ = _do(NB.load, ctx(), name)
    ext, text, _ = _do(F.from_ipynb, nb, fmt)
    path = out or f"{name.replace('/', '_')}{ext}"
    with open(path, "w") as fh:
        fh.write(text)
    emit({"path": path}, f"wrote {path}")


@notebook_app.command("open")
def nb_open(name: str):
    """Open a notebook in the desktop app."""
    _open_app(f"captain-search://notebook/{name}")


# ---- skills, agent ------------------------------------------------------------------------


@skill_app.command("list")
def skill_list():
    from .. import skills as SK

    rows = SK.listing(ctx())
    emit(rows, "\n".join(f"{r['name']}  {r['version']}  {r['description']}" for r in rows))


@skill_app.command("show")
def skill_show(name: str):
    from .. import skills as SK

    t = _do(SK.read, ctx(), name)
    emit({"name": name, "text": t}, t)


@skill_app.command("add")
def skill_add(folder: str):
    """Publish a folder with a SKILL.md as a workspace skill."""
    from .. import skills as SK

    m = _do(SK.add, ctx(), folder, dry_run=S.dry_run)
    _out(m, f"published skill {m.name}@v{m.version}")


# ---- daemon, app, demo --------------------------------------------------------------------


@app.command()
def serve(host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(0, "--port"),
          token: Optional[str] = typer.Option(None, "--token"),
          ui: Optional[str] = typer.Option(None, "--ui", help="folder of the built app screens to serve at / (default: app/dist or $CST_UI_DIR)")):
    """Run the daemon the desktop app talks to. Prints {port, token, pid, ui} on the first line."""
    from .. import proposals
    from ..server import serve as run

    proposals.DETECT_AGENT = False  # the app's requests come from a person, even if the app started in an agent's terminal
    run(host, port, token, ui)


APP_PATHS = ["/Applications/Captain Search Tools.app", os.path.expanduser("~/Applications/Captain Search Tools.app")]


def _open_app(url: str | None = None):
    path = next((p for p in APP_PATHS if os.path.exists(p)), None)
    if path is None:
        fail("Captain Search Tools.app is not installed. Build it with `npm run tauri build` in app/, or install the DMG.")
    subprocess.run(["open", path] + (["--args", url] if url else []), check=False)
    emit({"opened": path}, f"opened {path}")


@demo_app.command("import")
def demo_import():
    """Register the store demos in your private demos file (catalogs, logs, models, local deploys, readouts) by reference."""
    from .. import brands

    r = _do(brands.import_demos, ctx(), progress=(lambda s: None) if S.json else (lambda s: typer.echo(f"  {s}", err=True)))
    emit(r, "done. Imported results keep their evidence labels.")


@demo_app.command("seed")
def demo_seed(legacy: bool = typer.Option(False, "--legacy", help="Use the older simulated experiment demo in the current workspace")):
    """Load the isolated Sample North Pole workspace. Safe to rerun; no clicks or performance are invented."""
    if legacy:
        from .. import demo
        if S.dry_run:
            emit({"dry_run": True, "legacy": True}, "Would load the legacy simulated demo.")
            return
        r = _do(demo.seed, ctx(), progress=(lambda s: None) if S.json else (lambda s: typer.echo(f"  {s}", err=True)))
        emit(r, "Legacy demo ready. Every number is SIMULATED.")
        return
    from .. import sample_data
    r = _do(sample_data.seed, dry_run=S.dry_run)
    emit(r, "Sample North Pole ready. Select sample-north-pole in the workspace switcher. Remove with cst demo remove-sample.")


@demo_app.command("remove-sample")
def demo_remove_sample():
    """Remove only the unchanged, owned Sample North Pole workspace. Refuses added or edited files."""
    from .. import sample_data
    r = _do(sample_data.remove, dry_run=S.dry_run)
    emit(r, "Sample removal checked (dry run)." if S.dry_run else "Sample North Pole removed, or already absent. Other workspaces are unchanged.")


# ---- v2: cloud, compute, models, lineage, serving, coding agent ---------------------------

cloud_app = typer.Typer(no_args_is_help=True, help="Cloud profiles (credentials in the OS keychain)")
compute_app = typer.Typer(no_args_is_help=True, help="Compute targets for backtests and serving")
models_app = typer.Typer(no_args_is_help=True, help="The model registry (read-only import)")
app.add_typer(cloud_app, name="cloud")
app.add_typer(compute_app, name="compute")
app.add_typer(models_app, name="models")


@cloud_app.command("add")
def cloud_add(name: str, provider: str = typer.Option(..., "--provider", help="aws, gcp or azure"),
              auth: str = typer.Option(..., "--auth", help="aws: profile|keys|role|default, gcp: service-account|adc, azure: service-principal|connection-string|default"),
              region: Optional[str] = typer.Option(None, "--region"),
              field: list[str] = typer.Option([], "--field", "-f", help="name=value for non-secret fields"),
              secret: list[str] = typer.Option([], "--secret", "-s", help="name of a secret field; you will be prompted")):
    from .. import cloud

    fields = dict(f.split("=", 1) for f in field)
    secrets = {n: typer.prompt(n, hide_input=True) for n in secret}
    p = _do(cloud.add_profile, name, provider, auth, region, fields, secrets)
    emit({k: v for k, v in p.items() if k != "secret_fields"}, f"saved profile {name} ({provider}, {auth}). Secrets are in the keychain.")


@cloud_app.command("list")
def cloud_list():
    from .. import cloud

    rows = cloud.profiles()
    emit(rows, "\n".join(f"{p['name']}  {p['provider']}  {p['auth']}  {p.get('region') or ''}" for p in rows) or "no profiles")


@cloud_app.command("test")
def cloud_test(name: str):
    from .. import cloud

    r = cloud.test_profile(name)
    emit(r, f"ok: {r['identity']}" if r["ok"] else f"failed: {r['error']}")
    if not r["ok"]:
        raise typer.Exit(2)


@cloud_app.command("rm")
def cloud_rm(name: str):
    from .. import cloud

    _do(cloud.delete_profile, name)
    emit({"removed": name}, f"removed {name}")


@compute_app.command("kinds")
def compute_kinds():
    from .. import compute as C

    ks = C.kinds()
    emit(ks, "\n".join(f"{k['kind']:22} {k['label']}  ({', '.join(k['purposes'])})  "
                       f"{'last ran for real ' + k['live_tested'] if k['live_tested'] else 'not tested on a real account'}"
                       for k in ks))


@compute_app.command("add")
def compute_add(name: str, kind: str = typer.Option(..., "--kind"), profile: Optional[str] = typer.Option(None, "--profile"),
                region: Optional[str] = typer.Option(None, "--region"),
                config: list[str] = typer.Option([], "--set", help="field=value, see `cst compute kinds`")):
    from .. import compute as C

    t = _do(C.add, ctx(), name, kind, profile, region, dict(c.split("=", 1) for c in config))
    emit(t, f"added compute {name} ({kind})")


@compute_app.command("list")
def compute_list():
    from .. import compute as C

    rows = C.listing(ctx())
    emit(rows, "\n".join(f"{t['name']:16} {t['kind']:22} {t.get('profile') or '-':12} {t.get('region') or ''}" for t in rows))


@compute_app.command("test")
def compute_test(name: str):
    from .. import compute as C

    r = C.test(ctx(), name)
    emit(r, ("ok: " if r["ok"] else "failed: ") + r["detail"])


@models_app.command("import")
def models_import(uri: Optional[str] = typer.Option(None, "--uri", help="s3://<bucket>. Default: $CST_MODEL_REGISTRY or your demos file"), profile: Optional[str] = typer.Option(None, "--profile")):
    """Copy the registry's manifests (not weights) into the workspace as a snapshot."""
    from .. import models as MD

    s = _do(MD.import_registry, ctx(), uri, profile)
    emit({"snapshot": f"{s.name}@v{s.version}", "models": len(s.models)}, f"imported {s.description} as {s.name}@v{s.version}")


@models_app.command("list")
def models_list():
    from .. import models as MD

    rows = MD.listing(ctx())
    emit(rows, "\n".join(f"{m['id']:20} {m['architecture']:28} {m['status_kind']:12} {m['repro_status']:20} parent: {', '.join(m['parent_ids']) or '-'}"
                          for m in rows) or "no registry imported. Run `cst models import`.")


@models_app.command("show")
def models_show(model_id: str):
    from .. import models as MD

    m = _do(MD.detail, ctx(), model_id)
    emit(m, yaml.safe_dump({k: v for k, v in m.items() if k not in ("files", "manifest")}, sort_keys=False)
         + f"files: {len(m['files'])} ({m['bytes'] / 1e6:.1f} MB)")


@models_app.command("pull")
def models_pull(model_id: str = typer.Argument(..., help="brand@version, or a brand for its current model"),
                out: Optional[str] = typer.Option(None, "--out", "-o", help="Folder to download into. Default: ./<brand>-<version>"),
                only: list[str] = typer.Option([], "--only", help="weights, code, env, results or inputs. Repeat for more. Default: all but inputs"),
                profile: Optional[str] = typer.Option(None, "--profile")):
    """Download a model version's weights and files, checked against its SHA256SUMS."""
    from .. import models as MD

    def show(f):
        if not S.json:
            typer.echo(f"\r{f * 100:5.1f}%", nl=False, err=True)

    r = _do(MD.pull, ctx(), model_id, out, only or None, profile, show)
    if not S.json:
        typer.echo("", err=True)
    size = f", {r['bytes'] / 1e6:.1f} MB" if r["bytes"] is not None else ""
    note = f"\n{len(r['unchecked'])} file(s) aren't listed in SHA256SUMS, so they weren't checked." if r["unchecked"] else ""
    emit(r, f"pulled {r['model']} ({r['files']} files{size}) into {r['to']}. "
            f"{r['downloaded']} downloaded, {r['already_there']} already there, {r['checked']} checked against SHA256SUMS." + note)


lineage_app = typer.Typer(help="Print the lineage graph as edges. `cst lineage reindex` backfills its index.")
app.add_typer(lineage_app, name="lineage")


@lineage_app.callback(invoke_without_command=True)
def lineage(typer_ctx: typer.Context, view: str = typer.Option("artifacts", "--view", help="artifacts or models")):
    """Print the lineage graph as edges."""
    if typer_ctx.invoked_subcommand:
        return
    from .. import models as MD

    g = MD.lineage(ctx()) if view == "models" else ops.lineage(ctx())
    emit(g, "\n".join(f"{e['src']}  --{e['relation']}-->  {e['dst']}" for e in g["edges"]) or "no lineage yet")


@lineage_app.command("reindex")
def lineage_reindex():
    """Write lineage records for every version published before the index existed. Safe to run again."""
    from .. import lineage_index as LI

    r = LI.reindex(ctx(), dry_run=S.dry_run)
    verb = "Would add" if S.dry_run else "Added"
    emit(r, f"{verb} {r['added']} lineage records ({r['existing']} already there)."
            + ("" if S.dry_run else " The index now covers the whole workspace."))


@pipeline_app.command("serve")
def pipeline_serve(ref: str, host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(8080, "--port")):
    """Serve one pipeline version over HTTP: POST /search {query, k} returns {ids, ms}. What cloud targets run."""
    from ..serving import serve_pipeline

    serve_pipeline(ctx(), ref, host, port)


@app.command()
def code(agent_id: str = typer.Argument("claude", help="claude or codex")):
    """Start a coding agent in this workspace's agent folder (what the app's terminal panel runs)."""
    from .. import codeagent

    info = codeagent.prepare(ctx())
    a = next((x for x in info["agents"] if x["id"] == agent_id), None)
    if a is None or not a["available"]:
        fail(f"{agent_id} is not installed")
    env = {**os.environ, **info["env"]}
    os.chdir(info["cwd"])
    os.execvpe(a["command"][0], a["command"], env)


wf_app = typer.Typer(no_args_is_help=True, help="Workflows: saved chains of cst commands")
app.add_typer(wf_app, name="workflow")


@wf_app.command("templates")
def wf_templates():
    from .. import workflows as WF

    emit([{"id": k, **v} for k, v in WF.TEMPLATES.items()], "\n".join(f"{k:22} {v['description']}" for k, v in WF.TEMPLATES.items()))


@wf_app.command("save")
def wf_save(path: Optional[str] = typer.Argument(None, help="workflow YAML"), template: Optional[str] = typer.Option(None, "--template"),
            name: Optional[str] = typer.Option(None, "--name")):
    """Save a workflow from YAML (name, steps, params, schedule) or from a template."""
    from .. import workflows as WF

    if template:
        if template not in WF.TEMPLATES:
            fail(f"unknown template {template}")
        d = dict(WF.TEMPLATES[template])
        d["name"] = name or template
    elif path:
        d = yaml.safe_load(open(path))
        if name:
            d["name"] = name
    else:
        fail("give a YAML file or --template")
    w = _do(WF.save, ctx(), d["name"], d.get("steps", []), d.get("params"), d.get("schedule"), d.get("description", ""))
    emit(w.model_dump(mode="json"), f"saved workflow {w.name}@v{w.version} ({len(w.steps)} steps{', schedule ' + w.schedule if w.schedule else ''})")


@wf_app.command("list")
def wf_list():
    from .. import workflows as WF

    rows = WF.listing(ctx())
    emit(rows, "\n".join(f"{r['name']}@v{r['latest']}  {r['steps']} steps  {r['schedule'] or 'manual'}  last: {r['last_status'] or '-'}" for r in rows) or "no workflows")


@wf_app.command("show")
def wf_show(name: str):
    from .. import workflows as WF

    w = _do(ctx().registry.load, "workflow", name)
    emit(w.model_dump(mode="json"), WF.to_yaml(w))


@wf_app.command("run")
def wf_run(name: str, param: list[str] = typer.Option([], "-p", help="key=value")):
    from .. import workflows as WF

    def show(r):
        if not S.json:
            typer.echo(f"  {r['id']}: {r['status']}  {r.get('command') or r.get('reason') or ''}"
                       + (f"  {r['error']}" if r.get("error") else ""), err=True)
    r = _do(WF.run, ctx(), name, params=dict(p.split("=", 1) for p in param), on_step=show)
    emit(r.model_dump(mode="json"), f"{r.name}@v{r.version}: {r.status}")
    if r.status != "done":
        raise typer.Exit(2)


from .main import data_app  # noqa: E402


@data_app.command("browse")
def data_browse(ref: str, offset: int = typer.Option(0, "--offset"), limit: int = typer.Option(20, "--limit"),
                order: Optional[str] = typer.Option(None, "--order"), desc: bool = typer.Option(False, "--desc"),
                search: Optional[str] = typer.Option(None, "--search"), where: Optional[str] = typer.Option(None, "--where"),
                file: Optional[str] = typer.Option(None, "--file"), count: bool = typer.Option(False, "--count")):
    """Page through any dataset where it lives (local or S3), with sort, search and a filter."""
    from .. import browse
    from .main import table_text
    import pyarrow as pa

    c = ctx()
    m, _ = _do(c.registry.resolve, "dataset", ref)
    r = _do(browse.rows, c, m, file, offset, limit, order, desc, search, where, count)
    t = pa.table({col["name"]: [row[i] for row in r["rows"]] for i, col in enumerate(r["columns"])}) if r["rows"] else pa.table({})
    tail = f"\nrows {offset + 1}-{offset + len(r['rows'])}" + (f" of {r['total']:,}" if "total" in r else "") + f" from {r['file']} in {r['ms']} ms"
    emit(r, (table_text(t, limit) if r["rows"] else "no rows") + tail)


class_app = typer.Typer(no_args_is_help=True, help="Dataset classes your team defines")
data_app.add_typer(class_app, name="class")


@class_app.command("list")
def class_list():
    from .. import classes as CL

    rows = CL.listing(ctx())
    emit(rows, "\n".join(f"{c['name']:18} role={c['role'] or '-':10} {c['datasets']:3} datasets  {c['description']}" for c in rows))


@class_app.command("add")
def class_add(name: str, description: str = typer.Option("", "-m", "--description"),
              role: Optional[str] = typer.Option(None, "--role", help="catalog, queries, judgments, events or logs; omit for a plain label"),
              color: Optional[str] = typer.Option(None, "--color"),
              required: Optional[str] = typer.Option(None, "--required-columns", help="comma separated")):
    from .. import classes as CL

    c = _do(CL.upsert, ctx(), {"name": name, "description": description, "role": role, "color": color, "required_columns": required}, True)
    emit(c, f"added class {name}" + (f" (role {c['role']})" if c["role"] else ""))


@class_app.command("edit")
def class_edit(name: str, description: Optional[str] = typer.Option(None, "-m", "--description"),
               role: Optional[str] = typer.Option(None, "--role"), color: Optional[str] = typer.Option(None, "--color"),
               required: Optional[str] = typer.Option(None, "--required-columns")):
    from .. import classes as CL

    cur = next((c for c in CL.listing(ctx()) if c["name"] == name), None)
    if cur is None:
        fail(f"no class {name}")
    upd = {**cur, **{k: v for k, v in {"description": description, "role": role, "color": color, "required_columns": required}.items() if v is not None}}
    c = _do(CL.upsert, ctx(), upd, False)
    emit(c, f"updated class {name}")


@class_app.command("rm")
def class_rm(name: str):
    from .. import classes as CL

    _do(CL.delete, ctx(), name)
    emit({"removed": name}, f"removed class {name}")


@data_app.command("classify")
def data_classify(dataset: str, cls: list[str] = typer.Option([], "--class", "-c", help="repeat for several; none clears")):
    """Set a dataset's classes. The change is recorded with who and when."""
    from .. import classes as CL

    r = _do(CL.classify, ctx(), dataset, cls)
    emit(r, f"{dataset}: {', '.join(r['classes']) or 'no classes'}" + "".join(f"\nwarning: {w}" for w in r["warnings"]))

from . import value  # noqa: E402,F401  cost, monitor and retrain commands
from . import review  # noqa: E402,F401  proposals, grading, labels, replay and bench commands
from . import appctl  # noqa: E402,F401  cst app: open the desktop app and drive it
from . import invest  # noqa: E402,F401  investigations and signals
from . import ext  # noqa: E402,F401  extensions and the captain/ project folder
from . import library  # noqa: E402,F401  data import, folders, model registries
from . import tables  # noqa: E402,F401  the table browser
from . import rankings  # noqa: E402,F401  bring your own rankings
from . import jobs  # noqa: E402,F401  cst job and cst compute setup
from . import live  # noqa: E402,F401  cst live: runs as they happen
