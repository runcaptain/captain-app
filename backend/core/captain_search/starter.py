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
    out["size"] = _size(ctx, catalog, events, labels)
    return {**out, "ready": True,
            "text": "Learns to rank from your " + ("clicks" if out["labels_from"] == "clicks" else "judgments")
                    + ". Keyword search finds the candidates and the model reorders the top "
                    + f"{TOP_K}."}


def _size(ctx: Context, catalog: str | None, events: str | None, labels: str | None) -> dict:
    """What decides how long training takes, for the cost estimate: the rows it reads."""
    def rows(ref_):
        try:
            return int(ctx.registry.resolve("dataset", ref_)[0].row_count or 0) if ref_ else 0
        except Exception:  # noqa: BLE001
            return 0

    return {"how": "starter", "rows": rows(events) or rows(labels), "catalog_rows": rows(catalog)}


CLOUD_WORKSPACE = ("Cloud training needs a cloud workspace. This workspace is kept on this computer's disk, and a "
                   "cloud machine can't read it. Make the workspace in a bucket (s3://, gs:// or az://), or train on "
                   "this computer.")


def train_on(ctx: Context, pid: str, compute: str, params: dict | None = None, dry_run: bool = False,
             live_id: str | None = None, wait: bool = False, wait_timeout_s: float | None = None) -> dict:
    """Train the starter ranker as a job on a compute target. The job runs the same `projects starter-train` in the
    runner image: it reads the inputs from the workspace bucket, builds the training set, trains, and records the
    candidate there. Nothing large comes to this computer. Returns at once with {pending: true, job, live_id}: the
    candidate does not exist until the job finishes. `wait` polls the job and returns what it recorded."""
    import json

    from . import compute as C
    from . import cost_estimates as CE
    from . import live as LV

    target = C.get(ctx, compute)
    if target["kind"] == "local":
        raise ValueError("local is this computer. Leave the compute target out to train here.")
    if ctx.workspace.storage_uri.split("://")[0] not in ("s3", "gs", "gcs", "az", "azure"):
        raise ValueError(CLOUD_WORKSPACE)
    p = plan(ctx, pid)
    if not p["ready"]:
        raise ValueError(p["text"])
    args = ["projects", "starter-train", pid]
    for k, v in (params or {}).items():
        args += ["--param", f"{k}={json.dumps(v)}"]
    est = CE.estimate(ctx, "training", target, p["size"])
    plan_, job = C.plan_and_job(ctx, target, args, label=f"starter-{pid}"[:60])
    if dry_run:
        return {"pending": False, "dry_run": True, "project": pid, "compute": target["name"], "plan": plan_,
                "estimate": est, "size": p["size"]}
    live_id = live_id or LV.start_remote(ctx, "training", f"Starter ranker for {pid}", of=pid, where=target["name"])
    job.env["CST_LIVE_ID"] = live_id
    try:
        rec = C.submit_job(ctx, target, job.argv, label=job.label, kind="starter-train", job=job,
                           meta={"project": pid, "cost_kind": "training", "size": p["size"]})
    except Exception as e:
        LV.note_remote(ctx, live_id, status="failed", error=f"Couldn't submit the job: {e}")
        raise
    LV.note_remote(ctx, live_id, job=rec["id"])
    CE.note_estimate(ctx, rec["id"], est, kind="training", size=p["size"], project=pid,
                     title=f"Starter ranker for {pid}", target=target)
    from . import project_state as PS_

    PS_.record_change(ctx, pid, {"by": _who(), "what": f"started training a starter ranker on {target['name']}",
                                 "job": rec["id"], "estimate": est.get("text")})
    if wait:
        return {**wait_job(ctx, pid, rec["id"], wait_timeout_s), "live_id": live_id, "estimate": est}
    return {"pending": True, "project": pid, "job": rec["id"], "live_id": live_id, "compute": target["name"],
            "estimate": est, "simulated": p["simulated"],
            "submitted": {"provider_job": C.provider_job(rec["handle"]), "job": rec["id"]},
            "text": f"Training on {target['name']}. The candidate joins the project when the job finishes. "
                    f"{est.get('text') or ''}".strip(),
            "detail": f"Follow it with `cst job show {rec['id']}`."}


def _who() -> str:
    from .workspace import whoami

    return whoami()


def wait_job(ctx: Context, pid: str, job_id: str, timeout_s: float | None = None) -> dict:
    """Poll a starter-train job until it finishes and return the candidate it recorded, with what it cost. Fails if
    the job failed, or finished without recording a candidate."""
    import time

    from . import compute_providers as P
    from . import jobs as J
    from . import retrain as RT

    t0 = time.time()
    while True:
        r = J.show(ctx, job_id)
        if r["state"] in P.TERMINAL:
            break
        if timeout_s is not None and time.time() - t0 > timeout_s:
            raise TimeoutError(f"job {job_id} is still {r['state']} after {timeout_s:.0f} s. "
                               f"Follow it with `cst job show {job_id} --wait`.")
        time.sleep(RT.POLL_S)
    if r["state"] != "succeeded":
        raise ValueError(f"the training job {job_id} {r['state']}: {r.get('reason') or 'no reason given'}")
    res = r.get("result")
    if not res or not res.get("pipeline"):
        raise ValueError(f"the training job {job_id} finished but recorded no candidate. Check `cst job logs {job_id}`.")
    settle(ctx, pid, r)
    return {"pending": False, "job": job_id, "compute": r.get("target"), "cost": r.get("cost"), **res}


def settle(ctx: Context, pid: str, job: dict) -> None:
    """Put a finished training job's cost on its candidate and in the project's history, once."""
    from . import project_state as PS_

    cost, res = job.get("cost"), job.get("result") or {}
    if not cost or not res.get("experiment"):
        return
    try:
        exp = PS_._exp(PS_.get(ctx, pid), res["experiment"])
    except Exception:  # noqa: BLE001  the candidate was removed meanwhile
        return
    if exp.get("cost"):
        return
    PS_.experiment_record(ctx, pid, res["experiment"], cost=cost, train_job=job["id"])
    PS_.record_change(ctx, pid, {"by": job.get("submitted_by") or _who(),
                                 "what": f"trained {res.get('pipeline')} on {job.get('target')}", "job": job["id"],
                                 "cost": cost.get("text")})


def settle_all(ctx: Context, pid: str) -> int:
    """Ask after this project's training jobs that were still running, and settle the ones that finished. The
    project page calls this, so a candidate trained on a cloud gets its cost without anyone waiting on the job."""
    from . import compute_providers as P
    from . import jobs as J

    n = 0
    for rec in J.listing(ctx, limit=100):
        if rec.get("kind") != "starter-train" or (rec.get("meta") or {}).get("project") != pid or rec.get("settled"):
            continue
        rec = J.show(ctx, rec["id"])
        if rec["state"] not in P.TERMINAL:
            continue
        settle(ctx, pid, rec)
        J._save(ctx, {**{k: v for k, v in rec.items() if k not in ("result", "run_seconds")}, "settled": True})
        n += 1
    return n


def train(ctx: Context, pid: str, live=None, params: dict | None = None, progress=None) -> dict:
    """Set the keyword baseline if there is none, build the training set, train the starter ranker, and record it
    as a candidate. `live` is the live run its progress goes to (Runs, Live)."""
    import os
    import time

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

    t0 = time.time()
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
    size = {**p["size"], "training_rows": s["rows"], "queries": s["queries"]}
    job_id = os.environ.get("CST_JOB_ID")
    cost = None
    if job_id:
        # In a submitted job: say what was made and how big it was. Whoever submitted it reads this and prices the
        # run from the provider's own start and stop times.
        from . import jobs as J

        J.save_result(ctx, job_id, {"experiment": exp["id"], "pipeline": r["pipeline"], "training_set": ts_ref,
                                    "baseline": doc.get("champion"), "baseline_set": made_baseline,
                                    "metrics": r.get("metrics"), "simulated": s["simulated"], "size": size,
                                    "wall_s": round(time.time() - t0, 1)})
    else:
        # On this computer: free, and its time is kept so later estimates start from a real run.
        try:
            from . import compute as C
            from . import cost_estimates as CE

            cost = CE.note_actual(ctx, CE.new_run_id(), C.get(ctx, "local"), time.time() - t0, kind="training",
                                  size=size, project=pid, title=f"Starter ranker for {pid}")
            PS.experiment_record(ctx, pid, exp["id"], cost=cost)
        except Exception:  # noqa: BLE001  the candidate stands without its timing
            cost = None
    return {"project": pid, "experiment": exp["id"], "pipeline": r["pipeline"], "training_set": ts_ref, "cost": cost,
            "baseline": doc.get("champion"), "baseline_set": made_baseline, "metrics": r.get("metrics"),
            "summary": s, "notes": tp["notes"], "simulated": s["simulated"],
            "next": {"action": "backtest", "experiment": exp["id"], "text": "Judge it against the baseline."},
            "text": f"Trained {r['pipeline']} on {ts_ref}." + (" The events are simulated, so this is a SIMULATED result."
                                                              if s["simulated"] else "")}
