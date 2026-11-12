"""An A/B test between the baseline and a candidate, on the staging endpoint.

Offline evaluation says a candidate is safe to try. This is the try: shoppers are split between the two versions
and what they do is counted. One record per project (`ab` in the project's state) holds the arms, the split, when
it started, and the metrics chosen up front: one primary and the guardrails.

Two ways to run it:

  captain   Captain's staging endpoint does the split. Both versions are already loaded (staging packs the
            baseline beside the candidate), a hash of the session id picks the arm, and the arm is logged on every
            request. Ramping changes one number in the endpoint's control document: no redeploy.
  own       The team splits traffic in its own A/B system. Captain gives the address of each arm on the endpoint
            and records the readout the team brings back (`bring`).

Outcomes reach Captain through `POST /outcome` on the endpoint ({request_id, event, value}) or from an events
dataset that carries request_id (`import_outcomes`).

The readout counts by session, the unit that was assigned: per arm the sessions, the primary metric, the
difference with a 95% interval, and each guardrail. It says "not enough data yet" until both arms have the minimum
number of sessions. Where the numbers came from is never guessed: REAL_TRAFFIC only when the requests were real,
SIMULATED when the shopper simulator drove the endpoint (`simulate`), and "test traffic" otherwise. Traffic of
different kinds is never mixed in one readout.

Ramp and roll back are one call each and are written to the project's history. Rolling back sends every shopper
to the baseline again. With no test running, it restores the version that was on the endpoint before this one.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random

from .. import project_state as PS
from ..model.entities import new_id
from ..workspace import Context, whoami
from . import ENV, _note, _save, call, read_flush, set_control, state
from . import logs as L

METRICS = {"clicks": "Sessions with a click", "purchases": "Sessions with a purchase", "revenue": "Revenue per session"}
RAMP = (0.01, 0.05, 0.25, 0.5, 1.0)  # start small: ship an expensive model to 1% and learn
MODES = {"must": "Must not get worse", "watch": "Watch"}
SEGMENTS = {"head": "Head queries", "torso": "Torso queries", "tail": "Tail queries", "new": "New shoppers", "returning": "Returning shoppers"}
MIN_SESSIONS = 200
LABELS = {"real": ("REAL_TRAFFIC", "Real traffic"), "unlabelled": ("UNLABELLED", "Unlabelled"),
          "simulated": ("SIMULATED", "Simulated shoppers"), "test": ("TEST_TRAFFIC", "Test traffic")}
UNLABELLED = ('These requests carry no label, so Captain can\'t call them real shoppers. If they are, send '
              '"traffic": "real" with each request: the Connect your site code does.')
Z = 1.959964


def share(x) -> float:
    """A share of traffic as a person writes it: 0.05, 5 or "5%" all mean five percent."""
    t = str(x).strip()
    try:
        v = float(t.rstrip("%"))
    except ValueError:
        raise ValueError(f"{x!r} isn't a share of traffic. Write 5, 5% or 0.05.") from None
    if t.endswith("%") or v > 1:
        v = v / 100
    if not 0 < v <= 1:
        raise ValueError("the candidate's share of traffic is between 0 and 100%, for example 5%")
    return v


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get(ctx: Context, pid: str) -> dict | None:
    return PS.get(ctx, pid).get("ab") or None


def _put(ctx: Context, pid: str, change) -> dict:
    def ch(doc):
        ab = dict(doc.get("ab") or {})
        out = change(ab)
        doc["ab"] = ab if out is None else out
        return dict(doc["ab"]) if doc["ab"] else {}

    return PS._update(ctx, pid, ch)


def metrics_for(ctx: Context, pid: str) -> list[str]:
    """What this project can measure: clicks and purchases always, revenue per session when the catalog has a price."""
    from .. import features as F

    out = ["clicks", "purchases"]
    cat = PS.pinned(PS.get(ctx, pid), "catalog")
    if cat:
        try:
            m, _ = ctx.registry.resolve("dataset", cat)
            if F.detect_spec([f["name"] for f in m.schema_fields]).get("price"):
                out.append("revenue")
        except Exception:  # noqa: BLE001  no title column, or a catalog kept by reference: no revenue metric
            pass
    return out


def _running(ctx: Context, pid: str, env: str) -> dict:
    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Staging is not running. Stage a candidate first, then start the A/B test.")
    return rec


def addresses(rec: dict) -> dict:
    """Where each arm answers, for a team that splits traffic itself."""
    from .traffic import _http

    ep = rec.get("endpoint") or {}
    if _http(rec):  # this computer, and every host whose endpoint has an address of its own
        return {a: {"url": f"{ep.get('url')}/rank/{a}", "auth": "Authorization: Bearer <the endpoint key>"} for a in rec["arms"]}
    return {a: {"endpoint": ep.get("name"), "region": ep.get("region"),
                "body": {"op": "rank", "arm": a, "key": "<the endpoint key>", "query": "...", "session_id": "..."}} for a in rec["arms"]}


def traffic_seen(ctx: Context, pid: str, env: str = ENV) -> dict:
    """What has reached the endpoint so far: real requests and sessions, and the sessions a day that implies.
    Only real traffic counts: the console and simulated shoppers are not a site."""
    rec = state(ctx, pid, env)
    if not rec:
        return {"real": 0, "unlabelled": 0, "sessions": 0, "per_day": None}
    read_flush(ctx, rec)  # not on a sleeping host: reading a page must not wake a copy
    every = L.read(ctx, pid, env, rec["id"])["requests"]
    unlabelled = sum(1 for r in every if (r.get("traffic") or "unlabelled") == "unlabelled")
    reqs = [r for r in every if (r.get("traffic") or "unlabelled") == "real" and not r.get("forced")]
    units = {str(r.get("session_id") or r.get("user_id") or r["request_id"]) for r in reqs}
    per_day = None
    if len(reqs) >= 2:
        ts = sorted(str(r["ts"]) for r in reqs)
        try:
            span = (dt.datetime.fromisoformat(ts[-1].replace("Z", "+00:00")) - dt.datetime.fromisoformat(ts[0].replace("Z", "+00:00"))).total_seconds()
            per_day = round(len(units) * 86400 / max(span, 3600.0))  # under an hour of traffic counts as an hour
        except ValueError:
            per_day = None
    return {"real": len(reqs), "unlabelled": unlabelled, "sessions": len(units), "per_day": per_day}


def options(ctx: Context, pid: str, env: str = ENV) -> dict:
    """What the A/B form needs before it opens: has a site sent anything, and what each share would mean in
    sessions a day."""
    seen = traffic_seen(ctx, pid, env)
    rate = seen["per_day"]
    shares = [{"split": x, "label": f"{x:.0%}",
               "text": (f"{x:.0%}: about {max(1, round(rate * x)):,} of the {rate:,} sessions a day this endpoint sees" if rate
                        else f"{x:.0%} of sessions")} for x in RAMP[:4]]
    ready = seen["real"] > 0
    return {"project": pid, **seen, "ready": ready, "shares": shares, "can_measure": metrics_for(ctx, pid),
            "text": ("Your site is sending requests. A split now means something." if ready else
                     f"{seen['unlabelled']:,} request{'s have' if seen['unlabelled'] != 1 else ' has'} reached this endpoint with no label, so "
                     "Captain can't count them as shoppers. " + UNLABELLED.split(". ", 1)[1] + " Or practice with simulated shoppers."
                     if seen.get("unlabelled") else
                     "No real request has reached this endpoint yet, so a split would divide nothing. Connect your site "
                     "first, or practice with simulated shoppers.")}


def start(ctx: Context, pid: str, split: float = 0.05, primary: str | None = None, guardrails: list[str] | None = None,
          mode: str = "captain", min_sessions: int = MIN_SESSIONS, env: str = ENV, dry_run: bool = False,
          practice: bool = False) -> dict:
    """`practice` runs the test on simulated shoppers only: labelled SIMULATED throughout, and it can't go to launch
    review. Without it, Captain's own split needs a site that is already sending real requests."""
    rec = _running(ctx, pid, env)
    arms = rec.get("arms") or {}
    if not {"baseline", "candidate"} <= set(arms):
        raise ValueError("An A/B test needs the candidate and the baseline on the endpoint. Stage a candidate while "
                         "the project has a baseline, then start the test.")
    if rec.get("how") == "image":
        raise ValueError("Captain can't split traffic inside your own container. Run the test in your own A/B system.")
    if mode not in ("captain", "own"):
        raise ValueError("mode is captain (Captain's endpoint splits the traffic) or own (your A/B system does)")
    split = share(split)
    if mode == "captain" and not practice and not dry_run and not traffic_seen(ctx, pid, env)["real"]:
        raise ValueError("No real request has reached this endpoint yet, so there is nothing to split. Connect your "
                         "site first. To see how a test works without traffic, practice with simulated shoppers "
                         "(--practice): everything it shows is SIMULATED and it can't go to launch review.")
    cur = get(ctx, pid)
    if cur and cur.get("status") == "running":
        raise ValueError(f"An A/B test is already running ({cur['id']}). Roll it back or stop it first.")
    can = metrics_for(ctx, pid)
    primary = primary or "purchases"
    # A guardrail is "must" (must not get worse: a clear drop pauses the ramp) or "watch" (shown, never blocking).
    # Written as a name, "name:must" or "name:watch". Response time is always there, and is "must" unless told otherwise.
    modes: dict[str, str] = {"latency": "must"}
    named = []
    for g in (guardrails if guardrails is not None else can):
        name, _, how = (g if isinstance(g, str) else f"{g.get('metric')}:{g.get('mode') or ''}").partition(":")
        if how and how not in MODES:
            raise ValueError("a guardrail is marked must (must not get worse) or watch")
        if name == "latency":
            modes["latency"] = how or "must"
            continue
        if name != primary:
            named.append(name)
            modes[name] = how or "watch"
    guardrails = named
    for m in [primary, *guardrails]:
        if m not in can:
            raise ValueError(f"{m} can't be measured here. This project can measure: {', '.join(can)}."
                             + (" Revenue needs a price column in the catalog." if m == "revenue" else ""))
    ab = {"id": "ab-" + new_id().lower()[-10:], "env": env, "status": "running", "mode": mode, "arms": dict(arms),
          "stage": rec["id"], "split": float(split) if mode == "captain" else None, "primary": primary,
          "guardrails": guardrails, "guardrail_modes": modes, "paused": None, "min_sessions": int(min_sessions), "started_at": _now(), "started_by": whoami(),
          "ended_at": None, "ramps": [{"at": _now(), "by": whoami(), "split": float(split), "action": "start"}] if mode == "captain" else [],
          "brought": [], "last_readout": None, "addresses": addresses(rec) if mode == "own" else None,
          "practice": bool(practice), "decision": None, "rolled_back": None}
    if dry_run:
        return {**ab, "dry_run": True}
    old = get(ctx, pid)
    if mode == "captain":
        set_control(ctx, pid, env, split=float(split), experiment=ab["id"], salt=ab["id"], serve="candidate", shadow=None)
        call(ctx, rec, "reload", {})
        from . import traffic as TF

        sh = TF.shadow_get(ctx, pid)
        if sh and sh.get("status") == "running":  # shadow mode comes before a split, and ends when one starts
            TF._shadow_put(ctx, pid, {**sh, "status": "ended", "ended_at": _now()})

    def ch(doc):
        if old:  # the earlier test is filed, never left on screen as if it were current
            doc["ab_history"] = ((doc.get("ab_history") or []) + [old])[-20:]
        doc["ab"] = ab

    PS._update(ctx, pid, ch)
    _note(ctx, pid, (f"started an A/B test: {arms['candidate']} gets {split:.0%} of traffic against {arms['baseline']}"
                     if mode == "captain" else f"started an A/B test in the team's own system: {arms['candidate']} against {arms['baseline']}"))
    return ab


def _need(ctx: Context, pid: str) -> dict:
    ab = get(ctx, pid)
    if not ab:
        raise ValueError("No A/B test has been started for this project.")
    return ab


def ramp(ctx: Context, pid: str, split: float, dry_run: bool = False) -> dict:
    """Give the candidate this share of traffic. Shoppers already on the candidate stay on it when the share grows."""
    ab = _need(ctx, pid)
    if ab["status"] != "running" or ab["mode"] != "captain":
        raise ValueError("Only a running test that Captain's endpoint splits can be ramped." if ab["mode"] == "captain"
                         else "Your own A/B system holds the split. Ramp it there.")
    split = share(split)
    if float(split) > float(ab.get("split") or 0):
        ab = readout(ctx, pid)["ab"]  # look at the guardrails now, not as of the last time someone looked
        if ab.get("paused"):
            raise ValueError(f"The ramp is paused: {ab['paused']['why']} Roll back, or ramp down. It can go up again once that holds.")
    if dry_run:
        return {**ab, "split": float(split), "dry_run": True}
    rec = _running(ctx, pid, ab["env"])
    set_control(ctx, pid, ab["env"], split=float(split))
    call(ctx, rec, "reload", {})
    prev = ab["split"]

    def ch(a):
        a["split"] = float(split)
        a["ramps"] = (a.get("ramps") or []) + [{"at": _now(), "by": whoami(), "split": float(split), "action": "ramp", "from": prev}]

    out = _put(ctx, pid, ch)
    _note(ctx, pid, f"ramped the A/B test: the candidate gets {split:.0%} of traffic (was {prev:.0%})")
    return out


def rollback(ctx: Context, pid: str, env: str = ENV, dry_run: bool = False, reason: str | None = None) -> dict:
    """Send everyone back to the previous version. During a test: every shopper gets the baseline again, and the
    test ends as rolled back. With no test running: the version staged before this one is staged again."""
    from . import stage as _stage

    ab = get(ctx, pid)
    rec = state(ctx, pid, env)
    if ab and ab.get("status") == "running" and ab["mode"] == "captain":
        if dry_run:
            return {"rolled_back_to": ab["arms"]["baseline"], "experiment": ab["id"], "dry_run": True}
        if rec and rec.get("status") == "running":
            set_control(ctx, pid, env, split=None, experiment=None, serve="baseline")
            call(ctx, rec, "reload", {})
            _save(ctx, pid, env, serve="baseline", model_version=ab["arms"]["baseline"])

        why = (reason or "").strip() or ((ab.get("paused") or {}).get("why")) or f"{whoami()} rolled it back"
        was = ab.get("split") or 0.0

        def ch(a):
            a["status"], a["ended_at"] = "rolled_back", _now()
            a["ramps"] = (a.get("ramps") or []) + [{"at": _now(), "by": whoami(), "split": 0.0, "action": "rollback", "from": a.get("split")}]
            a["split"] = 0.0
            # One line for the page: what was rolled back, why, and what to do next. The readout itself is filed.
            a["rolled_back"] = {"at": _now(), "by": whoami(), "why": why, "from_split": was,
                                "text": f"Rolled back {a['arms']['candidate']} from {was:.0%} of traffic: {why.rstrip('.')}. "
                                        f"Every shopper gets {a['arms']['baseline']} again. Next: fix what went wrong and "
                                        "start a new test, or keep the baseline."}

        _put(ctx, pid, ch)
        _note(ctx, pid, f"rolled back the A/B test: every shopper gets the baseline {ab['arms']['baseline']} again")
        return {"rolled_back_to": ab["arms"]["baseline"], "experiment": ab["id"], "ab": get(ctx, pid)}
    if not rec or not rec.get("previous"):
        raise ValueError("There is no earlier version on this endpoint to roll back to.")
    prev = rec["previous"]
    if dry_run:
        return {"rolled_back_to": prev, "experiment": None, "dry_run": True}
    champ = PS.get(ctx, pid).get("champion")
    out = _stage(ctx, pid, candidate=None if prev == champ else prev, baseline=prev == champ, env=env, target=rec["target"],
                 confirm=True)
    _note(ctx, pid, f"rolled staging back to {prev}")
    return {"rolled_back_to": prev, "experiment": None, "staged": out}


def stop(ctx: Context, pid: str) -> dict:
    """End the test and keep the endpoint as it is serving now, with no split."""
    ab = _need(ctx, pid)
    if ab["status"] != "running":
        return ab
    rec = state(ctx, pid, ab["env"])
    if ab["mode"] == "captain" and rec and rec.get("status") == "running":
        serve = "candidate" if (ab.get("split") or 0) >= 1 else "baseline"
        set_control(ctx, pid, ab["env"], split=None, experiment=None, serve=serve)
        call(ctx, rec, "reload", {})
        _save(ctx, pid, ab["env"], serve=serve, model_version=ab["arms"][serve])

    def ch(a):
        a["status"], a["ended_at"] = "finished", _now()

    out = _put(ctx, pid, ch)
    _note(ctx, pid, "ended the A/B test")
    return out


# ---- outcomes from an events dataset ------------------------------------------------------------


def import_outcomes(ctx: Context, pid: str, events: str, env: str = ENV, dry_run: bool = False) -> dict:
    """Read outcomes from an events dataset whose rows carry request_id: either one row per event
    (request_id, event, product_id?, value?) or impression rows (request_id, product_id, clicked, purchased, ...)."""
    from ..ops import ref, rows_of

    rec = state(ctx, pid, env)
    if not rec:
        raise ValueError(f"Nothing is staged for {pid}.")
    m, rows, _ = rows_of(ctx, events)
    if not rows or "request_id" not in rows[0]:
        raise ValueError(f"{ref(m)} has no request_id column, so its rows can't be matched to what the endpoint served. "
                         "Carry the request_id that /rank returned into your events.")
    outs = []
    for r in rows:
        rid = r.get("request_id")
        if not rid:
            continue
        base = {"request_id": str(rid), "product_id": None if r.get("product_id") is None else str(r["product_id"]),
                "ts": str(r.get("ts") or _now()), "source": ref(m)}
        if r.get("event") in L.EVENTS:
            outs.append({**base, "event": r["event"], "value": float(r.get("value") or 0.0)})
            continue
        if r.get("clicked"):
            outs.append({**base, "event": "click", "value": 0.0})
        if r.get("added_to_cart"):
            outs.append({**base, "event": "add-to-cart", "value": 0.0})
        if r.get("purchased"):
            outs.append({**base, "event": "purchase", "value": float(r.get("value") or r.get("price") or 0.0)})
    if rec.get("status") == "running":
        try:
            call(ctx, rec, "flush", {})  # so requests still in the endpoint's buffer count as known
        except Exception:  # noqa: BLE001
            pass
    known = {r["request_id"] for r in L.read(ctx, pid, env)["requests"]}
    matched = sum(1 for o in outs if o["request_id"] in known)
    out = {"events": ref(m), "outcomes": len(outs), "matched": matched, "unmatched": len(outs) - matched, "dry_run": dry_run}
    if outs and not dry_run:
        L.shared(ctx, pid, env).put_new(L.prefix(pid, env, rec["id"]) + f"outcomes-import-{new_id()}.jsonl",
                          "\n".join(json.dumps(o) for o in outs).encode())
        _note(ctx, pid, f"brought in {len(outs):,} outcomes from {ref(m)}")
    return out


# ---- the readout --------------------------------------------------------------------------------


def _diff(c: list, b: list) -> dict:
    """Candidate minus baseline for per-session values kept as [n, sum, sum of squares], with a 95% interval."""
    def mv(x):
        n, s, q = x
        mean = s / n if n else 0.0
        var = max(0.0, (q - n * mean * mean) / (n - 1)) if n > 1 else 0.0
        return n, mean, var

    nc, mc, vc = mv(c)
    nb, mb, vb = mv(b)
    d = mc - mb
    se = math.sqrt(vc / nc + vb / nb) if nc and nb else 0.0
    return {"baseline": round(mb, 6), "candidate": round(mc, 6), "diff": round(d, 6), "ci_low": round(d - Z * se, 6),
            "ci_high": round(d + Z * se, 6), "lift": round(d / mb, 4) if mb else None}


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 3)


def _fmt(metric: str, v: float) -> str:
    return f"${v:,.2f}" if metric == "revenue" else f"{v * 100:.1f}%"


def segment_of(seg: dict | None, r: dict) -> list[str]:
    """The segments one request belongs to: head, torso or tail by how often its query is searched in the project's
    logs, and new or returning when the logs know its user."""
    if not seg:
        return []
    out = [(seg.get("query") or {}).get(" ".join(str(r.get("query") or "").lower().split()), "tail")] if seg.get("query") is not None else []
    if seg.get("users") is not None and r.get("user_id"):
        out.append("returning" if str(r["user_id"]) in seg["users"] else "new")
    return out


def query_segments(rows: list[dict]) -> dict:
    """Head, torso and tail from an events log: queries ranked by how many searches they get, cut where the running
    share of searches passes one third and two thirds. A query the log never saw is tail."""
    seen: dict[str, set] = {}
    users = set()
    for r in rows:
        q = " ".join(str(r.get("query") or "").lower().split())
        seen.setdefault(q, set()).add(str(r.get("session_id") or r.get("ts") or len(seen)))
        if r.get("user_id") is not None:
            users.add(str(r["user_id"]))
    counts = sorted(((len(v), q) for q, v in seen.items()), reverse=True)
    total, run, out = sum(c for c, _ in counts) or 1, 0, {}
    for c, q in counts:
        out[q] = "head" if run < total / 3 else "torso" if run < 2 * total / 3 else "tail"
        run += c
    return {"query": out, "users": users or None}


def compute(ab: dict, got: dict, traffic: str | None = None, segments: dict | None = None) -> dict:
    """The readout from a staging log (logs.read), for one kind of traffic. Pure: no storage, no endpoint.
    `segments` (query_segments) adds the same comparison per query segment and per new or returning shopper."""
    reqs = [r for r in got["requests"] if r.get("experiment") == ab["id"] and not r.get("forced")]
    by_kind: dict[str, int] = {}
    for r in reqs:
        by_kind[r.get("traffic") or "unlabelled"] = by_kind.get(r.get("traffic") or "unlabelled", 0) + 1
    kind = traffic or next((k for k in ("real", "unlabelled", "simulated", "test") if by_kind.get(k)), "real")
    reqs = [r for r in reqs if (r.get("traffic") or "unlabelled") == kind]
    unit_of = {}
    sess: dict[str, dict[str, dict]] = {"baseline": {}, "candidate": {}}
    by_seg: dict[str, dict[str, dict[str, dict]]] = {}  # segment -> arm -> session -> what happened in that segment
    ms: dict[str, list] = {"baseline": [], "candidate": []}
    new = lambda: {"clicks": 0.0, "purchases": 0.0, "revenue": 0.0, "searches": 0}  # noqa: E731
    for r in reqs:
        u = str(r.get("session_id") or r.get("user_id") or r["request_id"])
        segs = segment_of(segments, r)
        unit_of[str(r["request_id"])] = (r["arm"], u, segs)
        cells = [sess[r["arm"]].setdefault(u, new())]
        cells += [by_seg.setdefault(g, {"baseline": {}, "candidate": {}})[r["arm"]].setdefault(u, new()) for g in segs]
        for s in cells:
            s["searches"] += 1
        ms[r["arm"]].append(float(r.get("ms") or 0.0))
    unmatched = 0
    for o in got["outcomes"]:
        hit = unit_of.get(str(o["request_id"]))
        if not hit:
            unmatched += 1
            continue
        for s in [sess[hit[0]][hit[1]]] + [by_seg[g][hit[0]][hit[1]] for g in hit[2]]:
            if o["event"] in ("click", "add-to-cart", "purchase"):
                s["clicks"] = 1.0
            if o["event"] == "purchase":
                s["purchases"] = 1.0
                s["revenue"] += float(o.get("value") or 0.0)
    n = {a: len(sess[a]) for a in sess}
    need = int(ab.get("min_sessions") or MIN_SESSIONS)
    enough = min(n.values()) >= need

    def stat(metric: str, of: dict | None = None) -> dict:
        of = of or sess
        acc = {a: [0, 0.0, 0.0] for a in of}
        for a in of:
            for s in of[a].values():
                v = s[metric]
                acc[a][0] += 1
                acc[a][1] += v
                acc[a][2] += v * v
        return {"metric": metric, "label": METRICS[metric], **_diff(acc["candidate"], acc["baseline"])}

    evidence, label = LABELS[kind]
    if not reqs and not by_kind:
        # Nothing has arrived, so there is nothing to label. "Real traffic" is only ever said of real requests.
        evidence, label = None, "No traffic yet"
    p = stat(ab["primary"])
    if not enough:
        short = max(0, need - min(n.values()))
        p["verdict"] = "not enough data yet"
        p["text"] = (f"Not enough data yet: {n['baseline']:,} baseline and {n['candidate']:,} candidate sessions. "
                     f"Each arm needs {need:,}" + (f", so about {short:,} more." if short else "."))
    else:
        p["verdict"] = "candidate ahead" if p["ci_low"] > 0 else "candidate behind" if p["ci_high"] < 0 else "no clear difference"
        rng = f"{_fmt(p['metric'], p['baseline'])} baseline, {_fmt(p['metric'], p['candidate'])} candidate"
        sign = "+" if p["diff"] >= 0 else "-"
        delta = (f"{sign}${abs(p['diff']):,.2f}" if p["metric"] == "revenue" else f"{p['diff'] * 100:+.1f} points")
        between = (f"${p['ci_low']:,.2f} and ${p['ci_high']:,.2f}" if p["metric"] == "revenue"
                   else f"{p['ci_low'] * 100:+.1f} and {p['ci_high'] * 100:+.1f} points")
        p["text"] = (f"{p['label']}: {rng}. The difference is {delta}, between {between} (95% interval). "
                     + {"candidate ahead": "The candidate is ahead.", "candidate behind": "The candidate is behind.",
                        "no clear difference": "No clear difference yet."}[p["verdict"]])
    modes = ab.get("guardrail_modes") or {}
    guards = []
    for g in ab.get("guardrails") or []:
        s = stat(g)
        s["mode"] = modes.get(g, "watch")
        s["ok"] = (not enough) or s["ci_high"] >= 0  # fails only when the candidate is clearly worse
        s["text"] = (f"{s['label']}: {_fmt(g, s['baseline'])} baseline, {_fmt(g, s['candidate'])} candidate. "
                     + ("Too early to say." if not enough else "Holds." if s["ok"] else "The candidate is clearly worse."))
        guards.append(s)
    pb, pc = _p95(ms["baseline"]), _p95(ms["candidate"])
    if pb is not None and pc is not None:
        ok = pc <= max(2 * pb, pb + 20.0)
        guards.append({"metric": "latency", "label": "Response time (95th percentile)", "baseline": pb, "candidate": pc,
                       "ok": ok, "mode": modes.get("latency", "must"), "text": f"Response time: {pb:.1f} ms baseline, {pc:.1f} ms candidate (95th percentile). "
                                         + ("Holds." if ok else "The candidate is much slower.")})
    # Is the split what was asked for? Sessions assigned since the last ramp, against its share.
    srm = None
    ramps = ab.get("ramps") or []
    if ab.get("mode") == "captain" and ramps and ab.get("status") == "running":
        since = ramps[-1]["at"]
        cur = {"baseline": 0, "candidate": 0}
        first = {}
        for r in reqs:
            u = str(r.get("session_id") or r.get("user_id") or r["request_id"])
            if u not in first:
                first[u] = (r["ts"], r["arm"])
        for ts, arm in first.values():
            if str(ts)[:19] >= str(since)[:19]:
                cur[arm] += 1
        tot, share = cur["baseline"] + cur["candidate"], float(ramps[-1]["split"])
        if tot >= 100 and 0 < share < 1:
            chi = (cur["candidate"] - tot * share) ** 2 / (tot * share) + (cur["baseline"] - tot * (1 - share)) ** 2 / (tot * (1 - share))
            pv = math.erfc(math.sqrt(chi / 2))
            srm = {"expected": share, "observed": round(cur["candidate"] / tot, 4), "sessions": tot, "p_value": round(pv, 6),
                   "ok": pv >= 0.001,
                   "text": (f"The split is as asked: {cur['candidate'] / tot:.1%} of {tot:,} sessions went to the candidate "
                            f"(asked for {share:.0%}).") if pv >= 0.001 else
                           (f"The split looks off: {cur['candidate'] / tot:.1%} of {tot:,} sessions went to the candidate, "
                            f"and {share:.0%} was asked for. Don't trust this readout until that is explained.")}
    # The same comparison inside each segment, each with its own "not enough data yet". A win overall can hide a
    # loss on the tail: say so when it does.
    seg_rows = []
    for g in [k for k in SEGMENTS if k in by_seg]:
        sn = {a: len(by_seg[g][a]) for a in ("baseline", "candidate")}
        row = {"segment": g, "label": SEGMENTS[g], "sessions": sn, **{k: v for k, v in stat(ab["primary"], by_seg[g]).items() if k not in ("metric", "label")}}
        if min(sn.values()) < need:
            row["enough"], row["verdict"] = False, "not enough data yet"
            row["text"] = f"{SEGMENTS[g]}: not enough data yet ({sn['baseline']:,} baseline and {sn['candidate']:,} candidate sessions, each needs {need:,})."
        else:
            row["enough"] = True
            row["verdict"] = "candidate ahead" if row["ci_low"] > 0 else "candidate behind" if row["ci_high"] < 0 else "no clear difference"
            between = (f"${row['ci_low']:,.2f} and ${row['ci_high']:,.2f}" if p["metric"] == "revenue"
                       else f"{row['ci_low'] * 100:+.1f} and {row['ci_high'] * 100:+.1f} points")
            row["text"] = (f"{SEGMENTS[g]}: {_fmt(p['metric'], row['baseline'])} baseline, {_fmt(p['metric'], row['candidate'])} candidate, "
                           f"difference between {between}. "
                           + {"candidate ahead": "Ahead.", "candidate behind": "Behind.", "no clear difference": "No clear difference."}[row["verdict"]])
        seg_rows.append(row)
    behind = [r["label"].lower() for r in seg_rows if r["verdict"] == "candidate behind"]
    seg_warning = (("The candidate is ahead overall but behind on " if p["verdict"] == "candidate ahead" else "The candidate is behind on ")
                   + " and ".join(behind) + ".") if behind else None
    # A "must not get worse" guardrail that fails is a hard stop for the ramp.
    stops = [g for g in guards if not g["ok"] and g.get("mode") == "must"]
    for g in guards:
        g["text"] = f"{MODES[g.get('mode', 'watch')]}. {g['text']}"
    arms = {a: {"model_version": ab["arms"][a], "sessions": n[a], "searches": sum(s["searches"] for s in sess[a].values()),
                "p95_ms": _p95(ms[a])} for a in sess}
    return {"experiment": ab["id"], "status": ab["status"], "mode": ab["mode"], "split": ab.get("split"),
            "traffic": kind, "evidence": evidence, "label": label, "arms": arms, "primary": p, "guardrails": guards,
            "enough": enough, "min_sessions": need, "srm": srm, "unmatched_outcomes": unmatched,
            "segments": seg_rows, "segment_warning": seg_warning,
            "hard_stop": ("The ramp is paused: " + " ".join(g["text"] for g in stops)) if stops else None,
            "other_traffic": {k: v for k, v in by_kind.items() if k != kind},
            "guardrails_ok": all(g["ok"] for g in guards) and (srm is None or srm["ok"]),
            "text": f"{label}. " + p["text"]}


def readout(ctx: Context, pid: str, traffic: str | None = None) -> dict:
    """Where the test stands. For a test in the team's own system: the newest readout they brought in."""
    ab = get(ctx, pid)
    if not ab:
        return {"project": pid, "experiment": None, "status": "none", "can_measure": metrics_for(ctx, pid),
                "text": "No A/B test yet. Split traffic between the baseline and the candidate to see which shoppers prefer."}
    base = {"project": pid, "ab": ab, "can_measure": metrics_for(ctx, pid), "ramp_steps": list(RAMP)}
    if ab["mode"] == "own":
        last = (ab.get("brought") or [None])[-1]
        return {**base, "experiment": ab["id"], "status": ab["status"], "mode": "own", "brought": last, "enough": bool(last),
                "evidence": last["evidence"] if last else None, "label": last["label"] if last else None,
                "addresses": ab.get("addresses"),
                "text": last["text"] if last else "Waiting for the readout from your own A/B system. Bring it in when it has run."}
    rec = state(ctx, pid, ab["env"])
    read_flush(ctx, rec)  # not on a sleeping host: reading a page must not wake a copy
    out = compute(ab, L.read(ctx, pid, ab["env"], ab.get("stage")), traffic, _segments(ctx, pid))
    # A failed "must not get worse" guardrail pauses the ramp, and it is written down once, with why.
    why = " ".join(g["text"] for g in out["guardrails"] if not g["ok"] and g.get("mode") == "must") or None
    if ab["status"] == "running" and bool(why) != bool(ab.get("paused")):
        _put(ctx, pid, lambda a: a.__setitem__("paused", {"at": _now(), "why": why} if why else None))
        _note(ctx, pid, f"paused the A/B ramp: {why}" if why else "the A/B ramp can go on: the guardrail holds again")
        ab = get(ctx, pid)
        base["ab"] = ab
    out["practice"] = bool(ab.get("practice"))
    out["needed"] = needed(ab, out, traffic_seen(ctx, pid, ab["env"])["per_day"] if out["traffic"] == "real" else None)
    out["decision"] = ab.get("decision")
    out["rolled_back"] = ab.get("rolled_back")
    out["can_ship"] = bool(out["enough"] and out["evidence"] == "REAL_TRAFFIC" and not ab.get("practice"))
    out["cannot_ship"] = (None if out["can_ship"] else
                          "This is a practice test on simulated shoppers. It can't go to launch review." if ab.get("practice") or out["evidence"] == "SIMULATED"
                          else "These requests are test traffic, not shoppers. It can't go to launch review on them." if out["evidence"] == "TEST_TRAFFIC"
                          else UNLABELLED + " Until then it can't go to launch review." if out["evidence"] == "UNLABELLED"
                          else "Not enough data yet to send it to launch review.")
    summary = {"at": _now(), "sessions": {a: out["arms"][a]["sessions"] for a in out["arms"]}, "enough": out["enough"],
               "evidence": out["evidence"], "verdict": out["primary"]["verdict"], "text": out["text"]}
    if summary != {**(ab.get("last_readout") or {}), "at": summary["at"]}:
        _put(ctx, pid, lambda a: a.__setitem__("last_readout", summary))
    return {**base, **out}


_seg_cache: dict[str, dict] = {}


def _segments(ctx: Context, pid: str) -> dict | None:
    """Query segments from the project's own events log, when it has one. Read once per version."""
    from ..ops import rows_of

    ev = PS.pinned(PS.get(ctx, pid), "events")
    if not ev:
        return None
    k = f"{ctx.workspace.name}:{ev}"
    if k not in _seg_cache:
        try:
            _, rows, _ = rows_of(ctx, ev)
            _seg_cache[k] = query_segments(rows) if rows and "query" in rows[0] else None
        except Exception:  # noqa: BLE001  events kept somewhere this computer can't read: no segments
            _seg_cache[k] = None
        if len(_seg_cache) > 8:
            _seg_cache.pop(next(iter(_seg_cache)))
    return _seg_cache[k]


def needed(ab: dict, out: dict, per_day: int | None) -> dict | None:
    """"About N more sessions needed to tell a difference of X, roughly D days at this rate": the sessions each arm
    needs for the difference seen so far to be told from chance (80% power at the 95% level), from the spread of
    the primary metric across sessions."""
    p = out["primary"]
    nb, nc = out["arms"]["baseline"]["sessions"], out["arms"]["candidate"]["sessions"]
    if min(nb, nc) < 2 or p.get("verdict") in ("candidate ahead", "candidate behind"):
        return None
    revenue = p["metric"] == "revenue"
    delta = max(abs(p["diff"]), (0.01 * max(p["baseline"], 1.0)) if revenue else 0.005)  # never chase a zero difference
    half = (p["ci_high"] - p["ci_low"]) / 2  # = 1.96 * sqrt(var_c/nc + var_b/nb)
    if half <= 0:
        return None
    var = (half / Z) ** 2 / (1 / nc + 1 / nb)  # the per-session variance the interval implies
    per_arm = math.ceil(2 * (Z + 0.8416) ** 2 * var / delta ** 2)
    more = max(0, per_arm - min(nb, nc))
    split = float(ab.get("split") or 0.5)
    slow = min(split, 1 - split) if 0 < split < 1 else None
    days = math.ceil(more / (per_day * slow)) if per_day and slow and more else None
    x = f"${delta:,.2f}" if revenue else f"{delta * 100:.1f} points"
    text = (f"About {more:,} more sessions needed in the smaller arm to tell a difference of {x}"
            + (f", roughly {days:,} day{'s' if days != 1 else ''} at this rate." if days else "."))
    if not more:
        text = f"There are enough sessions to tell a difference of {x}. The two are that close."
    return {"per_arm": per_arm, "more": more, "difference": round(delta, 6), "days": days, "per_day": per_day, "text": text}


CHOICES = {"ship": "Ship it", "keep-baseline": "Keep the baseline", "keep-testing": "Keep testing"}


def decision_key(pipeline: str) -> str:
    return "staging/ab-decisions/" + pipeline.replace("/", "__").replace("@", "-") + ".json"


def decide(ctx: Context, pid: str, choice: str, note: str = "") -> dict:
    """How a test ends, recorded: ship it (opens launch review with the readout and its evidence label attached),
    keep the baseline, or keep testing. Shipping needs real traffic and enough of it."""
    ab = _need(ctx, pid)
    if choice not in CHOICES:
        raise ValueError(f"the choice is one of {', '.join(CHOICES)}")
    r = readout(ctx, pid)
    if choice == "ship" and ab["mode"] == "captain" and not r.get("can_ship"):
        raise ValueError(r.get("cannot_ship") or "This test can't go to launch review yet.")
    if choice == "ship" and ab["mode"] == "own" and not (r.get("brought") or {}).get("evidence") == "REAL_TRAFFIC":
        raise ValueError("Bring in a readout on real traffic first. Only real traffic can go to launch review.")
    text = r.get("text") or ""
    d = {"choice": choice, "label": CHOICES[choice], "at": _now(), "by": whoami(), "note": note.strip() or None,
         "evidence": r.get("evidence"), "readout": text, "experiment": ab["id"],
         "sessions": {a: x["sessions"] for a, x in (r.get("arms") or {}).items()}}
    _put(ctx, pid, lambda a: a.__setitem__("decision", d))
    cand = ab["arms"]["candidate"]
    out = {"decision": d, "ab": get(ctx, pid)}
    if choice == "ship":
        # launch review reads this when the candidate is sent: the readout rides with it, labelled
        from ..storage.backend import NotFound

        body = json.dumps({**d, "project": pid, "pipeline": cand, "baseline": ab["arms"]["baseline"]}).encode()
        try:
            _, tag = ctx.store.get_with_tag(decision_key(cand))
        except NotFound:
            tag = None
        ctx.store.put_if(decision_key(cand), body, tag)
        out["launch_review"] = {"pipeline": cand, "readout": text, "evidence": r.get("evidence")}
    elif choice == "keep-baseline" and ab["status"] == "running":
        if ab["mode"] == "captain":
            rollback(ctx, pid, ab["env"], reason="the decision was to keep the baseline")
        else:
            stop(ctx, pid)
        out["ab"] = get(ctx, pid)
    _note(ctx, pid, f"decided on the A/B test: {CHOICES[choice].lower()}" + (f" ({note.strip()})" if note.strip() else ""))
    return out


def decision_for(ctx: Context, pipeline: str) -> dict | None:
    from ..storage.backend import NotFound

    try:
        return json.loads(ctx.store.get(decision_key(pipeline)))
    except NotFound:
        return None


def bring(ctx: Context, pid: str, metric: str, baseline: float, candidate: float, source: str, traffic: str = "real",
          ci_low: float | None = None, ci_high: float | None = None, sessions: int | None = None,
          handoff: str | None = None) -> dict:
    """Record the readout from the team's own A/B system. `traffic` is what the team says it was: only `real` is
    recorded as REAL_TRAFFIC."""
    ab = _need(ctx, pid)
    if not source.strip():
        raise ValueError("say where the number comes from (the A/B testing system and its report)")
    if traffic not in ("real", "simulated", "test"):
        raise ValueError("traffic is real, simulated or test")
    evidence, label = LABELS[traffic]
    d = float(candidate) - float(baseline)
    verdict = ("no interval given" if ci_low is None or ci_high is None else
               "candidate ahead" if ci_low > 0 else "candidate behind" if ci_high < 0 else "no clear difference")
    row = {"at": _now(), "by": whoami(), "metric": metric, "baseline": float(baseline), "candidate": float(candidate),
           "diff": round(d, 6), "ci_low": ci_low, "ci_high": ci_high, "sessions": sessions, "source": source,
           "traffic": traffic, "evidence": evidence, "label": label, "verdict": verdict,
           "text": f"{label}, from {source}. {metric}: {baseline:g} baseline, {candidate:g} candidate"
                   + (f", difference between {ci_low:+g} and {ci_high:+g}" if ci_low is not None and ci_high is not None else "") + "."}
    _put(ctx, pid, lambda a: a.__setitem__("brought", (a.get("brought") or []) + [row]))
    if handoff and traffic == "real":
        from .. import handoff as HO

        HO.outcome(ctx, handoff, metric, d, source, experiment=ab["id"], ci_low=ci_low, ci_high=ci_high)
    _note(ctx, pid, f"brought in an A/B readout from {source} ({label.lower()})")
    return row


# ---- simulated shoppers -------------------------------------------------------------------------


def simulate(ctx: Context, pid: str, sessions: int = 1000, seed: int = 7, env: str = ENV, progress=None) -> dict:
    """Send simulated shoppers at the staging endpoint, so the whole loop can be seen with no traffic yet. Each one
    searches, looks down the page, clicks and sometimes buys, by the shopper simulator's own click model. Every
    request is marked simulated, and everything read from it says SIMULATED."""
    import bisect
    import itertools

    from .. import ops
    from ..eval import queries as Q
    from ..eval.judge import heuristic_grade
    from ..sim import BUY, CLICK, personas
    from ..sim import events as SE

    rec = _running(ctx, pid, env)
    if not 1 <= int(sessions) <= 20_000:
        raise ValueError("sessions is between 1 and 20,000")
    cat = next((ctx.registry.resolve("pipeline", r)[0].catalog for r in (rec.get("arms") or {}).values()
                if _is_version(ctx, r) and ctx.registry.resolve("pipeline", r)[0].catalog), None)
    cat = cat or PS.pinned(PS.get(ctx, pid), "catalog")
    if not cat:
        raise ValueError("Simulated shoppers search the project's catalog, and this project has none.")
    prods = [p for p in ops.products_of(ctx, cat) if p.get("title")]
    pmap = {p["id"]: p for p in prods}
    seen_as = {p["id"]: {**p, "description": " ".join(filter(None, [SE._text(p.get("description"))] + [SE._text(p.get(a)) for a in SE.ATTRS]))}
               for p in prods}
    prices = sorted(x for x in (SE._price(p) for p in prods) if x is not None)
    median = prices[len(prices) // 2] if prices else None
    classes = [c for c in SE.CLASSES if c != "numeric" or prices]
    qs = Q.generate(prods, classes, max(40, min(400, len(prods))), seed)
    cum = list(itertools.accumulate(1.0 / (r + 1) ** SE.ZIPF_S for r in range(len(qs))))
    grade: dict = {}
    run = new_id().lower()[-6:]
    n_req = n_click = n_buy = 0
    for i, person in enumerate(personas(int(sessions), seed)):
        rng = random.Random(person["seed"])
        sid, uid = f"sim-{run}-{i}", f"simu-{run}-{int(sessions * 0.7 * rng.random() ** 1.4)}"
        budget = median * math.exp(rng.gauss(0.1, 0.6)) if median else None
        for _ in range(4):
            q = qs[min(len(qs) - 1, bisect.bisect_left(cum, rng.random() * cum[-1]))]["query"]
            res = call(ctx, rec, "rank", {"query": q, "k": SE.PAGE, "session_id": sid, "user_id": uid, "traffic": "simulated"})
            n_req += 1
            depth = SE.FOLD
            while depth < len(res["ids"]) and rng.random() < person["patience"] * SE.SCROLL:
                depth += 1
            for k, prod in enumerate(res["ids"][:depth]):
                if prod not in pmap:
                    continue
                if (q, prod) not in grade:
                    grade[(q, prod)] = heuristic_grade(q, seen_as[prod])
                g = grade[(q, prod)]
                price = SE._price(pmap[prod])
                over = budget is not None and price is not None and price > budget
                clicked = rng.random() < SE.ATTENTION ** k * SE.NOTICE * CLICK[g] * (0.6 if over else 1.0)
                a, b = rng.random(), rng.random()
                value = 1.0 if budget is None or price is None else max(0.5, min(1.5, (budget / price) ** 0.5))
                carted = clicked and a < min(1.0, BUY[g] * SE.CART * value * (SE.OVER_BUDGET if over else 1.0))
                bought = carted and b < SE.CHECKOUT
                if clicked:
                    call(ctx, rec, "outcome", {"request_id": res["request_id"], "event": "click", "product_id": prod})
                    n_click += 1
                if bought:
                    call(ctx, rec, "outcome", {"request_id": res["request_id"], "event": "purchase", "product_id": prod,
                                               "value": price or 0.0})
                    n_buy += 1
            if rng.random() >= SE.MORE:
                break
        if progress and i % 50 == 0:
            progress(i / int(sessions))
    call(ctx, rec, "flush", {"publish": True})
    _note(ctx, pid, f"sent {int(sessions):,} simulated shoppers at staging (SIMULATED)")
    if (get(ctx, pid) or {}).get("status") == "running":
        readout(ctx, pid)  # so the project's steps and Next line know where the test stands now
    return {"project": pid, "sessions": int(sessions), "requests": n_req, "clicks": n_click, "purchases": n_buy,
            "traffic": "simulated", "evidence": "SIMULATED", "seed": seed,
            "text": f"Sent {int(sessions):,} simulated shoppers at staging: {n_req:,} searches, {n_click:,} clicks, "
                    f"{n_buy:,} purchases. SIMULATED: no real shopper did any of this."}


def _is_version(ctx: Context, ref_: str) -> bool:
    try:
        ctx.registry.resolve("pipeline", ref_)
        return True
    except Exception:  # noqa: BLE001  an endpoint address or an image, not a version
        return False
