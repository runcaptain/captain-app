"""The workflows behind the CLI, the daemon and the agent: queries, judging, pipelines, populations,
backtests and deploys. Every function takes a Context and returns plain manifests, so the CLI, the
HTTP API and the agent share one code path."""
from __future__ import annotations

import datetime as dt
import io
import json
import os

import pyarrow as pa
import yaml

from .data.datasets import load_table, snapshot
from .engines import make_engine, timed_search
from .eval import judge as J
from .eval import metrics as M
from .eval import queries as Q
from .eval.stats import compare
from .model.entities import (
    ArmResult, Backtest, BacktestRun, DatasetKind, DatasetVersion, Deployment, Environment, Evidence,
    FileEntry, Guardrail, PipelineVersion, Preregistration, Provenance, ShopperPopulation, Surface, Tier, new_id, utcnow,
)
from .sim import SIMULATORS, get_simulator, logs_from_bytes, logs_to_bytes, per_shopper, personas, run_cascade
from .sim import summarize as sim_summary
from .storage.backend import AlreadyExists, Conflict, NotFound
from .storage.registry import check_name, dump_yaml, sha256
from .workspace import Context, whoami


class GateFailed(Exception):
    def __init__(self, msg, results):
        super().__init__(msg)
        self.results = results



def _custom_metrics(ctx):
    """Metric extensions from installed packages and the project folder, computed next to the built-ins."""
    from . import extensions as X

    return X.current(ctx).custom("metric")


def _run_manifest(ctx, bt, pop) -> dict:
    """The code behind a run: cst's version and commit, Python, the project folder's commit, and the extensions used."""
    from . import extensions as X

    used = [("metric", e.name) for e in X.current(ctx).custom("metric")]
    for a in bt.arms:
        try:
            used.append(("engine", ctx.registry.resolve("pipeline", a)[0].engine))
        except Exception:  # noqa: BLE001
            pass
    if pop is not None:
        used.append(("simulator", pop.simulator))
    return X.run_manifest(ctx, used)

def _base(ctx: Context) -> dict:
    return {"org": ctx.workspace.org, "workspace": ctx.workspace.name, "created_by": whoami()}


def ref(m) -> str:
    return f"{m.name}@v{m.version}"


def rows_of(ctx: Context, dataset_ref: str) -> tuple[DatasetVersion, list[dict], str | None]:
    m, split = ctx.registry.resolve("dataset", dataset_ref)
    rows = load_table(ctx, m).to_pylist()
    if split:
        if not m.splits or split not in m.splits:
            raise ValueError(f"{ref(m)} has no split {split!r}. Seal it first with `cst queries seal`.")
        keep = set(m.splits[split])
        rows = [r for r in rows if r["query_id"] in keep]
    return m, rows, split


def products_of(ctx: Context, catalog_ref: str) -> list[dict]:
    _, rows, _ = rows_of(ctx, catalog_ref)
    if rows and "id" not in rows[0]:
        raise ValueError(f"catalog {catalog_ref} has no `id` column")
    for r in rows:
        r["id"] = str(r["id"])
    return rows


# ---- queries --------------------------------------------------------------------------


def queries_gen(ctx, catalog: str, name: str, classes: list[str], n: int, seed: int, dry_run=False) -> DatasetVersion:
    bad = [c for c in classes if c not in Q.CLASSES]
    if bad:
        raise ValueError(f"unknown classes {bad}. Known: {', '.join(Q.CLASSES)}")
    prods = products_of(ctx, catalog)
    rows = Q.generate(prods, classes, n, seed)
    prov = Provenance(source="generated", params={"catalog": catalog, "classes": classes, "n": n, "seed": seed,
                                                    "generator": "captain_search.eval.queries v1"})
    return snapshot(ctx, check_name(name), pa.Table.from_pylist(rows), kind=DatasetKind.queries, provenance=prov,
                    description=f"{len(rows)} generated queries from {catalog}", dry_run=dry_run)


def queries_seal(ctx, qref: str, holdout: float, dry_run=False) -> DatasetVersion:
    m, rows, _ = rows_of(ctx, qref)
    from . import classes as CL

    if m.dataset_kind != DatasetKind.queries and CL.role_of(ctx, m.name) != "queries":
        raise ValueError(f"{ref(m)} is not a query set: give it a class whose role is queries")
    if m.holdout_seal:
        raise ValueError(f"{ref(m)} is already sealed. A new split is a new version: run seal on the source again.")
    salt = f"{m.name}:{m.version}"
    splits = {"train": [], "holdout": []}
    for r in rows:
        splits[Q.split_of(r["query_id"], holdout, salt)].append(r["query_id"])
    sealed = m.model_copy(update={
        "splits": splits, "holdout_seal": Q.seal(rows, splits["holdout"]), "files": m.files, "parent": m.version,
        "description": (m.description + f" | sealed, holdout {holdout:.0%}").strip(" |"),
        "created_by": whoami(), "created_at": utcnow(), "id": new_id(),
    })
    if dry_run:
        return sealed
    return ctx.registry.publish(sealed)


def check_seal(ctx, qref: str) -> dict:
    m, _ = ctx.registry.resolve("dataset", qref.split(":")[0])
    if not m.holdout_seal:
        return {"gate": "holdout_seal", "ok": False, "detail": f"{ref(m)} is not sealed"}
    rows = load_table(ctx, m).to_pylist()
    ok = Q.seal(rows, m.splits["holdout"]) == m.holdout_seal
    return {"gate": "holdout_seal", "ok": ok, "detail": "intact" if ok else "the holdout queries changed after sealing"}


# ---- judging ---------------------------------------------------------------------------


def _rankings(ctx, pref: str, queries: list[dict], k: int = 50, check_fp: bool = True):
    pm, _ = ctx.registry.resolve("pipeline", pref)
    prods = products_of(ctx, pm.catalog) if pm.catalog else None
    eng = make_engine(pm.engine, pm.spec, prods, workspace=ctx.workspace.name)
    if check_fp and pm.index_fingerprint is not None:
        fp = eng.fingerprint()
        if fp is not None and (fp.doc_count, fp.sample_hash) != (pm.index_fingerprint.doc_count, pm.index_fingerprint.sample_hash):
            raise ValueError(f"the index behind {ref(pm)} changed since it was published "
                             f"({pm.index_fingerprint.doc_count} docs then, {fp.doc_count} now). Publish a new version.")
    out, ms = {}, {}
    for q in queries:
        ids, t = timed_search(eng, q["query"], k)
        out[q["query_id"]], ms[q["query_id"]] = ids, t
    return pm, eng, prods, out, ms


def judge_run(ctx, qref: str, pipelines: list[str], judge: str, into: str, depth: int = 10, progress=None,
              dry_run=False, rubric: str | None = None) -> DatasetVersion:
    _, qs, _ = rows_of(ctx, qref)
    source = "heuristic" if judge == "heuristic" else "llm"
    prev_rows: list[dict] = []
    try:
        prev = ctx.registry.load("dataset", into)
        if prev.judgment_source and prev.judgment_source != source and not (source == "heuristic" and prev.judgment_source == "heuristic"):
            raise ValueError(f"{ref(prev)} holds {prev.judgment_source} grades. Keep judges apart: use another --into name.")
        prev_rows = load_table(ctx, prev).to_pylist()
    except NotFound:
        prev = None
    have = {(str(r["query_id"]), str(r["product_id"])) for r in prev_rows}
    pairs: dict[str, tuple[str, list[str]]] = {}
    products: dict[str, dict] = {}
    for p in pipelines:
        _, _, prods, ranks, _ = _rankings(ctx, p, qs)
        for pr in prods or []:
            products[pr["id"]] = pr
        for q in qs:
            need = [i for i in ranks[q["query_id"]][:depth] if (q["query_id"], i) not in have]
            if need:
                cur = pairs.setdefault(q["query_id"], (q["query"], []))
                cur[1].extend(i for i in need if i not in cur[1])
    rub = None
    if rubric:
        from .rubrics import resolve

        rub = resolve(rubric, ctx.workspace.name)
    before = ((prev.provenance.params or {}).get("rubric") or {}).get("sha") if prev_rows and prev else None
    if prev_rows and before != (rub["sha"] if rub else None) and source == "llm":
        raise ValueError(f"{into} holds grades made under {'another rubric' if before else 'no rubric'}"
                         f"{'' if rub else ', and this run has none'}. Grades from different rubrics aren't comparable: "
                         "use another --into.")
    new_rows, meta = J.judge_pairs(pairs, products, judge, progress=progress, workspace=ctx.workspace.name,
                                   rubric=rub) if pairs else ([], {"calls": 0})
    rows = prev_rows + new_rows
    if not rows:
        rows = [{"query_id": "", "product_id": "", "grade": 0, "source": source, "model": "", "prompt_version": ""}][:0]
    table = pa.Table.from_pylist(rows) if rows else pa.table({"query_id": pa.array([], pa.string()), "product_id": pa.array([], pa.string()), "grade": pa.array([], pa.int64())})
    prov = Provenance(source="generated", params={"queries": qref, "pipelines": pipelines, "judge": judge, "depth": depth,
                                                    "new_pairs": len(new_rows), "reused_pairs": len(prev_rows), **meta})
    m = snapshot(ctx, check_name(into), table, kind=DatasetKind.judgments, provenance=prov, dry_run=True,
                 description=f"{len(rows)} grades ({len(new_rows)} new) by {judge}")
    m = m.model_copy(update={"judgment_source": source,
                             "judge": {"model": meta.get("model", ""), "prompt_version": meta.get("prompt_version", "")},
                             "calibration": prev.calibration if prev else None})
    if dry_run:
        return m
    from .data.datasets import to_parquet

    entry = ctx.registry.put_blob(to_parquet(table), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [entry]}))


def judge_import(ctx, path: str, name: str, dry_run=False) -> DatasetVersion:
    from .data.datasets import read_file

    t = read_file(path)
    need = {"query_id", "product_id", "grade"}
    if not need <= set(t.column_names):
        raise ValueError(f"the file needs columns {sorted(need)}; it has {t.column_names}")
    m = snapshot(ctx, check_name(name), t, kind=DatasetKind.judgments, dry_run=True,
                 provenance=Provenance(source="import", file=os.path.abspath(path)), description="human labels")
    m = m.model_copy(update={"judgment_source": "human"})
    if dry_run:
        return m
    from .data.datasets import to_parquet

    entry = ctx.registry.put_blob(to_parquet(t), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [entry]}))


def judge_calibrate(ctx, llm_ref: str, human_ref: str) -> dict:
    lm, lrows, _ = rows_of(ctx, llm_ref)
    _, hrows, _ = rows_of(ctx, human_ref)
    out = J.calibrate(lrows, hrows)
    out["vs"] = human_ref
    out["llm"] = ref(lm)
    return out


def judgments_map(ctx, jref: str) -> tuple[DatasetVersion, dict[str, dict[str, int]]]:
    m, rows, _ = rows_of(ctx, jref)
    return m, J.to_map(rows)


# ---- pipelines --------------------------------------------------------------------------

TEMPLATE = {
    "local-bm25": {"fields": {"title": 3.0, "brand": 1.5, "category": 1.0, "description": 0.5}, "k1": 1.2, "b": 0.75,
                   "numeric_filter": False, "prefix_match": False, "synonyms": {}, "boosts": []},
    "http": {"url": "https://search.internal/query", "headers": {"Authorization": "Bearer {secret_raw:SEARCH_TOKEN}"}},
    "elastic": {"url": "https://es.internal:9200", "index": "products", "fields": ["title^3", "description"], "api_key_secret": "ES_API_KEY"},
    "typesense": {"url": "https://ts.internal", "collection": "products", "query_by": "title,description", "api_key_secret": "TS_KEY"},
    "algolia": {"app_id": "APPID", "index": "products", "api_key_secret": "ALGOLIA_SEARCH_KEY"},
    "captain": {"url": "https://api.runcaptain.com/v1/rank", "api_key_secret": "CAPTAIN_API_KEY"},
}


def pipeline_template(name: str, engine: str, catalog: str | None) -> str:
    if engine not in TEMPLATE:
        raise ValueError(f"unknown engine {engine}. Choose from {', '.join(TEMPLATE)}")
    return yaml.safe_dump({"name": name, "engine": engine, "catalog": catalog, "description": "",
                           "spec": TEMPLATE[engine], "known_issues": []}, sort_keys=False)


def pipeline_publish(ctx, draft: dict, dry_run=False) -> PipelineVersion:
    name = check_name(draft["name"])
    catalog = draft.get("catalog")
    if catalog:
        cm, _ = ctx.registry.resolve("dataset", catalog)
        catalog = ref(cm)  # pin the exact version
    prods = products_of(ctx, catalog) if catalog else None
    eng = make_engine(draft["engine"], draft.get("spec") or {}, prods, workspace=ctx.workspace.name)
    fp = eng.fingerprint()
    spec_bytes = dump_yaml(draft.get("spec") or {})
    repro = (f"# Reproduce {name}\n\n```sh\ncst pipeline show {name}@v<N>\n"
             f"cst backtest run <backtest>   # rankings come from engine `{draft['engine']}` with spec.yaml\n```\n"
             f"Catalog: `{catalog}`. Index fingerprint at publish: {fp.doc_count if fp else 'n/a'} docs.\n").encode()
    files = [FileEntry(path="spec.yaml", sha256=sha256(spec_bytes), bytes=len(spec_bytes)),
             FileEntry(path="REPRODUCE.md", sha256=sha256(repro), bytes=len(repro))]
    m = PipelineVersion(**_base(ctx), name=name, version=0, engine=draft["engine"], spec=draft.get("spec") or {},
                        catalog=catalog, index_fingerprint=fp, description=draft.get("description") or "",
                        known_issues=draft.get("known_issues") or [], registry_ref=draft.get("registry_ref"),
                        dvc_alias=draft.get("dvc_alias"), training_commit=draft.get("training_commit"), files=files)
    if dry_run:
        return m
    ctx.registry.put_blob(spec_bytes, "spec.yaml")
    ctx.registry.put_blob(repro, "REPRODUCE.md")
    return ctx.registry.publish(m)


def pipeline_link(ctx, name: str, registry_uri: str, engine: str = "http", spec: dict | None = None, dry_run=False):
    """Point a pipeline at a model already in a model registry (s3://<bucket>/<arch>/<brand>/<ver>). Reads its MANIFEST.yaml
    (read-only) for repro status and the DVC alias, so we keep one alias scheme."""
    from .storage.backend import open_store

    st = open_store(registry_uri)
    man = yaml.safe_load(st.get("MANIFEST.yaml"))
    draft = {"name": name, "engine": engine, "spec": spec or {"url": "https://set-me/v1/rank"},
             "registry_ref": registry_uri, "dvc_alias": man.get("dvc_alias"),
             "training_commit": man.get("training_commit"),
             "description": f"linked to {registry_uri}", "known_issues": man.get("known_issues") or []}
    m = pipeline_publish(ctx, draft, dry_run=True)
    status = str(man.get("reproducibility", man.get("status", "not-tested")))
    m = m.model_copy(update={"repro_status": status if status in ("exact-verified", "equivalent-verified") else "not-tested"})
    return m if dry_run else ctx.registry.publish(m)


def pipeline_diff(ctx, a: str, b: str) -> list[str]:
    import difflib

    ma, _ = ctx.registry.resolve("pipeline", a)
    mb, _ = ctx.registry.resolve("pipeline", b)
    da = yaml.safe_dump({"engine": ma.engine, "catalog": ma.catalog, "spec": ma.spec}, sort_keys=True).splitlines()
    db = yaml.safe_dump({"engine": mb.engine, "catalog": mb.catalog, "spec": mb.spec}, sort_keys=True).splitlines()
    return list(difflib.unified_diff(da, db, ref(ma), ref(mb), lineterm=""))


# ---- populations ----------------------------------------------------------------------


def population_create(ctx, name: str, simulator: str, size: int, seed: int, click_judgments: str | None = None,
                      zipf_s: float = 2.0, from_logs: str | None = None, dry_run=False) -> ShopperPopulation:
    if simulator not in SIMULATORS:
        get_simulator(simulator, ctx.workspace.name)  # raises for unknown third-party names
    if click_judgments:
        jm, _ = ctx.registry.resolve("dataset", click_judgments)
        click_judgments = ref(jm)
    people = personas(size, seed)
    body = json.dumps(people).encode()
    pers = {"devices": "mobile 62%, desktop 33%, tablet 5%", "budget": "20 + lognormal(3.5, 0.8)"}
    traffic = {"shape": "power-law", "s": zipf_s}
    if from_logs:
        from . import historic as H

        lm, lrows = H._rows(ctx, from_logs)
        from_logs = ref(lm)
        pers["exam"] = H.ctr_by_rank(lrows)
        # ctr(r) = exam(r) * attractiveness; normalize so rank 1 is examined almost always
        top = max(pers["exam"][0], 1e-6)
        pers["exam"] = [round(min(1.0, x / top * 0.95), 4) for x in pers["exam"]]
        traffic = {"shape": "logged", "from": from_logs}
    m = ShopperPopulation(**_base(ctx), name=check_name(name), version=0, simulator=simulator,
                          simulator_version=SIMULATORS.get(simulator, "external"), size=size, seed=seed,
                          personas=pers, from_logs=from_logs,
                          traffic=traffic, click_model_judgments=click_judgments,
                          files=[FileEntry(path="personas.json", sha256=sha256(body), bytes=len(body))])
    if dry_run:
        return m
    ctx.registry.put_blob(body, "personas.json")
    return ctx.registry.publish(m)


# ---- backtests ------------------------------------------------------------------------


def backtest_plan(ctx, name: str, arms: list[str], queries: str | None, judgments: str | None, population: str | None,
                  hypothesis: str, primary: str = "ndcg@10", mde: float = 0.003, dry_run=False, mode: str = "simulated",
                  logs: str | None = None, compute: str | None = None, population_size: int = 4000,
                  surfaces: list[dict] | None = None) -> Backtest:
    if len(arms) < 2:
        raise ValueError("a backtest needs at least two arms. The first is the baseline.")
    if compute:
        from . import compute as C

        C.get(ctx, compute)
    lpin = None
    if mode == "historic":
        if not logs:
            raise ValueError("historic mode needs --logs: an events dataset with query, product_id, position, clicked")
        from . import historic as H

        lm, _ = ctx.registry.resolve("dataset", logs)
        lpin = ref(lm)
        if dry_run:
            raise ValueError("a historic plan builds its query set and grades from the logs, so it has no dry run")
        if not queries:
            queries = ref(H.queries_from_logs(ctx, lpin, f"{name}/queries")) + ":holdout"
        if not judgments:
            judgments = ref(H.judgments_from_logs(ctx, lpin, f"{name}/judgments"))
        if not population:
            population = ref(population_create(ctx, f"{name}/shoppers", "cascade-v1", population_size, 1, from_logs=lpin))
    elif mode != "simulated":
        raise ValueError("mode is simulated or historic")
    if not queries:
        raise ValueError("--queries is required in simulated mode")
    pinned = []
    for a in arms:
        pm, _ = ctx.registry.resolve("pipeline", a)
        pinned.append(ref(pm))
    qm, split = ctx.registry.resolve("dataset", queries)
    if split == "holdout":
        s = check_seal(ctx, ref(qm))
        if not s["ok"]:
            raise ValueError(f"holdout check failed: {s['detail']}")
    qpin = ref(qm) + (f":{split}" if split else "")
    jpin = ref(ctx.registry.resolve("dataset", judgments)[0]) if judgments else None
    ppin = ref(ctx.registry.resolve("population", population)[0]) if population else None
    if primary not in M.NAMES and primary not in ("ctr", "conversion", "rpv", "ndcg@10_tw"):
        raise ValueError(f"unknown primary metric {primary}")
    if (primary in M.NAMES or primary == "ndcg@10_tw") and not jpin:
        raise ValueError(f"{primary} needs --judgments: nDCG and friends only come from graded judgments")
    if primary in ("ctr", "conversion", "rpv") and not ppin:
        raise ValueError(f"{primary} needs --population: it only comes from simulated shoppers")
    m = Backtest(**_base(ctx), name=check_name(name), version=0, mode=mode, logs=lpin, compute=compute,
                 arms=pinned, queries=qpin, judgments=jpin, surfaces=[_pin_surface(ctx, sf) for sf in (surfaces or [])],
                 population=ppin, preregistration=Preregistration(hypothesis=hypothesis, primary_metric=primary, mde=mde))
    names = [sf.name for sf in m.surfaces]
    if len(set(names)) != len(names):
        raise ValueError("surface names must be unique")
    return m if dry_run else ctx.registry.publish(m)


def _pin_surface(ctx, sf: dict) -> Surface:
    if not sf.get("name") or not sf.get("queries") or not sf.get("judgments"):
        raise ValueError("a surface needs a name, queries and judgments")
    qm, split = ctx.registry.resolve("dataset", sf["queries"])
    jm, _ = ctx.registry.resolve("dataset", sf["judgments"])
    guards = []
    for g in sf.get("guardrails") or []:
        g = Guardrail.model_validate(g)
        if g.metric not in M.NAMES:
            raise ValueError(f"guardrail metric {g.metric} is not an offline metric: use {', '.join(M.NAMES)}")
        if g.min is None and g.max_drop is None:
            raise ValueError(f"guardrail on {g.metric} needs a minimum or a largest allowed drop")
        guards.append(g)
    return Surface(name=check_name(sf["name"]), queries=ref(qm) + (f":{split}" if split else ""), judgments=ref(jm),
                   guardrails=guards)


SIM_BATCH = 100  # shoppers per batch: arms alternate by batch, so a live run shows the same shopper on each arm
LIVE_SESSIONS = 2  # shopper sessions per arm and batch sent to the live view


def _live_steps(ev: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k in ("t", "query", "results", "rank", "product_id", "grade", "price")} for e in ev]


def backtest_run(ctx, name: str, progress=None, live=None) -> BacktestRun:
    """Run a backtest here. `live` (a live.Run) gets its stages, progress, running metrics and sample shoppers."""
    import time as _time

    from . import costs as COST
    from . import segments as SEG

    t_start = _time.time()
    bt = ctx.registry.load("backtest", name)
    _, qs, split = rows_of(ctx, bt.queries)
    if split == "holdout":
        s = check_seal(ctx, bt.queries)
        if not s["ok"]:
            raise ValueError(f"holdout check failed: {s['detail']}")
    jm, jmap = judgments_map(ctx, bt.judgments) if bt.judgments else (None, {})
    offline_ev = J.EVIDENCE[jm.judgment_source or "heuristic"] if jm else Evidence.SIMULATED
    if jm and str((jm.tags or {}).get("synthetic", "")).lower() == "true":
        offline_ev = Evidence.SIMULATED  # grades replayed from synthetic logs are still simulated
    weights = {q["query_id"]: float(q["weight"]) for q in qs if q.get("weight") is not None} or None
    pop = ctx.registry.resolve("population", bt.population)[0] if bt.population else None
    people = json.loads(ctx.registry.get_blob(pop.files[0])) if pop else []
    click_map = judgments_map(ctx, pop.click_model_judgments)[1] if pop and pop.click_model_judgments else None
    perq, pershop = {}, {}
    n_arms = len(bt.arms)
    sim = get_simulator(pop.simulator, ctx.workspace.name) if pop else None
    # Captain's simulator draws each shopper from their own seed, so batches change nothing. A team's own simulator
    # may carry state from one shopper to the next: it gets everyone in one go.
    size = SIM_BATCH if sim is run_cascade else max(1, len(people))
    batches = [people[i:i + size] for i in range(0, len(people), size)] if pop else []
    total_steps = n_arms + len(batches) * n_arms or 1
    done_steps = 0

    def step(frac_of_step: float = 1.0):
        f = (done_steps - 1 + frac_of_step) / total_steps if frac_of_step < 1 else done_steps / total_steps
        if progress:
            progress(f)
        if live:
            live.progress(f, done=done_steps, total=total_steps, unit="steps")

    # 1. Rank every query with every arm, and score the rankings against the judgments.
    staged = []
    for n, a in enumerate(bt.arms):
        if live:
            live.stage(f"Rank {len(qs):,} queries with {a}", n + 1, n_arms + (2 if pop else 1))
        pm, eng, prods, ranks, ms = _rankings(ctx, a, qs)
        pq = M.per_query(ranks, jmap, _custom_metrics(ctx)) if jm else {}
        offline = M.summarize(pq) if jm else {}
        if jm:
            offline["coverage"] = offline.get("judged@10", 0.0)
            if weights:
                tot = sum(weights.get(q, 0.0) for q in pq) or 1.0
                offline["ndcg@10_tw"] = round(sum(r["ndcg10"] * weights.get(q, 0.0) for q, r in pq.items()) / tot, 4)
            if live:
                for k in ("ndcg@10", "p@10", "judged@10"):
                    if k in offline:
                        live.metric(k, offline[k], arm=a)
        rank_bytes = ("\n".join(json.dumps({"query_id": k, "ids": v, "ms": round(ms[k], 3)}) for k, v in sorted(ranks.items())) + "\n").encode()
        logs_entries = [ctx.registry.put_blob(rank_bytes, f"rankings/{pm.name}.jsonl")]
        staged.append({"arm": a, "pm": pm, "eng": eng, "prods": prods, "ms": ms, "offline": offline, "logs": logs_entries})
        perq[a] = pq
        done_steps += 1
        step()

    # 2. Simulated shoppers: each arm in turn, a batch at a time. A shopper's choices come from their own seed, so
    #    the logs are the same as simulating each arm in one go.
    if pop:
        if live:
            live.stage(f"Simulate {len(people):,} shoppers on {n_arms} arms", n_arms + 1, n_arms + 2)
        qtext = {q["query_id"]: q["query"] for q in qs}
        cache: dict = {}
        for st in staged:
            st["pmap"] = {p["id"]: p for p in (st["prods"] or [])}
            pershop[st["arm"]] = []

        def relevance_for(pmap):
            def relevance(qid, pid):
                if click_map is not None:
                    return click_map.get(qid, {}).get(pid, 0)
                k = (qid, pid)
                if k not in cache:
                    cache[k] = J.heuristic_grade(qtext[qid], pmap.get(pid, {}))
                return cache[k]
            return relevance

        for b, batch in enumerate(batches):
            for st in staged:
                a = st["arm"]
                logs = sim(batch, qs, lambda text, eng=st["eng"]: timed_search(eng, text, 50), relevance_for(st["pmap"]), st["pmap"],
                           zipf_s=float(pop.traffic.get("s", 2.0)), progress=None, weights=weights, exam=pop.personas.get("exam"))
                pershop[a] += logs
                done_steps += 1
                step()
                if live:
                    so_far = pershop[a]
                    seen = len(so_far) or 1
                    live.metric("conversion", round(sum(1 for x in so_far if x["bought"]) / seen, 4), arm=a, step=len(so_far))
                    live.metric("ctr", round(sum(1 for x in so_far if x["clicked"]) / seen, 4), arm=a, step=len(so_far))
                    live.metric("revenue per shopper", round(sum(x["revenue"] for x in so_far) / seen, 3), arm=a, step=len(so_far))
                    for x in logs[:LIVE_SESSIONS]:
                        first = next((e for e in x["events"] if e.get("t") == "search"), {})
                        live.emit("shopper", arm=a, shopper=x["shopper"], device=x.get("device"), query=first.get("query") or qtext.get(x["query_id"]),
                                  results=first.get("results"), steps=_live_steps(x["events"]), clicked=x["clicked"],
                                  bought=x["bought"], revenue=x["revenue"])

    arms: list[ArmResult] = []
    for st in staged:
        a = st["arm"]
        simulated = {}
        if pop:
            simulated = sim_summary(pershop[a])
            st["logs"].append(ctx.registry.put_blob(logs_to_bytes(pershop[a]), f"logs/{st['pm'].name}.jsonl"))
        arms.append(ArmResult(arm=a, offline=st["offline"], simulated=simulated,
                              judged_at_10=st["offline"].get("judged@10") if jm else None, evidence=offline_ev, logs=st["logs"],
                              cost=COST.arm_cost(ctx, a, st["ms"], bt.compute or "local")))
    if live:
        live.stage("Compare the arms", n_arms + (2 if pop else 1), n_arms + (2 if pop else 1))
    base = bt.arms[0]
    comps = []
    for a in bt.arms[1:]:
        if jm:
            for metric in ("ndcg@10", "p@10", "r@50", "mrr"):
                key = M.NAMES[metric]
                comps.append(compare({q: r[key] for q, r in perq[base].items()}, {q: r[key] for q, r in perq[a].items()},
                                     metric, bt.preregistration.mde if metric == bt.preregistration.primary_metric else 0.0,
                                     arm_a=base, arm_b=a) | {"evidence": offline_ev.value})
        if pop:
            for metric in ("ctr", "conversion", "rpv"):
                comps.append(compare(per_shopper(pershop[base], metric), per_shopper(pershop[a], metric), metric,
                                     bt.preregistration.mde if metric == bt.preregistration.primary_metric else 0.0,
                                     arm_a=base, arm_b=a) | {"evidence": Evidence.SIMULATED.value})
    for c in comps:
        c["primary"] = c["metric"] == bt.preregistration.primary_metric
    independent = not (pop and pop.click_model_judgments and bt.judgments and pop.click_model_judgments == bt.judgments)
    complete = all((r.judged_at_10 or 0) >= 0.9 for r in arms) if jm else True
    files = [e for r in arms for e in r.logs]
    if jm and weights:
        for a in bt.arms[1:]:
            c = compare({q: r["ndcg10"] * len(perq[base]) * weights.get(q, 0) / (sum(weights.get(x, 0) for x in perq[base]) or 1)
                         for q, r in perq[base].items()},
                        {q: r["ndcg10"] * len(perq[a]) * weights.get(q, 0) / (sum(weights.get(x, 0) for x in perq[a]) or 1)
                         for q, r in perq[a].items()},
                        "ndcg@10_tw", bt.preregistration.mde if bt.preregistration.primary_metric == "ndcg@10_tw" else 0.0,
                        arm_a=base, arm_b=a) | {"evidence": offline_ev.value}
            c["primary"] = bt.preregistration.primary_metric == "ndcg@10_tw"
            comps.append(c)
    qpd = COST.settings(ctx)["queries_per_day"]
    by_arm = {r.arm: r for r in arms}
    for c in comps:
        if c["primary"]:
            c["cost"] = COST.roi(by_arm[c["a"]].cost, by_arm[c["b"]].cost, c["delta"], c["verdict"], qpd)
    segs = SEG.segments(qs, perq, bt.arms, bt.preregistration.mde) if jm else []
    surfaces = [SEG.score_surface(ctx, sf, bt.arms, bt.preregistration.mde, lambda a, q: _rankings(ctx, a, q)[3:5])
                for sf in bt.surfaces]
    run = BacktestRun(**_base(ctx), name=name, version=0, backtest=ref(bt), mode=bt.mode, compute=os.environ.get("CST_COMPUTE") or bt.compute or "local",
                      arms=arms, comparisons=comps, segments=segs, surfaces=surfaces,
                      independent=independent, complete=complete, files=files, wall_s=round(_time.time() - t_start, 2),
                      manifest=_run_manifest(ctx, bt, pop),
                      description=f"{len(qs)} queries, {len(people)} shoppers per arm")
    return ctx.registry.publish(run)


def backtest_launch(ctx, name: str, compute: str | None = None, progress=None, dry_run: bool = False, live=None,
                    live_id: str | None = None) -> dict:
    """Run locally, or submit to the backtest's (or the given) compute target. `live` streams a run here; `live_id`
    is the remote live run (live.start_remote) a submitted job streams to."""
    from . import compute as C

    from . import settings as ST

    bt = ctx.registry.load("backtest", name)
    # Inside a submitted job, run here: never submit the job again.
    in_job = bool(os.environ.get("CST_JOB_ID"))
    target = C.get(ctx, "local" if in_job else compute or bt.compute or ST.default_compute(ctx, "backtest") or "local")
    if target["kind"] == "local":
        if dry_run:
            return {"compute": target["name"], "plan": C.plan_backtest(ctx, target, name)}
        r = backtest_run(ctx, name, progress=progress, live=live)
        return {"compute": "local", "run": ref(r)}
    plan, job = C.plan_and_job(ctx, target, ["backtest", "run", name], label=name, backtest=name)
    if dry_run:
        return {"compute": target["name"], "plan": plan}
    if live_id:
        job.env["CST_LIVE_ID"] = live_id  # the container streams its events to this run, through the bucket
        plan = C.P.get(target, ctx).plan(job)
    rec = C.submit_job(ctx, target, job.argv, label=name, kind="backtest", job=job)
    return {"compute": target["name"], "plan": plan, "job": rec["id"], "live": live_id,
            "submitted": {"provider_job": C.provider_job(rec["handle"]), "job": rec["id"]},
            "detail": f"the run lands in the workspace bucket when the job finishes. Follow it with `cst job show {rec['id']}`."}


def backtest_report(run: BacktestRun, bt: Backtest) -> str:
    L = [f"# Backtest {run.name} run v{run.version} ({run.mode}, on {run.compute})", "",
         f"Hypothesis (pre-registered {bt.preregistration.written_at:%Y-%m-%d}): {bt.preregistration.hypothesis}",
         f"Primary metric: {bt.preregistration.primary_metric}, smallest effect that counts: {bt.preregistration.mde}",
         f"Queries: {bt.queries}   Judgments: {bt.judgments or 'none'}   Population: {bt.population or 'none'}", ""]
    if run.mode == "imported":
        L += [f"Imported readout: {run.source}", run.description, ""]
    if not run.independent:
        L += ["WARNING: not independent. The shoppers' clicks and the nDCG come from the same judgments,", "so they are one signal counted twice.", ""]
    if not run.complete:
        L += ["WARNING: incomplete. An arm has judged@10 under 90%, so its nDCG is understated.",
              "Fill the holes with `cst judge run` on these arms, then rerun.", ""]
    if any(r.offline for r in run.arms):
        L += [f"## Offline, from judgments [{run.arms[0].evidence.value}]", "",
              f"{'arm':32} {'nDCG@10':>8} {'P@10':>6} {'R@50':>6} {'MRR':>6} {'judged@10':>9}"]
        for r in run.arms:
            o = r.offline
            tw = f"  traffic-weighted nDCG@10 {o['ndcg@10_tw']:.4f}" if "ndcg@10_tw" in o else ""
            L.append(f"{r.arm:32} {o['ndcg@10']:8.4f} {o['p@10']:6.3f} {o['r@50']:6.3f} {o['mrr']:6.3f} {o['judged@10']:9.0%}{tw}")
        L.append("")
        if run.mode == "historic":
            L += ["Grades come from logged clicks (clicks over expected clicks at each rank). judged@10 is the share of",
                  "each arm's top 10 that the old ranking ever showed; unseen products count as not relevant.", ""]
    if any(r.simulated for r in run.arms):
        L += ["## Simulated shoppers [SIMULATED]", "", f"{'arm':32} {'shoppers':>8} {'CTR':>6} {'conv':>6} {'RPV':>8} {'p99 ms':>7}"]
        for r in run.arms:
            s = r.simulated
            L.append(f"{r.arm:32} {int(s['shoppers']):8d} {s['ctr']:6.3f} {s['conversion']:6.3f} {s['rpv']:8.2f} {s['p99_ms']:7.2f}")
        L.append("")
    L += ["## Comparisons (b minus a, paired, 95% bootstrap CI)", ""]
    for c in run.comparisons:
        star = "*" if c.get("primary") else " "
        L.append(f"{star} {c['metric']:10} {c['b']} vs {c['a']}: {c['delta']:+.4f} [{c['ci_low']:+.4f}, {c['ci_high']:+.4f}] "
                 + (f"p={c['p']:.3f}  " if c.get("p") is not None else "") + f"{c['verdict'].upper()}  [{c['evidence']}]")
    L += ["", "* primary metric. NOISE means the CI crosses zero or the gain is under the pre-registered minimum.",
          "Simulated results do not prove revenue. A live deploy needs human labels or real traffic."]
    L += _report_cost(run) + _report_segments(run) + _report_surfaces(run)
    return "\n".join(L)


def _f(v, fmt):
    return "-" if v is None else format(v, fmt)


def _report_cost(run: BacktestRun) -> list[str]:
    if not any(a.cost for a in run.arms):
        return []
    L = ["", "## Cost", "", f"{'arm':32} {'train $':>9} {'serve $/1k':>10} {'p50 ms':>7} {'p99 ms':>7}"]
    for a in run.arms:
        c = a.cost
        L.append(f"{a.arm:32} {_f(c.get('train_usd'), '9.2f')} {_f(c.get('serve_usd_per_1k'), '10.4f')} "
                 f"{_f(c.get('p50_ms'), '7.2f')} {_f(c.get('p99_ms'), '7.2f')}")
    for c in run.comparisons:
        r = c.get("cost")
        if not r:
            continue
        if r.get("flag") is None:
            L.append(f"{c['b']}: no cost recorded for these arms. Set one with `cst pipeline cost set`.")
            continue
        money = f"extra {r['extra_monthly_usd']:+,.2f} $/month" if r.get("extra_monthly_usd") is not None else r.get("needs") or ""
        per = f", {r['gain_per_1k_usd']:+.5f} {c['metric']} per $1k a month" if r.get("gain_per_1k_usd") is not None else ""
        L.append(f"{c['b']} vs {c['a']}: {r['flag'].upper()}  ({money}{per})")
    L.append(f"Latency is {run.arms[0].cost.get('latency_source', 'measured')}. Wall time of this run: {_f(run.wall_s, '.1f')} s.")
    return L


def _report_segments(run: BacktestRun) -> list[str]:
    if not run.segments:
        return []
    L = ["", "## Segments (nDCG@10, challenger minus baseline)", ""]
    for r in run.segments:
        if r["delta"] is None:
            continue
        L.append(f"{r['kind']:8} {r['segment']:16} {r['arm']:28} n={r['n']:<5} {r['delta']:+.4f} "
                 f"[{r['ci_low']:+.4f}, {r['ci_high']:+.4f}] {str(r['verdict']).upper()}")
    return L


def _report_surfaces(run: BacktestRun) -> list[str]:
    L = []
    for sf in run.surfaces:
        L += ["", f"## Surface: {sf['surface']} ({sf['queries']}, judged by {sf['judgments']})", ""]
        for a in sf["arms"]:
            m = a["metrics"]
            L.append(f"{a['arm']:32} nDCG@10 {m.get('ndcg@10', 0):.4f}  R@50 {m.get('r@50', 0):.3f}  judged@10 {m.get('judged@10', 0):.0%}")
        for c in sf["comparisons"]:
            L.append(f"  {c['metric']:8} {c['b']} vs {c['a']}: {c['delta']:+.4f} [{c['ci_low']:+.4f}, {c['ci_high']:+.4f}] {c['verdict'].upper()}")
        for g in sf["guardrails"]:
            L.append(f"  [{'ok ' if g['ok'] else 'FAIL'}] {g['arm']} {g['metric']} {g['rule']}: {g['value']}")
    return L


def backtest_verify(ctx, name: str, version: int | None = None) -> dict:
    """Rerun the simulator and compare with the stored logs. Reports the share of shoppers whose
    logs differ. Stored logs stay the source of truth either way."""
    run = ctx.registry.load("backtest-run", name, version)
    bt = ctx.registry.load("backtest", name)
    if not bt.population:
        return {"run": ref(run), "checked": False, "detail": "no population, nothing to rerun"}
    _, qs, _ = rows_of(ctx, bt.queries)
    pop = ctx.registry.resolve("population", bt.population)[0]
    people = json.loads(ctx.registry.get_blob(pop.files[0]))
    out = {"run": ref(run), "checked": True, "arms": []}
    for arm in run.arms:
        stored = next((e for e in arm.logs if e.path.startswith("logs/")), None)
        if stored is None:
            continue
        old = logs_from_bytes(ctx.registry.get_blob(stored))
        pm, eng, prods, _, _ = _rankings(ctx, arm.arm, qs[:1])
        pmap = {p["id"]: p for p in (prods or [])}
        qtext = {q["query_id"]: q["query"] for q in qs}
        cm = judgments_map(ctx, pop.click_model_judgments)[1] if pop.click_model_judgments else None
        rel = (lambda q, p: cm.get(q, {}).get(p, 0)) if cm is not None else (lambda q, p: J.heuristic_grade(qtext[q], pmap.get(p, {})))
        new = get_simulator(pop.simulator, ctx.workspace.name)(people, qs, lambda t: timed_search(eng, t, 50), rel, pmap,
                                           zipf_s=float(pop.traffic.get("s", 2.0)))
        strip = lambda l: {k: v for k, v in l.items() if k != "ms"}  # latency always varies
        differ = sum(1 for a, b in zip(old, new) if strip(a) != strip(b)) + abs(len(old) - len(new))
        out["arms"].append({"arm": arm.arm, "shoppers": len(old), "differ": differ,
                            "share_differ": round(differ / max(1, len(old)), 5)})
    return out


# ---- deploys --------------------------------------------------------------------------


def env_key(name: str) -> str:
    return f"environments/{name}.yaml"


def env_create(ctx, name: str, tier: str, target: dict, gates: dict | None = None, compute: str | None = None) -> Environment:
    if not compute and (target or {}).get("type", "record") not in ("record", "webhook", "captain"):
        raise ValueError(f"target type {target['type']} is served by a compute target: pick one of that kind as compute "
                         "(Compute screen), or use record, webhook or captain")
    if (target or {}).get("type") in ("webhook", "captain") and not target.get("url"):
        raise ValueError(f"a {target['type']} target needs a url")
    if compute:
        from . import compute as C

        t = C.get(ctx, compute)
        if "serve" not in t["purposes"]:
            raise ValueError(f"compute target {compute} ({t['kind']}) cannot serve pipelines")
        target = {**target, "type": target.get("type") if target.get("type") not in (None, "record") else t["kind"]}
    e = Environment(**_base(ctx), name=check_name(name), tier=Tier(tier), target=target, compute=compute,
                    gates=gates or ({"p99_ms": 50.0, "recall_floor": 0.98, "min_evidence": "HUMAN_GRADED"} if tier == "live" else {}))
    ctx.store.put_new(env_key(name), dump_yaml(e.model_dump(mode="json")))
    return e


def env_load(ctx, name: str) -> Environment:
    try:
        return Environment.model_validate(yaml.safe_load(ctx.store.get(env_key(name))))
    except NotFound:
        raise NotFound(f"no environment {name!r}. Create it with `cst deploy env add`.") from None


def env_list(ctx) -> list[dict]:
    out = []
    for k in ctx.store.list("environments/"):
        if not k.endswith(".yaml"):
            continue
        e = Environment.model_validate(yaml.safe_load(ctx.store.get(k)))
        cur = current(ctx, e.name)
        region = None
        if e.compute:
            from . import compute as C

            try:
                region = C.get(ctx, e.compute).get("region")
            except NotFound:
                region = None
        hist = history(ctx, e.name)
        endpoint = next((h.gate_results.get("target", {}).get("endpoint") for h in reversed(hist)
                         if h.gate_results.get("target", {}).get("endpoint")), None)
        out.append({"name": e.name, "tier": e.tier.value, "target": e.target, "compute": e.compute, "region": region,
                    "endpoint": endpoint, "current": cur["pipeline"] if cur else None})
    return out


def current_key(env: str) -> str:
    return f"aliases/environment/{env}/current.yaml"


def current(ctx, env: str) -> dict | None:
    try:
        return yaml.safe_load(ctx.store.get(current_key(env)))
    except NotFound:
        return None


def history(ctx, env: str) -> list[Deployment]:
    return [Deployment.model_validate(yaml.safe_load(ctx.store.get(k))) for k in ctx.store.list(f"deployments/{env}/")
            if k.endswith(".yaml")]


def _latest_run_for(ctx, pipeline_ref: str) -> BacktestRun | None:
    best = None
    for name in ctx.registry.names("backtest-run"):
        r = ctx.registry.load("backtest-run", name)
        # ULIDs sort by creation time below a second, so they break ties between runs made in the same second
        if any(a.arm == pipeline_ref for a in r.arms[1:]) and (best is None or (r.created_at, r.id) > (best.created_at, best.id)):
            best = r
    return best


def gates_for(ctx, env: Environment, pipeline_ref: str) -> list[dict]:
    g = env.gates
    res = []
    run = _latest_run_for(ctx, pipeline_ref)
    if run is None:
        return [{"gate": "backtest", "ok": False, "detail": f"no backtest run has {pipeline_ref} as a challenger arm"}]
    bt = ctx.registry.load("backtest", run.name)
    primary = bt.preregistration.primary_metric
    comp = next((c for c in run.comparisons if c["b"] == pipeline_ref and c["metric"] == primary), None)
    res.append({"gate": "primary_metric", "ok": bool(comp and comp["verdict"] == "win"),
                "detail": f"{primary} {comp['delta']:+.4f} [{comp['ci_low']:+.4f}, {comp['ci_high']:+.4f}] {comp['verdict']}" if comp else "no comparison",
                "run": ref(run)})
    arm = next(a for a in run.arms if a.arm == pipeline_ref)
    need = Evidence(g.get("min_evidence", "HUMAN_GRADED"))
    ev = arm.evidence if primary in M.NAMES else arm.simulated_evidence
    res.append({"gate": "evidence", "ok": ev.rank >= need.rank, "detail": f"{ev.value}, needs {need.value} or stronger"})
    res.append({"gate": "complete", "ok": run.complete and run.independent,
                "detail": "complete and independent" if run.complete and run.independent else
                ("not independent" if not run.independent else "judged@10 under 90%")})
    if bt.queries.endswith(":holdout"):
        res.append(check_seal(ctx, bt.queries))
    base = run.arms[0]
    if base.offline and arm.offline:
        floor = float(g.get("recall_floor", 0.98))
        ok = arm.offline["r@50"] >= floor * base.offline["r@50"]
        res.append({"gate": "recall", "ok": ok, "detail": f"R@50 {arm.offline['r@50']:.3f} vs floor {floor:.0%} of {base.offline['r@50']:.3f}"})
    from . import proposals as PR

    prop = PR.approval_for(ctx, pipeline_ref)
    if prop is not None:
        ok = bool(prop.get("review") and prop["review"]["decision"] == "approved")
        res.append({"gate": "human_approval", "ok": ok,
                    "detail": f"agent {prop['agent']} proposed it; approved by {prop['review']['by']}" if ok
                    else f"agent {prop['agent']} proposed it in {prop['id']}, and no person has approved that proposal"})
    guards = [x for sf in run.surfaces for x in sf.get("guardrails", []) if x["arm"] == pipeline_ref]
    if guards:
        bad = [f"{sf['surface']} {x['metric']} {x['rule']} ({x['value']})" for sf in run.surfaces for x in sf.get("guardrails", [])
               if x["arm"] == pipeline_ref and not x["ok"]]
        res.append({"gate": "surface_guardrails", "ok": not bad,
                    "detail": "all hold on " + ", ".join(sf["surface"] for sf in run.surfaces) if not bad else "failed: " + "; ".join(bad)})
    if arm.simulated:
        budget = float(g.get("p99_ms", 50.0))
        res.append({"gate": "latency", "ok": arm.simulated["p99_ms"] <= budget, "detail": f"p99 {arm.simulated['p99_ms']:.1f} ms, budget {budget:.0f} ms"})
    return res


def _apply_target(env: Environment, pm: PipelineVersion, ctx=None, dry_run: bool = False) -> dict:
    t = env.target or {"type": "record"}
    if env.compute and ctx is not None:
        from . import compute as C

        target = C.get(ctx, env.compute)
        plan = C.plan_serve(ctx, target, ref(pm), env.name)
        if dry_run:
            return {"type": target["kind"], "plan": plan, "executed": False}
        return {"type": target["kind"], "plan": plan, "executed": True, **C.execute_serve(ctx, target, plan)}
    if t.get("type", "record") == "record":
        return {"type": "record", "detail": "recorded only; no external system was changed"}
    if t["type"] in ("webhook", "captain"):
        import httpx

        from .workspace import get_secret

        body = {"pipeline": ref(pm), "engine": pm.engine, "spec": pm.spec, "catalog": pm.catalog}
        plan = [{"service": t["type"], "call": "POST", "args": {"url": t["url"], "json": body,
                                                               "auth": f"Bearer {{secret:{t['token_secret']}}}" if t.get("token_secret") else None}}]
        if dry_run:
            return {"type": t["type"], "plan": plan, "executed": False}
        h = {"Authorization": f"Bearer {get_secret(t['token_secret'])}"} if t.get("token_secret") else {}
        try:
            r = httpx.post(t["url"], json=body, headers=h, timeout=60)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ValueError(f"the {t['type']} target at {t['url']} failed: {e}") from None
        return {"type": t["type"], "plan": plan, "executed": True, "status": r.status_code}
    raise ValueError(f"unknown target type {t['type']}")


def promote(ctx, pipeline: str, env_name: str, override_reason: str | None = None, confirm: bool = False,
            dry_run=False) -> Deployment:
    env = env_load(ctx, env_name)
    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    pref = ref(pm)
    results = [] if env.tier == Tier.demo else gates_for(ctx, env, pref)
    failed = [r for r in results if not r["ok"]]
    if env.tier == Tier.live and not confirm:
        raise GateFailed("live deploys need --confirm", results)
    if failed and not override_reason:
        raise GateFailed(f"{len(failed)} gate(s) failed: " + "; ".join(f"{r['gate']}: {r['detail']}" for r in failed), results)
    prev = current(ctx, env_name)
    if env.tier == Tier.live:
        # Captain stays outside production: a live "promote" is a handoff. Readiness is checked and recorded, a
        # candidate bundle is written, and nothing is deployed. The customer's deployment process takes it from here.
        from . import handoff as HO

        b = HO.bundle(ctx, pref, env_name, results, override_reason if failed else None)
        d = Deployment(**_base(ctx), environment=env_name, pipeline=pref, action="handoff", handoff=b["id"],
                       gate_results={"tier": env.tier.value, "gates": results, "overridden": [r["gate"] for r in failed],
                                     "target": {"type": "handoff", "executed": False,
                                                "detail": "nothing deployed. Deploy through your own process, then `cst handoff link`."}},
                       override_reason=override_reason if failed else None, rollback_to=prev["pipeline"] if prev else None)
        if not dry_run:
            HO.write(ctx, b)
            ctx.store.put_new(f"deployments/{d.environment}/{d.created_at:%Y%m%dT%H%M%SZ}-{d.id}.yaml", dump_yaml(d.model_dump(mode="json")))
        return d
    d = Deployment(**_base(ctx), environment=env_name, pipeline=pref, action="promote",
                   gate_results={"tier": env.tier.value, "gates": results, "overridden": [r["gate"] for r in failed]},
                   override_reason=override_reason if failed else None, rollback_to=prev["pipeline"] if prev else None)
    if dry_run:
        d.gate_results["target"] = _apply_target(env, pm, ctx, dry_run=True)
        return d
    d.gate_results["target"] = _apply_target(env, pm, ctx)
    _record(ctx, d, prev)
    return d


def _record(ctx, d: Deployment, prev: dict | None) -> None:
    key = f"deployments/{d.environment}/{d.created_at:%Y%m%dT%H%M%SZ}-{d.id}.yaml"
    ctx.store.put_new(key, dump_yaml(d.model_dump(mode="json")))
    try:
        _, tag = ctx.store.get_with_tag(current_key(d.environment))
    except NotFound:
        tag = None
    if prev is not None and tag is None:
        raise Conflict("the current deployment changed during the promote")
    ctx.store.put_if(current_key(d.environment), dump_yaml({"pipeline": d.pipeline, "deployment": d.id}), tag)


def rollback(ctx, env_name: str, confirm: bool = False, dry_run=False) -> Deployment:
    env = env_load(ctx, env_name)
    if env.tier == Tier.live:
        raise ValueError(f"{env_name} is live, and Captain stays outside production: roll back through your own deployment "
                         "process, then record what is live with `cst handoff link` on the earlier handoff.")
    cur = current(ctx, env_name)
    if cur is None:
        raise ValueError(f"nothing is deployed to {env_name}")
    hist = [h for h in history(ctx, env_name) if h.id == cur["deployment"]]
    target = hist[0].rollback_to if hist else None
    if not target:
        raise ValueError(f"{env_name} has no earlier version to roll back to")
    if env.tier == Tier.live and not confirm:
        raise GateFailed("live rollbacks need --confirm", [])
    pm, _ = ctx.registry.resolve("pipeline", target)
    prior = [h for h in history(ctx, env_name) if h.pipeline == target]
    d = Deployment(**_base(ctx), environment=env_name, pipeline=target, action="rollback",
                   rollback_to=prior[-1].rollback_to if prior else None, gate_results={"from": cur["pipeline"]})
    if dry_run:
        d.gate_results["target"] = _apply_target(env, pm, ctx, dry_run=True)
        return d
    d.gate_results["target"] = _apply_target(env, pm, ctx)
    _record(ctx, d, cur)
    return d


# ---- lineage ----------------------------------------------------------------------------


def lineage(ctx) -> dict:
    nodes, edges = {}, []

    def node(kind, r, label=None, scope=None):
        nid = f"{kind}:{scope + ':' if scope else ''}{r}"
        nodes[nid] = {"id": nid, "kind": kind, "label": label or r}
        return nid

    for n in ctx.registry.names("dataset"):
        for v in ctx.registry.versions("dataset", n):
            m = ctx.registry.load("dataset", n, v)
            me = node("dataset", ref(m))
            p = m.provenance
            if p.connection:
                edges.append({"src": node("connection", p.connection), "dst": me, "relation": "produced_by"})
            for key in ("catalog", "queries"):
                if isinstance(p.params.get(key), str):
                    edges.append({"src": node("dataset", ref(ctx.registry.resolve("dataset", p.params[key].split(":")[0])[0])), "dst": me, "relation": "derived_from"})
            if m.parent and m.holdout_seal:
                edges.append({"src": node("dataset", f"{m.name}@v{m.parent}"), "dst": me, "relation": "derived_from"})
    for n in ctx.registry.names("pipeline"):
        for v in ctx.registry.versions("pipeline", n):
            m = ctx.registry.load("pipeline", n, v)
            me = node("pipeline", ref(m))
            if m.catalog:
                edges.append({"src": node("dataset", m.catalog), "dst": me, "relation": "derived_from"})
            for par in filter(None, (m.tags or {}).get("parents", "").split(",")):
                vs = ctx.registry.versions("pipeline", par)
                src = node("pipeline", f"{par}@v{vs[-1]}") if vs else node("model-unregistered", par)
                edges.append({"src": src, "dst": me, "relation": "parent"})
            if m.registry_ref:
                edges.append({"src": node("registry", m.registry_ref), "dst": me, "relation": "produced_by"})
    for n in ctx.registry.names("backtest-run"):
        for v in ctx.registry.versions("backtest-run", n):
            r = ctx.registry.load("backtest-run", n, v)
            me = node("run", ref(r))
            bt = ctx.registry.load("backtest", n)
            for a in r.arms:
                if "@v" in a.arm:
                    src = node("pipeline", a.arm)
                else:  # a baseline named in an imported readout: one node per run, not shared across brands
                    src = node("baseline", a.arm, label=f"{a.arm} ({(bt.tags or {}).get('brand', n)})", scope=n)
                edges.append({"src": src, "dst": me, "relation": "evaluated_on"})
            if bt.judgments:
                edges.append({"src": node("dataset", bt.judgments), "dst": me, "relation": "evaluated_on"})
            if bt.mode != "imported":
                edges.append({"src": node("dataset", bt.queries.split(":")[0]), "dst": me, "relation": "evaluated_on"})
            if bt.logs:
                edges.append({"src": node("dataset", bt.logs), "dst": me, "relation": "evaluated_on"})
    for e in env_list(ctx):
        me = node("environment", e["name"])
        if e["current"]:
            edges.append({"src": node("pipeline", e["current"]), "dst": me, "relation": "deployed_from"})
    return {"nodes": list(nodes.values()), "edges": edges}
