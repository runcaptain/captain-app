"""Starting a project from nothing: a keyword-search baseline, then a first trained ranker, with no code.

A project with a catalog and some events has everything a first ranker needs, but until now "New candidate" asked
for a training command. These three steps close that gap:

  baseline_start   make a model version that needs no training (BM25 keyword search over the project's catalog)
                   and set it as the project's baseline, so there is something to compare against;
  plan             say what a starter ranker would train on, which dates it would cut the events at and why, and
                   what is missing;
  train            build the training set (training_set.py, with its leak checks), train LightGBM lambdarank on it
                   through the trainer contract (trainers/), and record the result as a candidate.

The trained version is a `bm25-ltr` model version: keyword search finds the candidates and the model reorders the
top 50, with features from the one module that built the training set (features/). The project's own evaluation
then judges it against the baseline like any other candidate, with the same evidence labels and the same rule that
a model judged by the labeler it learned from "agrees with its own labeler".
"""
from __future__ import annotations

from . import features as F
from . import project_state as PS
from .workspace import Context

TRAINER = "lightgbm-ltr"
TOP_K = 50


def _names(pid: str) -> dict:
    return {"baseline": f"{pid}/keyword-baseline", "ranker": f"{pid}/starter-ranker", "training": f"{pid}/starter-training"}


def _catalog_spec(ctx: Context, catalog: str, title: str | None = None) -> tuple[dict, list[str]]:
    m, _ = ctx.registry.resolve("dataset", catalog)
    cols = [f["name"] for f in m.schema_fields]
    if not cols:  # kept by reference: read one row for its columns
        from .ops import rows_of

        rows = rows_of(ctx, catalog)[1]
        cols = list(rows[0]) if rows else []
    if "id" not in cols:
        raise ValueError(f"{m.name}@v{m.version} has no id column, so search results can't be matched to events or "
                         "judgments. Prepare the catalog first (Data, Prepare) and say which column is the id.")
    return F.detect_spec(cols, title), cols


def _retrieve(spec: dict) -> dict:
    return F.retrieve_spec(spec)


def baseline_plan(ctx: Context, pid: str, title: str | None = None) -> dict:
    """What `baseline_start` would do. `ready` is false, with the reason in `text`, when it can't."""
    doc = PS.get(ctx, pid)
    catalog = PS.pinned(doc, "catalog")
    out = {"project": pid, "champion": doc.get("champion"), "catalog": catalog, "name": _names(pid)["baseline"],
           "ready": False, "ask_title": False, "columns": [], "title": None, "fields": None}
    if doc.get("champion"):
        return {**out, "text": f"{doc['champion']} is already the baseline."}
    if not catalog:
        return {**out, "text": "Choose a catalog first. Keyword search needs products to search."}
    try:
        spec, cols = _catalog_spec(ctx, catalog, title)
    except ValueError as e:
        cols = [f["name"] for f in ctx.registry.resolve("dataset", catalog)[0].schema_fields]
        return {**out, "columns": cols, "ask_title": "title" in str(e), "text": str(e)}
    return {**out, "ready": True, "columns": cols, "title": spec["title"], "fields": _retrieve(spec)["fields"],
            "text": f"Keyword search over {catalog}: the {spec['title']} column counts most"
                    + (f", then {', '.join(k for k in _retrieve(spec)['fields'] if k != spec['title'])}" if len(_retrieve(spec)['fields']) > 1 else "")
                    + ". It needs no training."}


def baseline_start(ctx: Context, pid: str, title: str | None = None, dry_run: bool = False) -> dict:
    """Make the keyword-search version for the project's catalog and set it as the baseline."""
    from . import ops

    p = baseline_plan(ctx, pid, title)
    if not p["ready"]:
        raise ValueError(p["text"])
    spec = {**ops.TEMPLATE["local-bm25"], **_retrieve(F.detect_spec(p["columns"], title))}
    name = p["name"]
    ref_, created = None, False
    try:
        old = ctx.registry.load("pipeline", name)
        if old.catalog == p["catalog"] and old.spec == spec:
            ref_ = ops.ref(old)  # the same search over the same catalog: no new version
    except Exception:  # noqa: BLE001
        old = None
    if dry_run:
        return {**p, "baseline": ref_ or f"{name}@v{(old.version + 1) if old else 1}", "created": ref_ is None, "dry_run": True}
    if ref_ is None:
        pm = ops.pipeline_publish(ctx, {"name": name, "engine": "local-bm25", "catalog": p["catalog"], "spec": spec,
                                        "description": f"keyword search (BM25) over {p['catalog']}: no training, the "
                                                       "starting baseline"})
        ref_, created = ops.ref(pm), True
    PS.set_champion(ctx, pid, ref_, expect=None)
    return {**p, "baseline": ref_, "created": created, "champion": ref_, "dry_run": False,
            "text": f"Keyword search over {p['catalog']} is now the baseline ({ref_}). Candidates are judged against it."}


def plan(ctx: Context, pid: str) -> dict:
    """What a starter ranker would use from the project's inputs, the cutoff it picked and why, and what is missing.
    Reads the events once for their dates. Writes nothing."""
    from . import historic as H
    from . import trainers as TR
    from . import training_set as TS
    from .ops import rows_of

    doc = PS.get(ctx, pid)
    catalog, events = PS.pinned(doc, "catalog"), PS.pinned(doc, "events")
    labels, queries, holdout = PS.pinned(doc, "labels"), PS.pinned(doc, "queries"), PS.pinned(doc, "holdout")
    out = {"project": pid, "trainer": TRAINER, "catalog": catalog, "events": events, "judgments": labels,
           "queries": queries, "holdout": holdout, "champion": doc.get("champion"), "ready": False, "missing": [],
           "simulated": False, "labels_from": None, "as_of": None, "train_until": None, "why": None, "warnings": [],
           "uses": [], "setup": TR.status(), "names": _names(pid), "baseline": None}
    if not catalog:
        return {**out, "missing": [s["id"] for s in PS.by_role(doc, "catalog")][:1],
                "text": "A starter ranker needs a catalog: the products it ranks."}
    try:
        spec, _ = _catalog_spec(ctx, catalog)
    except ValueError as e:
        return {**out, "text": str(e)}
    out["uses"].append({"role": "catalog", "ref": catalog, "text": f"Products and their text, from {catalog}."})
    if not events and not labels:
        return {**out, "missing": [s["id"] for s in PS.by_role(doc, "events")][:1],
                "text": "A starter ranker learns from clicks or from judgments, and this project has neither. Add "
                        "events, or generate simulated ones to try the flow."}
    cut = None
    if events:
        em, rows, _ = rows_of(ctx, events)
        if rows and not H.REQUIRED <= set(rows[0]):
            if not labels:
                return {**out, "text": f"{events} needs the columns {', '.join(sorted(H.REQUIRED))} to learn from clicks."}
            events = None
        else:
            out["simulated"] = H.synthetic(em)
            try:
                cut = TS.cutoffs(rows)
            except ValueError as e:
                if not labels:
                    return {**out, "text": str(e)}
                out["warnings"].append(str(e))
    if cut:
        out.update(as_of=cut["as_of"], train_until=cut["train_until"], labels_from="clicks",
                   why=f"The events run from {cut['first']} to {cut['last']}. Click rates used as features count "
                       f"searches up to {cut['as_of']}. Labels come from clicks after that, up to {cut['train_until']}. "
                       f"Everything after {cut['train_until']}, about the last fifth, is kept back to judge the model, "
                       "so it is never judged on searches it learned from.")
        out["uses"].append({"role": "events", "ref": events, "simulated": out["simulated"],
                            "text": f"Clicks and purchases as labels, and popularity as features, from {events}."})
    if labels:
        out["uses"].append({"role": "labels", "ref": labels,
                            "text": f"Judgments from {labels}, when they grade enough queries outside the sealed test set."})
        out["labels_from"] = out["labels_from"] or "judgments"
    if holdout:
        out["uses"].append({"role": "holdout", "ref": holdout,
                            "text": f"Queries in the sealed test set ({holdout}) are left out of training."})
    if out["simulated"]:
        out["warnings"].append("These events are simulated. The ranker will learn the simulator, not real shoppers, "
                               "and every result judged on them reads SIMULATED.")
    if not doc.get("champion"):
        bp = baseline_plan(ctx, pid)
        out["baseline"] = {"will_set": bp["ready"], "name": bp["name"],
                           "text": "No baseline yet, so keyword search is set as the baseline first." if bp["ready"] else bp["text"]}
    out["features"] = F.feature_names(spec, bool(cut))
    return {**out, "ready": True,
            "text": "Learns to rank from your " + ("clicks" if out["labels_from"] == "clicks" else "judgments")
                    + ". Keyword search finds the candidates and the model reorders the top "
                    + f"{TOP_K}."}


def train(ctx: Context, pid: str, live=None, params: dict | None = None, progress=None) -> dict:
    """Set the keyword baseline if there is none, build the training set, train the starter ranker, and record it
    as a candidate. `live` is the live run its progress goes to (Runs, Live)."""
    from . import ops
    from . import project_inputs as PI
    from . import retrain as RT
    from . import trainers as TR
    from . import training_set as TS

    def say(text: str) -> None:
        if live:
            live.log(text)
        if progress:
            progress(text)

    def stage(name: str, i: int) -> None:
        if live:
            live.stage(name, i, 4)

    p = plan(ctx, pid)
    if not p["ready"]:
        raise ValueError(p["text"])
    names = p["names"]
    made_baseline = None
    if not p["champion"]:
        stage("Set keyword search as the baseline", 1)
        b = baseline_start(ctx, pid)
        made_baseline = b["baseline"]
        say(f"No baseline yet, so keyword search was set as the baseline: {made_baseline}.")
    stage("Build the training set", 2)
    tm, tp = TS.build(ctx, names["training"], p["catalog"], events=p["events"], judgments=p["judgments"],
                      queries=p["queries"], holdout=p["holdout"])
    ts_ref = ops.ref(tm)
    s = tp["summary"]
    say(f"Built {ts_ref}: {s['rows']:,} rows over {s['queries']:,} queries, {s['features']} features, labels from "
        f"{s['labels_from']}." + (" SIMULATED." if s["simulated"] else ""))
    for n in tp["notes"]:
        say(n)

    # The untrained version this trains from: keyword order over the project's catalog. One per catalog and field set.
    spec, _ = _catalog_spec(ctx, p["catalog"])
    seed_spec = {"retrieve": _retrieve(spec), "rerank": {"top_k": TOP_K}, "train": {"command": TR.command(TRAINER)}}
    seed = None
    for v in reversed(ctx.registry.versions("pipeline", names["ranker"])):
        pm = ctx.registry.load("pipeline", names["ranker"], v)
        if pm.catalog == p["catalog"] and pm.spec == seed_spec:
            seed = pm
            break
    if seed is None:
        seed = ops.pipeline_publish(ctx, {"name": names["ranker"], "engine": "bm25-ltr", "catalog": p["catalog"],
                                          "spec": seed_spec,
                                          "description": "starter ranker before training: keyword order"})
    stage("Train the starter ranker", 3)
    r = RT.retrain(ctx, ops.ref(seed), local_exec=True, live=live, inputs=[{"ref": ts_ref, "role": "training"}],
                   params=params or None)
    stage("Record the candidate", 4)
    read = PI.run_inputs(ctx, pid)
    for i in read:
        if i.get("role") == "events":
            i["as_of"] = s["train_until"]  # the evaluation replays only searches after this
    read.append({"slot": None, "role": "training", "ref": ts_ref, "mode": "snapshot", "rows": tm.row_count,
                 "fingerprint": PS.dataset_fingerprint(tm), "as_of": s["train_until"] or s["as_of"],
                 "changed_since_pin": False, "read_at": PS._now()})
    exp = PS.experiment_add(ctx, pid, r["pipeline"], training_set=ts_ref, trainer=TRAINER,
                            note="Starter ranker" + (" (simulated events)" if s["simulated"] else ""), inputs_read=read)
    doc = PS.get(ctx, pid)
    return {"project": pid, "experiment": exp["id"], "pipeline": r["pipeline"], "training_set": ts_ref,
            "baseline": doc.get("champion"), "baseline_set": made_baseline, "metrics": r.get("metrics"),
            "summary": s, "notes": tp["notes"], "simulated": s["simulated"],
            "next": {"action": "backtest", "experiment": exp["id"], "text": "Judge it against the baseline."},
            "text": f"Trained {r['pipeline']} on {ts_ref}." + (" The events are simulated, so this is a SIMULATED result."
                                                              if s["simulated"] else "")}
