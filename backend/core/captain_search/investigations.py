"""Investigations: a question, its evidence, the runs that test it, and the decision it reached.

Spec stories S1, S3, S6 and S7 (2026-11-06). The investigation is the main object. Everything else (datasets,
signals, runs, slices, queries) is evidence attached to one.

  state     draft, running, ready_for_review, decided. A failed or cancelled run stays visible
  revisions the hypothesis and plan, append-only. A run pins the revision it used, so revising never rewrites
            what an earlier run was testing
  initiator who opened it, and whether an agent did (CST_AGENT or --agent)
  evidence  typed references: dataset, run, slice, query, signal, note
  decision  write-once: outcome (continue, online-test, reject, inconclusive), claim, interpretation,
            limitations, next step. A claim of online or profit lift needs REAL_TRAFFIC evidence

Agents create, revise and run investigations within the execution policy (policies/execution.yaml). People decide.
Missing information is explicit: an investigation can exist before its plan is complete, and running checks only what
the run needs.

  investigations/<id>.yaml   the investigation (compare-and-swap, with history)
  decisions/<id>.yaml        decisions (write-once)
  signals/<id>.yaml          candidate signals (compare-and-swap, with history)
"""
from __future__ import annotations

import datetime as dt

import yaml

from .model.entities import Evidence, new_id
from .proposals import agent_name
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

STATES = ("draft", "running", "ready_for_review", "decided")
OUTCOMES = ("continue", "online-test", "reject", "inconclusive")
CLAIMS = ("offline-improvement", "offline-regression", "no-detectable-change", "online-lift", "none")
EVIDENCE_KINDS = ("dataset", "run", "slice", "query", "signal", "note", "label", "replay", "handoff")
PLAN_FIELDS = ("baseline", "candidates", "queries", "judgments", "population", "primary_metric", "mde", "guardrails")
POLICY_KEY = "policies/execution.yaml"
DEFAULT_POLICY = {"agent_runs": True, "max_queries": 20000, "max_shoppers": 20000, "compute": ["local"], "max_runs_per_day": 50}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _key(iid: str) -> str:
    return f"investigations/{iid}.yaml"


def _load(ctx: Context, iid: str):
    try:
        data, tag = ctx.store.get_with_tag(_key(iid))
    except NotFound:
        raise NotFound(f"no investigation {iid}") from None
    return yaml.safe_load(data), tag


def _save(ctx: Context, doc: dict, tag) -> dict:
    try:
        ctx.store.put_if(_key(doc["id"]), dump_yaml(doc), tag)
    except Conflict:
        raise Conflict(f"investigation {doc['id']} changed while you were editing it (another person or agent). "
                       "Reload it and apply your change again.") from None
    return doc


def _actor(agent: str | None = None) -> dict:
    return {"by": whoami(), "agent": agent_name(agent), "at": _now()}


def _event(doc: dict, what: str, agent: str | None = None, **extra) -> None:
    doc.setdefault("history", []).append({**_actor(agent), "event": what, **extra})


def current_plan(doc: dict) -> dict:
    return doc["revisions"][-1] if doc["revisions"] else {}


def missing(plan: dict) -> list[str]:
    need = [f for f in ("hypothesis", "baseline", "candidates", "queries", "primary_metric") if not plan.get(f)]
    if plan.get("primary_metric", "").startswith(("ndcg", "p@", "r@", "mrr")) and not plan.get("judgments"):
        need.append("judgments (the primary metric needs graded judgments)")
    return need


def create(ctx: Context, title: str, hypothesis: str = "", agent: str | None = None, dry_run: bool = False,
           expected_revision: int | None = None, **plan) -> dict:
    if not title.strip():
        raise ValueError("an investigation needs a title (the question)")
    unknown = set(plan) - set(PLAN_FIELDS)
    if unknown:
        raise ValueError(f"unknown plan fields {sorted(unknown)}: use {', '.join(PLAN_FIELDS)}")
    rev = _revision(ctx, 1, hypothesis, plan, agent, note="opened")
    doc = {"id": new_id().lower(), "title": title.strip(), "state": "draft", "initiator": _actor(agent),
           "revisions": [rev], "evidence": [], "runs": [], "signals": [], "decision": None, "history": []}
    _event(doc, "opened", agent)
    if dry_run:
        return doc | {"missing": missing(rev)}
    ctx.store.put_new(_key(doc["id"]), dump_yaml(doc))
    return doc | {"missing": missing(rev)}


def _revision(ctx: Context, n: int, hypothesis: str, plan: dict, agent: str | None, note: str = "") -> dict:
    clean = {k: v for k, v in plan.items() if v not in (None, "", [])}
    if isinstance(clean.get("candidates"), str):
        clean["candidates"] = [c.strip() for c in clean["candidates"].split(",") if c.strip()]
    for k in ("baseline",):
        if clean.get(k):
            pm, _ = ctx.registry.resolve("pipeline", clean[k])
            clean[k] = f"{pm.name}@v{pm.version}"
    if clean.get("candidates"):
        pinned = []
        for c in clean["candidates"]:
            pm, _ = ctx.registry.resolve("pipeline", c)
            pinned.append(f"{pm.name}@v{pm.version}")
        clean["candidates"] = pinned
    for k in ("queries", "judgments"):
        if clean.get(k):
            ctx.registry.resolve("dataset", clean[k])
    clean.setdefault("primary_metric", "ndcg@10")
    return {"rev": n, "hypothesis": hypothesis.strip(), **clean, "note": note, **_actor(agent)}


def get(ctx: Context, iid: str) -> dict:
    doc, _ = _load(ctx, iid)
    plan = current_plan(doc)
    return doc | {"missing": missing(plan), "plan": plan}


def listing(ctx: Context, state: str | None = None, q: str | None = None) -> list[dict]:
    out = []
    for k in ctx.store.list("investigations/"):
        if not k.endswith(".yaml"):
            continue
        d = yaml.safe_load(ctx.store.get(k))
        if state and d["state"] != state:
            continue
        plan = current_plan(d)
        if q:
            hay = " ".join([d["title"], plan.get("hypothesis", ""), " ".join(str(e.get("ref", "")) + " " + str(e.get("note", ""))
                            for e in d["evidence"]), " ".join(d["signals"]), str((d.get("decision") or {}).get("outcome", ""))]).lower()
            if not all(w in hay for w in q.lower().split()):
                continue
        last = d["runs"][-1] if d["runs"] else None
        out.append({"id": d["id"], "title": d["title"], "state": d["state"], "hypothesis": plan.get("hypothesis", ""),
                    "initiator": d["initiator"], "revision": plan.get("rev"), "runs": len(d["runs"]), "latest_run": last,
                    "decision": d.get("decision"), "updated_at": d["history"][-1]["at"] if d.get("history") else None,
                    "missing": missing(plan)})
    return sorted(out, key=lambda x: x["updated_at"] or "", reverse=True)


def revise(ctx: Context, iid: str, hypothesis: str | None = None, note: str = "", agent: str | None = None,
           expected_revision: int | None = None, **plan) -> dict:
    doc, tag = _load(ctx, iid)
    if doc["state"] == "decided":
        raise ValueError(f"investigation {iid} is decided. Open a new investigation that links to it to keep going.")
    cur = current_plan(doc)
    if expected_revision is not None and cur.get("rev") != expected_revision:
        raise Conflict(f"investigation {iid} is at revision {cur.get('rev')}, not {expected_revision}. Reload and revise again.")
    merged = {k: cur.get(k) for k in PLAN_FIELDS} | {k: v for k, v in plan.items() if v is not None}
    rev = _revision(ctx, cur.get("rev", 0) + 1, hypothesis if hypothesis is not None else cur.get("hypothesis", ""), merged,
                    agent, note=note)
    doc["revisions"].append(rev)
    _event(doc, "revised", agent, rev=rev["rev"], note=note)
    return get(ctx, _save(ctx, doc, tag)["id"])


def add_evidence(ctx: Context, iid: str, kind: str, ref: str, note: str = "", agent: str | None = None) -> dict:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"evidence kind is one of {', '.join(EVIDENCE_KINDS)}")
    if kind == "dataset":
        ctx.registry.resolve("dataset", ref)
    elif kind == "run":
        name, _, v = ref.partition("@v")
        ctx.registry.load("backtest-run", name, int(v) if v else None)
    elif kind == "signal":
        signal_get(ctx, ref)
    doc, tag = _load(ctx, iid)
    doc["evidence"].append({"kind": kind, "ref": ref, "note": note, **_actor(agent)})
    if kind == "signal" and ref not in doc["signals"]:
        doc["signals"].append(ref)
    _event(doc, "evidence", agent, kind=kind, ref=ref)
    return get(ctx, _save(ctx, doc, tag)["id"])


# ---- execution policy -----------------------------------------------------------------------


def policy(ctx: Context) -> dict:
    try:
        return DEFAULT_POLICY | (yaml.safe_load(ctx.store.get(POLICY_KEY)) or {})
    except NotFound:
        return dict(DEFAULT_POLICY)


def set_policy(ctx: Context, values: dict, agent: str | None = None) -> dict:
    if agent_name(agent):
        raise ValueError("an agent session cannot change the execution policy. A person sets it.")
    bad = set(values) - set(DEFAULT_POLICY)
    if bad:
        raise ValueError(f"unknown policy fields {sorted(bad)}: use {', '.join(DEFAULT_POLICY)}")
    doc = policy(ctx) | values
    try:
        _, tag = ctx.store.get_with_tag(POLICY_KEY)
    except NotFound:
        tag = None
    ctx.store.put_if(POLICY_KEY, dump_yaml(doc), tag)
    return doc


def _check_policy(ctx: Context, doc: dict, plan: dict, agent: str | None) -> None:
    from .ops import rows_of

    pol = policy(ctx)
    who = agent_name(agent)
    if who and not pol["agent_runs"]:
        raise ValueError("the execution policy does not let agents start runs. A person can, or can change the policy.")
    _, qs, _ = rows_of(ctx, plan["queries"])
    if len(qs) > pol["max_queries"]:
        raise ValueError(f"{len(qs):,} queries is over the policy limit of {pol['max_queries']:,}")
    today = _now()[:10]
    n_today = sum(1 for r in doc["runs"] if str(r.get("at", "")).startswith(today))
    if n_today >= pol["max_runs_per_day"]:
        raise ValueError(f"this investigation already started {n_today} runs today, the policy limit")


# ---- runs -----------------------------------------------------------------------------------


def run(ctx: Context, iid: str, agent: str | None = None, idempotency_key: str | None = None, progress=None) -> dict:
    """Plan and run the comparison the current revision describes. The run pins the revision."""
    from . import ops

    doc, tag = _load(ctx, iid)
    if doc["state"] == "decided":
        raise ValueError(f"investigation {iid} is decided. Open a new one to run more.")
    plan = current_plan(doc)
    need = missing(plan)
    if need:
        raise ValueError("this revision can't run yet. Missing: " + "; ".join(need))
    if idempotency_key:
        prior = next((r for r in doc["runs"] if r.get("idempotency_key") == idempotency_key), None)
        if prior:
            return get(ctx, iid) | {"reused": prior}
    _check_policy(ctx, doc, plan, agent)
    name = f"investigations/{iid[:12]}-r{plan['rev']}"
    arms = [plan["baseline"], *plan["candidates"]]
    entry = {"run": None, "rev": plan["rev"], "status": "running", "idempotency_key": idempotency_key, **_actor(agent)}
    doc["runs"].append(entry)
    doc["state"] = "running"
    _event(doc, "run started", agent, rev=plan["rev"])
    doc = _save(ctx, doc, tag)
    try:
        sf = [{"name": g["surface"], "queries": g["queries"], "judgments": g["judgments"], "guardrails": g.get("rules", [])}
              for g in plan.get("guardrails", []) if isinstance(g, dict) and g.get("surface")]
        bt = ops.backtest_plan(ctx, name, arms, plan["queries"], plan.get("judgments"), plan.get("population"),
                               f"{plan['hypothesis']} (investigation {iid}, revision {plan['rev']})",
                               plan.get("primary_metric", "ndcg@10"), float(plan.get("mde") or 0.003), surfaces=sf or None)
        r = ops.backtest_run(ctx, bt.name, progress=progress)
        status, ref, err = "done", f"{r.name}@v{r.version}", None
    except Exception as e:  # noqa: BLE001  a failed run stays visible in the investigation
        status, ref, err = "failed", None, str(e)[:500]
    doc, tag = _load(ctx, iid)
    for x in doc["runs"]:
        if x is not None and x.get("status") == "running" and x.get("rev") == plan["rev"] and x.get("at") == entry["at"]:
            x.update(status=status, run=ref, error=err, finished_at=_now())
    if not any(x.get("status") == "running" for x in doc["runs"]):
        doc["state"] = "ready_for_review" if any(x.get("status") == "done" for x in doc["runs"]) else "draft"
    _event(doc, f"run {status}", agent, run=ref, error=err)
    out = get(ctx, _save(ctx, doc, tag)["id"])
    if status == "failed":
        raise ValueError(f"the run failed and is recorded on the investigation: {err}")
    return out


# ---- decisions --------------------------------------------------------------------------------


def decide(ctx: Context, iid: str, outcome: str, interpretation: str, limitations: list[str], next_step: str,
           runs: list[str] | None = None, slices: list[str] | None = None, claim: str = "none",
           agent: str | None = None) -> dict:
    if agent_name(agent):
        raise ValueError("an agent session cannot record the decision. Agents investigate. A person decides, in the app or "
                         "from a session without CST_AGENT.")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome is one of {', '.join(OUTCOMES)}")
    if claim not in CLAIMS:
        raise ValueError(f"claim is one of {', '.join(CLAIMS)}")
    if not interpretation.strip() or not next_step.strip():
        raise ValueError("a decision needs an interpretation and a next step, so someone else can assess it")
    doc, tag = _load(ctx, iid)
    if doc["state"] == "decided":
        raise ValueError(f"investigation {iid} is already decided ({doc['decision']['outcome']})")
    runs = runs or [r["run"] for r in doc["runs"] if r.get("run")]
    labels = []
    for ref in runs:
        name, _, v = ref.partition("@v")
        rr = ctx.registry.load("backtest-run", name, int(v) if v else None)
        labels += [a.evidence for a in rr.arms] + [a.simulated_evidence for a in rr.arms if a.simulated]
    strongest = max(labels, key=lambda e: e.rank) if labels else None
    if claim == "online-lift" and (strongest is None or strongest.rank < Evidence.REAL_TRAFFIC.rank):
        raise ValueError("an online-lift claim needs REAL_TRAFFIC evidence. These runs are offline "
                         f"({strongest.value if strongest else 'none'}). Use offline-improvement, or attach the online result first.")
    if outcome != "inconclusive" and not runs:
        raise ValueError("a decision other than inconclusive needs at least one run as evidence")
    d = {"id": new_id().lower(), "investigation": iid, "title": doc["title"], "revision": current_plan(doc).get("rev"),
         "hypothesis": current_plan(doc).get("hypothesis"), "outcome": outcome, "claim": claim,
         "evidence_label": strongest.value if strongest else None, "interpretation": interpretation.strip(),
         "limitations": [x for x in limitations if x.strip()], "next_step": next_step.strip(), "runs": runs,
         "slices": slices or [], "signals": doc["signals"], **_actor(None)}
    if strongest is not None and strongest.rank < Evidence.REAL_TRAFFIC.rank:
        d["limitations"].append(f"offline evidence ({strongest.value}): not proof of online or profit lift")
    ctx.store.put_new(f"decisions/{d['id']}.yaml", dump_yaml(d))
    doc["decision"] = {"id": d["id"], "outcome": outcome, "claim": claim, "at": d["at"], "by": d["by"]}
    doc["state"] = "decided"
    _event(doc, "decided", None, outcome=outcome)
    _save(ctx, doc, tag)
    return d


def decision_get(ctx: Context, did: str) -> dict:
    try:
        return yaml.safe_load(ctx.store.get(f"decisions/{did}.yaml"))
    except NotFound:
        raise NotFound(f"no decision {did}") from None


def export(ctx: Context, iid: str) -> dict:
    """Everything an outside reader needs: the investigation, its decision, and its runs' manifests and results."""
    doc = get(ctx, iid)
    runs = []
    for r in doc["runs"]:
        if r.get("run"):
            name, _, v = r["run"].partition("@v")
            runs.append(ctx.registry.load("backtest-run", name, int(v)).model_dump(mode="json"))
    sig = [signal_get(ctx, s) for s in doc["signals"]]
    dec = decision_get(ctx, doc["decision"]["id"]) if doc.get("decision") else None
    return {"format": "captain.investigation/v1", "investigation": doc, "decision": dec, "runs": runs, "signals": sig}


# ---- signals ----------------------------------------------------------------------------------


def signal_create(ctx: Context, name: str, source: str, definition: str, rationale: str, available_at: str = "",
                  investigation: str | None = None, agent: str | None = None) -> dict:
    if not name.strip() or not source.strip() or not definition.strip():
        raise ValueError("a signal needs a name, a source and a definition (SQL, a label ref, or a code path)")
    s = {"id": new_id().lower(), "name": name.strip(), "source": source.strip(), "definition": definition.strip(),
         "rationale": rationale.strip(), "available_at": available_at.strip() or None,
         "availability": {"status": "unchecked", "note": "historical availability has not been checked"},
         "leakage": {"status": "unchecked", "note": "possible leakage has not been checked"},
         "evidence": [], **_actor(agent), "history": []}
    ctx.store.put_new(f"signals/{s['id']}.yaml", dump_yaml(s))
    if investigation:
        add_evidence(ctx, investigation, "signal", s["id"], f"candidate signal {s['name']}", agent)
    return s


def signal_get(ctx: Context, sid: str) -> dict:
    try:
        return yaml.safe_load(ctx.store.get(f"signals/{sid}.yaml"))
    except NotFound:
        raise NotFound(f"no signal {sid}") from None


def signal_check(ctx: Context, sid: str, what: str, status: str, note: str, agent: str | None = None) -> dict:
    """Record the result of an availability or leakage check. Only a recorded check changes 'unchecked'."""
    if what not in ("availability", "leakage"):
        raise ValueError("check is availability or leakage")
    if status not in ("ok", "problem", "unchecked"):
        raise ValueError("status is ok, problem or unchecked")
    if status != "unchecked" and not note.strip():
        raise ValueError("say how it was checked, so the result can be trusted")
    try:
        data, tag = ctx.store.get_with_tag(f"signals/{sid}.yaml")
    except NotFound:
        raise NotFound(f"no signal {sid}") from None
    s = yaml.safe_load(data)
    s["history"].append({**_actor(agent), "check": what, "from": s[what]["status"], "to": status, "note": note})
    s[what] = {"status": status, "note": note, **_actor(agent)}
    ctx.store.put_if(f"signals/{sid}.yaml", dump_yaml(s), tag)
    return s


def signals(ctx: Context) -> list[dict]:
    out = [yaml.safe_load(ctx.store.get(k)) for k in ctx.store.list("signals/") if k.endswith(".yaml")]
    return sorted(out, key=lambda s: s["at"], reverse=True)


# ---- copy prompt ------------------------------------------------------------------------------


def prompt(ctx: Context, iid: str, ask: str = "investigate", base_url: str | None = None) -> str:
    """An editable handoff for any coding agent, built from real context. Unknowns are marked, secrets never included."""
    doc = get(ctx, iid)
    p = doc["plan"]
    pol = policy(ctx)
    unk = lambda v: v if v not in (None, "", []) else "UNKNOWN (find it first, don't guess)"
    runs = [r["run"] for r in doc["runs"] if r.get("run")]
    asks = {
        "investigate": "Inspect the failures behind this question, document candidate signals with their historical "
                       "availability, and propose the next comparison.",
        "run": "Run the comparison this investigation's current revision describes, then summarise what changed, where it "
               "helped or hurt, and whether the comparison can be trusted.",
        "panel": "Build a custom panel (a Captain app mod) that shows the evidence for this investigation.",
        "explain": "Explain the latest run's regressions at query and slice level.",
    }
    if ask not in asks:
        raise ValueError(f"ask is one of {', '.join(asks)}")
    from .codeagent import cst_line

    lines = [
        cst_line(),
        "",
        f"Investigation {doc['id']}: {doc['title']}",
        "",
        f"Hypothesis (revision {p.get('rev')}): {unk(p.get('hypothesis'))}",
        f"Baseline: {unk(p.get('baseline'))}   Candidates: {unk(', '.join(p.get('candidates') or []))}",
        f"Queries: {unk(p.get('queries'))}   Judgments: {unk(p.get('judgments'))}   Primary metric: {unk(p.get('primary_metric'))}",
        f"Runs so far: {', '.join(runs) if runs else 'none'}   State: {doc['state']}",
        f"Evidence: {', '.join(e['kind'] + ' ' + e['ref'] for e in doc['evidence']) or 'none yet'}",
        "",
        f"What to do: {asks[ask]}",
        "",
        "How: use the `cst` CLI with --json (run `cst --help`; `cst investigate show " + doc["id"] + " --json` for this record). "
        "Record your work on the investigation: `cst investigate evidence`, `cst signal new`, `cst investigate revise`, "
        "`cst investigate run`. Save definitions as code (labels: `cst label define`).",
        f"Limits: agent runs {'allowed' if pol['agent_runs'] else 'NOT allowed'}, up to {pol['max_queries']:,} queries, "
        f"{pol['max_runs_per_day']} runs a day, compute {', '.join(pol['compute'])}. Captain labels your work as yours (Claude Code, Codex and Cursor are recognised; anything else, set CST_AGENT=<your name>). "
        "Recommend an experiment for a person to approve with `cst recommend new --title ... --hypothesis ... --arms baseline@v1,candidate@v1 --queries ...`.",
        "Credentials: use the workspace's configured profiles and keychain secrets by name. Never print or copy secret values.",
        "Report limitations and the next experiment. Keep offline evidence separate from online or business impact. "
        "A person records the decision.",
    ]
    if base_url:
        lines.append(f"Captain API: {base_url} (token from `cst` config, not this prompt)")
    if doc["missing"]:
        lines += ["", "Missing before this can run: " + "; ".join(doc["missing"]) + ". Discover these first."]
    return "\n".join(lines)
