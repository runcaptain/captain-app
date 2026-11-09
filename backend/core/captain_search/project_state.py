"""A project's working state: the inputs it is pinned to, its experiments, and its champion.

`projects/<id>/state.yaml`, written by compare-and-swap (Store.put_if), the same pattern as projects.yaml:

  pins:         {catalog, events, queries, labels, holdout}: exact refs, name@vN (holdout: name@vN:holdout)
  experiments:  [{id, pipeline, training_set, labels, trainer, backtest, run, job, status, ...}]
  champion:     the pipeline ref the project ships, or null
  registry:     {arch, brand, uri}: where a published version goes (models publish reads it)

Stages (`stages`) come from this doc plus the lineage index only. They never list the registry, so a project page
stays one listing (the edge prefix) however big the bucket is. A newer version of a pinned input is information
for the person, never an error: pins are exact on purpose.

Experiments are judged against the champion on the project's sealed holdout and pinned labels
(`experiment_backtest`). When the training labels and the holdout grades come from the same labeler, a win only
shows the model learned that labeler, so the verdict reads "agrees with its own labeler" (`label_independence`).
"""
from __future__ import annotations

import datetime as dt
import random
import re
import time

import yaml

from .model.entities import new_id
from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

SLOTS = {  # pin slot -> (group on the page, dataset kinds it accepts)
    "catalog": ("data", ("catalog",)),
    "events": ("data", ("events", "logs")),
    "queries": ("data", ("queries",)),
    "labels": ("labels", ("judgments",)),
    "holdout": ("holdout", ("queries",)),
}
VERDICTS = {"win": "beats the champion", "loss": "loses to the champion", "noise": "no clear difference from the champion"}
OWN_LABELER = "agrees with its own labeler"
NOT_ONLINE = "not yet confirmed online"
TRIES = 50


def key(pid: str) -> str:
    return f"projects/{pid}/state.yaml"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _empty(pid: str) -> dict:
    return {"project": pid, "pins": {s: None for s in SLOTS}, "experiments": [], "champion": None,
            "champion_history": [], "registry": {}}


def _check_project(ctx: Context, pid: str) -> None:
    from . import projects as PR

    doc, _ = PR._read(ctx)
    PR._get(doc, pid)


def read(ctx: Context, pid: str) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(key(pid))
        doc = yaml.safe_load(body) or {}
    except NotFound:
        doc, tag = {}, None
    base = _empty(pid)
    for k, v in base.items():
        doc.setdefault(k, v)
    for s in SLOTS:
        doc["pins"].setdefault(s, None)
    return doc, tag


def get(ctx: Context, pid: str) -> dict:
    _check_project(ctx, pid)
    return read(ctx, pid)[0]


def _update(ctx: Context, pid: str, change, dry_run: bool = False):
    """Read, change, write if nobody wrote in between, else read again. Many writers converge; none is lost."""
    _check_project(ctx, pid)
    for i in range(TRIES):
        doc, tag = read(ctx, pid)
        out = change(doc)
        if dry_run:
            return out if out is not None else doc
        try:
            ctx.store.put_if(key(pid), yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            time.sleep(random.uniform(0, 0.005 * (i + 1)))
    raise Conflict(f"project {pid} kept changing while saving. Try again.")


# ---- pins ---------------------------------------------------------------------------------------


def _exact(ctx: Context, slot: str, raw: str) -> str:
    from .ops import check_seal, ref

    m, split = ctx.registry.resolve("dataset", raw)
    kind = getattr(m.dataset_kind, "value", m.dataset_kind)
    ok = SLOTS[slot][1]
    if kind not in ok:
        from . import classes as CL

        if CL.role_of(ctx, m.name) not in ok:
            raise ValueError(f"{ref(m)} holds {kind} rows, and the {slot} pin takes {' or '.join(ok)}")
    if slot == "holdout":
        if not m.holdout_seal:
            raise ValueError(f"{ref(m)} isn't sealed, so it has no holdout yet. Seal it first: that keeps part of its queries back for judging.")
        s = check_seal(ctx, ref(m))
        if not s["ok"]:
            raise ValueError(f"{ref(m)} failed its seal check: {s['detail']}")
        return f"{ref(m)}:holdout"
    if split:
        raise ValueError(f"the {slot} pin takes a whole dataset, not the {split} split")
    return ref(m)


def pin(ctx: Context, pid: str, refs: dict[str, str | None], dry_run: bool = False) -> dict:
    """Pin inputs to exact versions. A value of None or "" unpins that slot."""
    bad = [s for s in refs if s not in SLOTS]
    if bad:
        raise ValueError(f"pins are {', '.join(SLOTS)}, not {', '.join(bad)}")
    exact = {s: (_exact(ctx, s, r) if r else None) for s, r in refs.items()}

    def ch(doc):
        before = dict(doc["pins"])
        doc["pins"].update(exact)
        doc["pins_updated"] = {"at": _now(), "by": whoami()}
        return {"project": pid, "pins": doc["pins"], "changed": {s: {"from": before.get(s), "to": v}
                                                                for s, v in exact.items() if before.get(s) != v},
                "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


def set_registry(ctx: Context, pid: str, arch: str | None = None, brand: str | None = None, uri: str | None = None,
                 dry_run: bool = False) -> dict:
    """Where this project's versions go in a model registry: <uri>/<arch>/<brand>/<version>."""
    for label, v in (("architecture", arch), ("brand", brand)):
        if v is not None and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", v):
            raise ValueError(f"the {label} is lowercase letters, digits, . _ and -")

    def ch(doc):
        reg = doc.setdefault("registry", {}) or {}
        for k, v in (("arch", arch), ("brand", brand), ("uri", uri)):
            if v is not None:
                reg[k] = v
        doc["registry"] = reg
        return {"project": pid, "registry": reg, "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


# ---- experiments --------------------------------------------------------------------------------


def experiment_add(ctx: Context, pid: str, pipeline: str, training_set: str | None = None, labels: str | None = None,
                   trainer: str | None = None, note: str = "", dry_run: bool = False) -> dict:
    """Record a trained pipeline version as an experiment. training_set and labels are what it trained on, when
    known: label independence reads them."""
    from .ops import ref

    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    ts = ref(ctx.registry.resolve("dataset", training_set)[0]) if training_set else None
    lb = ref(ctx.registry.resolve("dataset", labels)[0]) if labels else None
    exp = {"id": "exp-" + new_id().lower()[-10:], "pipeline": ref(pm), "training_set": ts, "labels": lb,
           "trainer": trainer or (pm.tags or {}).get("trainer") or None, "backtest": None, "run": None, "job": None,
           "status": "trained", "note": note, "error": None, "created_at": _now(), "created_by": whoami(),
           "updated_at": None}

    def ch(doc):
        if any(e["pipeline"] == exp["pipeline"] for e in doc["experiments"]):
            raise ValueError(f"{exp['pipeline']} is already an experiment in {pid}")
        doc["experiments"].append(exp)
        return exp

    return _update(ctx, pid, ch, dry_run)


def _exp(doc: dict, eid: str) -> dict:
    e = next((e for e in doc["experiments"] if e["id"] == eid or e["pipeline"] == eid), None)
    if e is None:
        raise NotFound(f"no experiment {eid!r} in {doc['project']}")
    return e


def experiment_record(ctx: Context, pid: str, eid: str, **fields) -> dict:
    allowed = {"backtest", "run", "job", "status", "note", "error"}
    if set(fields) - allowed:
        raise ValueError(f"an experiment records {', '.join(sorted(allowed))}")

    def ch(doc):
        e = _exp(doc, eid)
        e.update(fields)
        e["updated_at"] = _now()
        return dict(e)

    return _update(ctx, pid, ch)


def set_champion(ctx: Context, pid: str, which: str | None, expect: str | None = "", dry_run: bool = False) -> dict:
    """Make an experiment (by id) or a pipeline ref the champion. `expect` is the champion the caller saw: if it
    changed since, nothing is written (pass "" to skip the check). None clears the champion."""
    from .ops import ref

    target = None
    if which:
        doc0, _ = read(ctx, pid)
        hit = next((e for e in doc0["experiments"] if e["id"] == which), None)
        target = hit["pipeline"] if hit else ref(ctx.registry.resolve("pipeline", which)[0])

    def ch(doc):
        if expect != "" and doc.get("champion") != expect:
            raise Conflict(f"the champion changed to {doc.get('champion') or 'none'} since you looked. Check again.")
        prev = doc.get("champion")
        doc["champion"] = target
        doc["champion_history"] = (doc.get("champion_history") or []) + [{"from": prev, "to": target, "at": _now(),
                                                                           "by": whoami()}]
        exp = next((e for e in doc["experiments"] if e["pipeline"] == target), None)
        return {"project": pid, "champion": target, "previous": prev, "experiment": exp["id"] if exp else None,
                "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


# ---- label independence -------------------------------------------------------------------------


def training_labels(ctx: Context, exp: dict) -> str | None:
    """Where an experiment's training labels came from: a dataset ref, a bare source (human, llm, click,
    heuristic), or None when nothing says."""
    if exp.get("labels"):
        return exp["labels"]
    try:
        pm, _ = ctx.registry.resolve("pipeline", exp["pipeline"])
    except Exception:  # noqa: BLE001
        pm = None
    if pm is not None:
        lab = labels_from_inputs(getattr(pm, "train_inputs", None) or [])
        if lab:
            return lab
        if (pm.tags or {}).get("train_labels"):
            return pm.tags["train_labels"]
    if exp.get("training_set"):
        try:
            tm, _ = ctx.registry.resolve("dataset", exp["training_set"])
        except Exception:  # noqa: BLE001
            return None
        params = tm.provenance.params or {}
        for k in ("labels", "judgments"):
            if isinstance(params.get(k), str):
                return params[k]
        if params.get("label_source"):
            return str(params["label_source"])
        if tm.judgment_source:
            return f"{tm.name}@v{tm.version}"
    return None


def labels_from_inputs(inputs: list) -> str | None:
    """The labels in a version's train_inputs: the judgments item, preferring role "labels". Its exact ref when it
    has one, else its label_source (human, llm, clicks, simulated)."""
    items = [i if isinstance(i, dict) else (i.model_dump() if hasattr(i, "model_dump") else {}) for i in inputs]
    labs = [i for i in items if i.get("kind") == "judgments"]
    lab = next((i for i in labs if i.get("role") == "labels"), labs[0] if labs else None)
    if not lab:
        return None
    return lab.get("ref") or lab.get("label_source") or None


# The trainer contract says clicks and simulated, judgment datasets say click and heuristic: one name each.
_SOURCE = {"click": "clicks", "clicks": "clicks", "heuristic": "simulated", "simulated": "simulated", "llm": "llm",
           "human": "human"}


def label_independence(ctx: Context, train: str | None, holdout_judgments: str | None) -> dict:
    """Do the training labels and the holdout grades come from different labelers? status is independent,
    shared or unknown."""
    if not holdout_judgments:
        return {"status": "unknown", "train": train, "holdout": None,
                "text": "The holdout has no judgments, so independence can't be checked."}
    try:
        hm, _ = ctx.registry.resolve("dataset", holdout_judgments)
    except Exception:  # noqa: BLE001
        return {"status": "unknown", "train": train, "holdout": holdout_judgments,
                "text": f"{holdout_judgments} can't be found, so independence can't be checked."}
    href = f"{hm.name}@v{hm.version}"
    hsrc = hm.judgment_source
    out = {"train": train, "holdout": href, "holdout_source": hsrc}
    if not train:
        return {**out, "status": "unknown", "train_source": None,
                "text": "Nothing records where the training labels came from, so independence can't be checked."}
    tm = None
    if "@" in train or ctx.registry.versions("dataset", train.split(":")[0]):
        try:
            tm, _ = ctx.registry.resolve("dataset", train.split(":")[0])
        except Exception:  # noqa: BLE001
            tm = None
    if tm is None:
        tsrc = _SOURCE.get(train.lower())
        if tsrc is None:
            return {**out, "status": "unknown", "train_source": None,
                    "text": f"The training labels ({train}) can't be found, so independence can't be checked."}
        if tsrc == _SOURCE.get(hsrc or "") and tsrc != "human":
            return {**out, "status": "shared", "train_source": tsrc,
                    "text": f"Training labels and holdout grades both come from {tsrc} labeling."}
        return {**out, "status": "independent", "train_source": tsrc,
                "text": f"Training labels come from {tsrc} labeling and the holdout grades from {hsrc or 'another source'}."}
    tref = f"{tm.name}@v{tm.version}"
    tsrc = tm.judgment_source
    out |= {"train": tref, "train_source": tsrc}
    if tm.name == hm.name:
        return {**out, "status": "shared",
                "text": f"The training labels and the holdout grades are the same judgment set ({tm.name})."}
    tj, hj = (tm.judge or {}).get("model"), (hm.judge or {}).get("model")
    if tsrc and tsrc == hsrc and tsrc != "human" and (tsrc == "click" or not tj or not hj or tj == hj):
        who = f"the same {tsrc} judge" + (f" ({tj})" if tj and tsrc != "click" else "")
        return {**out, "status": "shared", "text": f"The training labels and the holdout grades were made by {who}."}
    return {**out, "status": "independent",
            "text": f"Training labels come from {tref} ({tsrc or 'unknown source'}), holdout grades from {href} "
                    f"({hsrc or 'unknown source'})."}


def verdict(comparison_verdict: str | None, independence: dict) -> str | None:
    if comparison_verdict is None:
        return None
    if independence.get("status") == "shared":
        return OWN_LABELER
    return VERDICTS.get(comparison_verdict, comparison_verdict)


# ---- experiments against the champion ------------------------------------------------------------


def experiment_backtest(ctx: Context, pid: str, eid: str, compute: str | None = None, progress=None, live=None,
                        live_id: str | None = None, dry_run: bool = False) -> dict:
    """Backtest an experiment against the champion on the project's sealed holdout and pinned labels, and record
    the run (or the submitted job) on the experiment."""
    from . import ops

    doc = get(ctx, pid)
    exp = _exp(doc, eid)
    champ, pins = doc.get("champion"), doc["pins"]
    if not champ:
        raise ValueError(f"{pid} has no champion yet. Make one first: the first experiment or today's ranker.")
    if champ == exp["pipeline"]:
        raise ValueError(f"{exp['pipeline']} is the champion. Pick another experiment to compare with it.")
    if not pins.get("holdout"):
        raise ValueError(f"{pid} has no sealed holdout pinned. Choose one in Inputs first.")
    if not pins.get("labels"):
        raise ValueError(f"{pid} has no labels pinned, so the holdout can't be scored. Choose labels in Inputs first.")
    train = training_labels(ctx, exp)
    ind = label_independence(ctx, train, pins["labels"])
    name = f"{pid}/{exp['id']}"
    bt = ops.backtest_plan(ctx, name, [champ, exp["pipeline"]], pins["holdout"], pins["labels"], None,
                           hypothesis=f"{exp['pipeline']} ranks the sealed holdout better than the champion {champ}",
                           dry_run=True)
    bt = bt.model_copy(update={"tags": {**(bt.tags or {}), "project": pid, "experiment": exp["id"],
                                        "train_labels": ind.get("train") or "", "label_independence": ind["status"]}})
    plan = {"project": pid, "experiment": exp["id"], "backtest": name, "arms": bt.arms, "queries": bt.queries,
            "judgments": bt.judgments, "label_independence": ind}
    if dry_run:
        return {**plan, "dry_run": True}
    bt = ctx.registry.publish(type(bt).model_validate(bt.model_dump(mode="json")))
    experiment_record(ctx, pid, exp["id"], backtest=ops.ref(bt), status="backtesting", run=None, job=None, error=None)
    try:
        r = ops.backtest_launch(ctx, name, compute, progress=progress, live=live, live_id=live_id)
    except Exception as e:
        experiment_record(ctx, pid, exp["id"], status="failed", error=str(e)[:500])
        raise
    if r.get("run"):
        experiment_record(ctx, pid, exp["id"], run=r["run"], status="evaluated")
    else:
        experiment_record(ctx, pid, exp["id"], job=r.get("job"), status="backtesting")
    return {**plan, "backtest": ops.ref(bt), **{k: r.get(k) for k in ("run", "job", "compute", "live", "detail")}}


def _evaluation(ctx: Context, exp: dict, champion: str | None, index=None) -> dict | None:
    """The experiment's row numbers from its recorded run. Loads by exact version: no listing."""
    run_ref = exp.get("run")
    if not run_ref and exp.get("backtest") and index is not None:
        bname = exp["backtest"].split("@v")[0]
        vs = index.runs_of(bname)
        if vs:
            run_ref = f"{bname}@v{vs[-1]}"
    if not run_ref:
        return None
    name, _, v = run_ref.partition("@v")
    try:
        run = ctx.registry.load("backtest-run", name, int(v))
        bname, _, bv = run.backtest.partition("@v")
        bt = ctx.registry.load("backtest", bname, int(bv) if bv else None)
    except Exception as e:  # noqa: BLE001
        return {"run": run_ref, "error": f"the run can't be read: {e}"}
    by = {a.arm: a for a in run.arms}
    me, base = by.get(exp["pipeline"]), by.get(bt.arms[0])
    primary = bt.preregistration.primary_metric
    comp = next((c for c in run.comparisons if c.get("b") == exp["pipeline"] and c.get("metric") == primary), None)
    ind = label_independence(ctx, (bt.tags or {}).get("train_labels") or training_labels(ctx, exp), bt.judgments)
    rec = next((k for k in (me.offline if me else {}) if k.startswith("r@") or k.startswith("recall")), None)
    return {
        "run": f"{run.name}@v{run.version}", "against": bt.arms[0], "against_is_champion": bt.arms[0] == champion,
        "metric": primary,
        "value": (me.offline.get(primary) if me else None),
        "champion_value": (base.offline.get(primary) if base else None),
        "delta": comp.get("delta") if comp else None,
        "ci_low": comp.get("ci_low") if comp else None, "ci_high": comp.get("ci_high") if comp else None,
        "p": comp.get("p") if comp else None,
        "comparison": comp.get("verdict") if comp else None,
        "recall": {"metric": rec, "value": me.offline.get(rec), "champion_value": base.offline.get(rec) if base else None}
        if rec and me else None,
        "judged_at_10": me.judged_at_10 if me else None,
        "evidence": me.evidence.value if me else None,
        "label_independence": ind,
        "verdict": verdict(comp.get("verdict") if comp else None, ind),
        "online": NOT_ONLINE,
        "click_grades": bt.mode == "historic" or (ind.get("holdout_source") == "click"),
    }


def experiments(ctx: Context, pid: str) -> dict:
    from . import lineage_index as LI

    doc = get(ctx, pid)
    index = LI.Index(ctx.store)
    rows = []
    for e in doc["experiments"]:
        ev = _evaluation(ctx, e, doc.get("champion"), index)
        rows.append({**e, "is_champion": e["pipeline"] == doc.get("champion"), "evaluation": ev})
    return {"project": pid, "champion": doc.get("champion"), "pins": doc["pins"], "experiments": rows}


# ---- stages -------------------------------------------------------------------------------------


def stages(ctx: Context, pid: str) -> dict:
    """Where the project stands, from the pin doc and the lineage index only (one listing: the edge prefix)."""
    from . import lineage_index as LI

    doc = get(ctx, pid)
    idx = LI.Index(ctx.store)
    pins = doc["pins"]
    pinned_names = {}
    for s, r in pins.items():
        if r:
            n, _, v = r.split(":")[0].partition("@v")
            pinned_names.setdefault(n, []).append((s, int(v)))
    inputs = []
    for slot, (group, kinds) in SLOTS.items():
        r = pins.get(slot)
        row = {"slot": slot, "group": group, "accepts": list(kinds), "pinned": r, "status": "pinned" if r else "missing",
               "newest": None, "info": [], "next": None}
        if not r:
            row["next"] = {"action": "pin", "slot": slot}
            inputs.append(row)
            continue
        name, _, v = r.split(":")[0].partition("@v")
        v = int(v)
        newest = idx.newest("dataset", name)
        row["newest"] = f"{name}@v{newest}" if newest else r.split(":")[0]
        if newest and newest > v:
            row["info"].append({"code": "newer-version", "ref": f"{name}@v{newest}",
                                "text": f"Newer version available: {name}@v{newest}."})
            row["next"] = {"action": "pin", "slot": slot, "ref": f"{name}@v{newest}" + (":holdout" if slot == "holdout" else "")}
        for rec in idx.upstream(f"dataset:{name}@v{v}"):
            m = re.match(r"^dataset:(.+)@v(\d+)$", rec["from"] or "")
            if not m or m.group(1) == name:
                continue
            for other, ov in pinned_names.get(m.group(1), []):
                if ov != int(m.group(2)):
                    row["info"].append({"code": "made-from-other-version", "ref": f"{m.group(1)}@v{m.group(2)}",
                                        "text": f"Made from {m.group(1)}@v{m.group(2)}, but the {other} pin is "
                                                f"{m.group(1)}@v{ov}."})
        inputs.append(row)
    exps = doc["experiments"]
    champ = doc.get("champion")
    pending = [e for e in exps if e.get("status") in ("trained", "failed") and e["pipeline"] != champ]
    if not exps:
        nxt = {"action": "new-experiment"}
    elif not champ:
        nxt = {"action": "make-champion", "experiment": exps[0]["id"]}
    elif pending:
        nxt = {"action": "backtest", "experiment": pending[0]["id"]}
    else:
        nxt = {"action": "new-experiment"}
    by_status = {}
    for e in exps:
        by_status[e.get("status", "trained")] = by_status.get(e.get("status", "trained"), 0) + 1
    champ_exp = next((e for e in exps if e["pipeline"] == champ), None)
    newest_champ = None
    if champ:
        cn, _, cv = champ.partition("@v")
        nv = idx.newest("pipeline", cn)
        if nv and nv > int(cv):
            newest_champ = f"{cn}@v{nv}"
    reg = doc.get("registry") or {}
    notes = [] if idx.complete else [{"code": "index-incomplete",
                                      "text": "Lineage for older versions isn't indexed yet, so newer versions may be "
                                              "missing here. Run `cst lineage reindex` once."}]
    return {
        "project": pid, "index_complete": idx.complete, "notes": notes,
        "inputs": inputs,
        "experiments": {"count": len(exps), "by_status": by_status, "next": nxt},
        "champion": {"pinned": champ, "experiment": champ_exp["id"] if champ_exp else None,
                     "info": [{"code": "newer-version", "ref": newest_champ,
                               "text": f"Newer version available: {newest_champ}."}] if newest_champ else []},
        "ship": {"registry": {k: reg.get(k) for k in ("arch", "brand", "uri")},
                 "ready": bool(champ and reg.get("arch") and reg.get("brand")), "online": NOT_ONLINE},
    }
