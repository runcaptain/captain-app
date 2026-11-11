"""Getting real traffic to a staged model, and seeing what it would change before any shopper does.

An A/B test means nothing until a site is calling the endpoint. These are the steps between "it runs" and "split
the traffic":

  connect   where the endpoint is and whether a site can reach it at all, how to call it (snippets for the two
            calls: get a ranking, report what the shopper did), and how many real requests have arrived.
  shadow    the site keeps showing the baseline's results while the candidate ranks the same requests silently.
            The readout says how often the two differ, on which queries, and what the candidate costs in
            response time. No shopper sees the candidate.
  differ    the same comparison on a sample of the project's own queries, for the Try it box, with one plain
            sentence when a candidate mostly reproduces the baseline and why.

An endpoint on this computer answers on 127.0.0.1 only, so no site can call it. That is said plainly, with the two
ways forward: stage it in your cloud, or keep testing here with simulated shoppers.
"""
from __future__ import annotations

import datetime as dt
import json

from .. import project_state as PS
from ..model.entities import new_id
from ..workspace import Context, whoami
from . import ENV, _note, _save, call, key_name, set_control, state
from . import logs as L

TIMEOUT_MS = 300  # what the snippets give the endpoint before the site falls back to its own search
SAME_SHARE = 0.8  # a candidate whose top 10 matches the baseline's on this share of queries "mostly reproduces" it
LABELS = {"real": ("REAL_TRAFFIC", "Real traffic"), "simulated": ("SIMULATED", "Simulated shoppers"),
          "test": ("TEST_TRAFFIC", "Test traffic")}
# What a signal is, in words a person who never saw the feature list can read.
SIGNALS = {"bm25_all": "how well its text matches the query's words", "title_overlap": "how many of the query's words are in its title",
           "title_partial": "query words that start a word in its title", "title_phrase": "the query appears as a phrase in its title",
           "title_cover": "how much of its title the query covers", "any_overlap": "query words found anywhere in its text",
           "query_len": "how long the query is", "title_len": "how long its title is", "brand_match": "the query names its brand",
           "price_log": "its price", "price_rank": "where its price sits in the catalog", "price_has_limit": "the query sets a price limit",
           "price_under_limit": "it is under the price the query names", "pop_impressions_log": "how often it was shown before",
           "pop_clicks_log": "how often it was clicked before", "pop_coec": "how often it was clicked, for where it was shown",
           "pop_purchase_rate": "how often it was bought when shown", "qp_impressions_log": "how often it was shown for this query before",
           "qp_coec": "how often it was clicked for this query before"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def signal_label(name: str) -> str:
    if name in SIGNALS:
        return SIGNALS[name]
    if name.startswith("bm25_"):
        return f"how well its {name[5:]} matches the query's words"
    if name.startswith("attr_") and name.endswith("_match"):
        return f"the query names its {name[5:-6]}"
    return name.replace("_", " ")


def _running(ctx: Context, pid: str, env: str) -> dict:
    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Staging is not running. Stage a model version first.")
    return rec


# ---- where it runs, and how a site calls it ------------------------------------------------------


def where(rec: dict) -> dict:
    """Where the staged model runs and who can reach it, in one sentence."""
    ep = rec.get("endpoint") or {}
    if rec.get("kind") == "local":
        return {"reachable": False, "address": ep.get("url"),
                "text": "Running on this computer. Only this computer can reach it."}
    return {"reachable": True, "address": ep.get("name"),
            "text": f"Running in your AWS account at {ep.get('name')}" + (f" ({ep.get('region')})" if ep.get("region") else "")
                    + ". Your site can call it."}


def snippets(rec: dict) -> dict:
    """Copy-paste code for the two calls a site makes: get a ranking, then report what the shopper did, with the
    request_id carried between them. The key is never in them: each reads it from the site's own environment."""
    ep = rec.get("endpoint") or {}
    fallback = ("If the endpoint doesn't answer in time, or answers with an error, show your own search results. "
                "A slow ranking must never hold up the page.")
    if rec.get("kind") == "local":
        url = ep.get("url")
        curl = f"""# Get a ranking. Keep the request_id it returns.
curl -s --max-time {TIMEOUT_MS / 1000:g} {url}/rank \\
  -H "Authorization: Bearer $CAPTAIN_STAGING_KEY" -H "Content-Type: application/json" \\
  -d '{{"query": "waterproof jacket", "k": 10, "session_id": "the-shopper-session"}}'

# Report what the shopper did: click, add-to-cart or purchase.
curl -s {url}/outcome \\
  -H "Authorization: Bearer $CAPTAIN_STAGING_KEY" -H "Content-Type: application/json" \\
  -d '{{"request_id": "<from /rank>", "event": "purchase", "product_id": "<the product>", "value": 59.00}}'"""
        js = f"""// {fallback}
async function rank(query, sessionId) {{
  try {{
    const r = await fetch("{url}/rank", {{
      method: "POST", signal: AbortSignal.timeout({TIMEOUT_MS}),
      headers: {{ Authorization: `Bearer ${{process.env.CAPTAIN_STAGING_KEY}}`, "Content-Type": "application/json" }},
      body: JSON.stringify({{ query, k: 10, session_id: sessionId }}),
    }});
    if (!r.ok) return null;            // an error body is {{"detail": "..."}}: fall back to your own search
    return await r.json();             // {{ request_id, ids, scores, arm, model_version, ms }}
  }} catch {{ return null; }}            // too slow or unreachable: fall back to your own search
}}

function outcome(requestId, event, productId, value) {{   // event: "click" | "add-to-cart" | "purchase"
  return fetch("{url}/outcome", {{
    method: "POST",
    headers: {{ Authorization: `Bearer ${{process.env.CAPTAIN_STAGING_KEY}}`, "Content-Type": "application/json" }},
    body: JSON.stringify({{ request_id: requestId, event, product_id: productId, value }}),
  }}).catch(() => {{}});
}}"""
        py = f"""# {fallback}
import os, requests

URL, KEY = "{url}", os.environ["CAPTAIN_STAGING_KEY"]
HEADERS = {{"Authorization": f"Bearer {{KEY}}"}}

def rank(query, session_id):
    try:
        r = requests.post(f"{{URL}}/rank", json={{"query": query, "k": 10, "session_id": session_id}},
                          headers=HEADERS, timeout={TIMEOUT_MS / 1000:g})
        return r.json() if r.ok else None   # an error body is {{"detail": "..."}}: fall back to your own search
    except requests.RequestException:
        return None                         # too slow or unreachable: fall back to your own search

def outcome(request_id, event, product_id=None, value=0.0):   # event: click, add-to-cart or purchase
    try:
        requests.post(f"{{URL}}/outcome", json={{"request_id": request_id, "event": event, "product_id": product_id,
                                               "value": value}}, headers=HEADERS, timeout=2)
    except requests.RequestException:
        pass"""
        return {"curl": curl, "javascript": js, "python": py}
    name, region = ep.get("name"), ep.get("region") or "us-east-1"
    cli = f"""# Get a ranking. Keep the request_id it returns. SageMaker signs the call with your AWS credentials.
aws sagemaker-runtime invoke-endpoint --region {region} --endpoint-name {name} \\
  --content-type application/json --cli-read-timeout 1 --cli-binary-format raw-in-base64-out \\
  --body '{{"op": "rank", "key": "'"$CAPTAIN_STAGING_KEY"'", "query": "waterproof jacket", "k": 10, "session_id": "the-shopper-session"}}' /dev/stdout

# Report what the shopper did: click, add-to-cart or purchase.
aws sagemaker-runtime invoke-endpoint --region {region} --endpoint-name {name} \\
  --content-type application/json --cli-binary-format raw-in-base64-out \\
  --body '{{"op": "outcome", "key": "'"$CAPTAIN_STAGING_KEY"'", "request_id": "<from rank>", "event": "purchase", "product_id": "<the product>", "value": 59.00}}' /dev/stdout"""
    js = f"""// {fallback}
import {{ SageMakerRuntimeClient, InvokeEndpointCommand }} from "@aws-sdk/client-sagemaker-runtime";
const client = new SageMakerRuntimeClient({{ region: "{region}" }});
const call = async (body, ms) => {{
  const r = await client.send(new InvokeEndpointCommand({{ EndpointName: "{name}", ContentType: "application/json",
    Body: JSON.stringify({{ key: process.env.CAPTAIN_STAGING_KEY, ...body }}) }}), {{ abortSignal: AbortSignal.timeout(ms) }});
  return JSON.parse(new TextDecoder().decode(r.Body));
}};

async function rank(query, sessionId) {{
  try {{ return await call({{ op: "rank", query, k: 10, session_id: sessionId }}, {TIMEOUT_MS}); }}   // {{ request_id, ids, ... }}
  catch {{ return null; }}   // too slow, unreachable or refused: fall back to your own search
}}

function outcome(requestId, event, productId, value) {{   // event: "click" | "add-to-cart" | "purchase"
  return call({{ op: "outcome", request_id: requestId, event, product_id: productId, value }}, 2000).catch(() => {{}});
}}"""
    py = f"""# {fallback}
import json, os, boto3
from botocore.config import Config

rt = boto3.client("sagemaker-runtime", region_name="{region}",
                  config=Config(read_timeout={TIMEOUT_MS / 1000:g}, connect_timeout={TIMEOUT_MS / 1000:g}, retries={{"max_attempts": 0}}))

def _call(body):
    r = rt.invoke_endpoint(EndpointName="{name}", ContentType="application/json",
                           Body=json.dumps({{"key": os.environ["CAPTAIN_STAGING_KEY"], **body}}))
    return json.loads(r["Body"].read())

def rank(query, session_id):
    try:
        return _call({{"op": "rank", "query": query, "k": 10, "session_id": session_id}})   # request_id, ids, ...
    except Exception:
        return None   # too slow, unreachable or refused: fall back to your own search

def outcome(request_id, event, product_id=None, value=0.0):   # event: click, add-to-cart or purchase
    try:
        _call({{"op": "outcome", "request_id": request_id, "event": event, "product_id": product_id, "value": value}})
    except Exception:
        pass"""
    return {"aws cli": cli, "javascript": js, "python": py}


def _ago(iso: str | None) -> float | None:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None


def _since(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s // 3600} h"


def connect(ctx: Context, pid: str, env: str = ENV) -> dict:
    """What "Connect your site" shows: the address, whether a site can reach it, the snippets, and how many real
    requests have arrived. Counts real traffic only: the console and simulated shoppers are not a site."""
    rec = _running(ctx, pid, env)
    w = where(rec)
    out = {"project": pid, "env": env, **w, "kind": rec.get("kind"), "key_name": key_name(pid, env),
           "key_note": ("The key is kept in this computer's keychain. Copy it from here and give it to your site as "
                        "CAPTAIN_STAGING_KEY. It is never shown on screen or written to a log."),
           "timeout_ms": TIMEOUT_MS,
           "fallback": (f"Give the endpoint {TIMEOUT_MS} ms. If it doesn't answer in time, or answers with an error, "
                        "show your own search results. An error body is {\"detail\": \"...\"} with a 4xx or 5xx status."),
           "snippets": snippets(rec) if rec.get("how") != "image" else {}, "real": 0, "test": 0, "simulated": 0,
           "last_real_at": None}
    try:
        h = call(ctx, rec, "status", {}) if rec.get("how") != "image" else {}
        t = h.get("traffic") or {}
        out.update(real=int(t.get("real") or 0), test=int(t.get("test") or 0), simulated=int(t.get("simulated") or 0),
                   last_real_at=h.get("last_real_at"))
    except Exception as e:  # noqa: BLE001
        out["warning"] = f"Captain couldn't reach the endpoint: {str(e)[:160]}"
    ago = _ago(out["last_real_at"])
    out["connected"] = out["real"] > 0
    out["live"] = (f"Received {out['real']:,} request{'s' if out['real'] != 1 else ''}"
                   + (f", the last one {_since(ago)} ago" if ago is not None else "") + "."
                   if out["real"] else "Waiting for the first request.")
    if not w["reachable"]:
        out["blocked"] = ("A site can't reach an endpoint on this computer. Two ways forward: stage it in your cloud, "
                          "so your site can call it, or keep testing here with simulated shoppers.")
    if out["connected"] and not rec.get("connected_at"):
        _save(ctx, pid, env, connected_at=_now())
        _note(ctx, pid, "the first real request reached staging")
    return out


def key_for_copy(ctx: Context, pid: str, env: str = ENV) -> dict:
    """The endpoint key, for the app's Copy button only. It goes to the clipboard, never to the screen or a log."""
    from . import _key

    rec = _running(ctx, pid, env)
    k = _key(rec)
    if not k:
        raise ValueError("This endpoint has no key of Captain's: it is your own container.")
    return {"key": k, "name": "CAPTAIN_STAGING_KEY"}


def test_request(ctx: Context, pid: str, query: str | None = None, env: str = ENV) -> dict:
    """One request through the same door a site uses, marked as test traffic, to prove the path works."""
    rec = _running(ctx, pid, env)
    q = query or "test"
    r = call(ctx, rec, "rank", {"query": q, "k": 5, "traffic": "test", "session_id": "test-" + new_id().lower()[-6:]})
    return {"ok": True, "request_id": r["request_id"], "results": len(r["ids"]), "ms": r["ms"], "arm": r["arm"],
            "text": f"The endpoint answered in {r['ms']:.0f} ms with {len(r['ids'])} results. This was test traffic: it isn't counted as a shopper."}


# ---- shadow mode ---------------------------------------------------------------------------------


def shadow_get(ctx: Context, pid: str) -> dict | None:
    return PS.get(ctx, pid).get("shadow") or None


def _shadow_put(ctx: Context, pid: str, value: dict | None) -> None:
    def ch(doc):
        doc["shadow"] = value

    PS._update(ctx, pid, ch)


def shadow_start(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Answer every request with the baseline's ranking, and rank it with the candidate too, silently."""
    rec = _running(ctx, pid, env)
    arms = rec.get("arms") or {}
    if not {"baseline", "candidate"} <= set(arms) or rec.get("how") == "image":
        raise ValueError("Shadow mode needs the candidate and the baseline on the endpoint. Stage a candidate while "
                         "the project has a baseline.")
    ab = PS.get(ctx, pid).get("ab") or {}
    if ab.get("status") == "running" and ab.get("mode") == "captain":
        raise ValueError("An A/B test is running. Shadow mode comes before a split: end or roll back the test first.")
    cur = shadow_get(ctx, pid)
    if cur and cur.get("status") == "running" and cur.get("stage") == rec["id"]:
        return cur
    sh = {"id": "sh-" + new_id().lower()[-10:], "status": "running", "env": env, "stage": rec["id"], "arms": dict(arms),
          "started_at": _now(), "started_by": whoami(), "ended_at": None}
    set_control(ctx, pid, env, shadow=sh["id"], split=None, experiment=None, serve="baseline")
    call(ctx, rec, "reload", {})
    _save(ctx, pid, env, serve="baseline", model_version=arms["baseline"])
    _shadow_put(ctx, pid, sh)
    _note(ctx, pid, f"started shadow mode: shoppers see {arms['baseline']}, and {arms['candidate']} ranks the same requests silently")
    return sh


def shadow_stop(ctx: Context, pid: str) -> dict:
    sh = shadow_get(ctx, pid)
    if not sh:
        raise ValueError("Shadow mode was never started for this project.")
    if sh["status"] != "running":
        return sh
    rec = state(ctx, pid, sh["env"])
    if rec and rec.get("status") == "running" and rec.get("id") == sh["stage"]:
        set_control(ctx, pid, sh["env"], shadow=None)
        call(ctx, rec, "reload", {})
    sh = {**sh, "status": "ended", "ended_at": _now()}
    _shadow_put(ctx, pid, sh)
    _note(ctx, pid, "ended shadow mode")
    return sh


def _overlap(a: list, b: list, k: int = 10) -> float:
    a, b = a[:k], b[:k]
    return len(set(a) & set(b)) / max(1, max(len(a), len(b)))


def _p95(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2) if xs else None


def shadow_compute(sh: dict, got: dict, traffic: str | None = None) -> dict:
    """The shadow readout from a staging log. Pure. One kind of traffic at a time, strongest first."""
    reqs = [r for r in got["requests"] if (r.get("shadow") or {}).get("id") == sh["id"]]
    kinds: dict[str, int] = {}
    for r in reqs:
        kinds[r.get("traffic") or "real"] = kinds.get(r.get("traffic") or "real", 0) + 1
    kind = traffic or next((k for k in ("real", "simulated", "test") if kinds.get(k)), "real")
    reqs = [r for r in reqs if (r.get("traffic") or "real") == kind]
    evidence, label = LABELS[kind]
    n = len(reqs)
    out = {"shadow": sh["id"], "status": sh["status"], "arms": sh["arms"], "traffic": kind, "evidence": evidence,
           "label": label, "requests": n, "other_traffic": {k: v for k, v in kinds.items() if k != kind}, "queries": []}
    if not n:
        return {**out, "top1_differs": None, "top10_differs": None, "overlap": None, "ms": None,
                "text": f"{label}. No requests yet. The candidate has ranked nothing, and no shopper has seen it."}
    top1 = sum(1 for r in reqs if (r["ids"][:1] != r["shadow"]["ids"][:1]))
    top10 = sum(1 for r in reqs if r["ids"][:10] != r["shadow"]["ids"][:10])
    ov = sum(_overlap(r["ids"], r["shadow"]["ids"]) for r in reqs) / n
    per: dict[str, list] = {}
    for r in reqs:
        d = per.setdefault(" ".join(str(r["query"]).lower().split()), [0, 0.0, 0])
        d[0] += 1
        d[1] += 1 - _overlap(r["ids"], r["shadow"]["ids"])
        d[2] += r["ids"][:10] != r["shadow"]["ids"][:10]
    worst = sorted(({"query": q, "requests": c, "differs": round(d / c, 3), "top10_differs": bool(t)}
                    for q, (c, d, t) in per.items() if t), key=lambda x: (-x["differs"], -x["requests"], x["query"]))[:10]
    errors = sum(1 for r in reqs if r["shadow"].get("error"))
    ms = {"baseline": {"p95": _p95([r.get("ms") for r in reqs]), "mean": round(sum(r.get("ms") or 0 for r in reqs) / n, 2)},
          "candidate": {"p95": _p95([r["shadow"].get("ms") for r in reqs]),
                        "mean": round(sum(r["shadow"].get("ms") or 0 for r in reqs) / n, 2)}}
    text = (f"{label}. On {n:,} request{'s' if n != 1 else ''} the candidate would have changed the top result on "
            f"{top1 / n:.0%} and the top 10 on {top10 / n:.0%}. On average {ov:.0%} of the top 10 is the same. "
            f"No shopper saw the candidate.")
    if not top10:
        text = (f"{label}. On {n:,} request{'s' if n != 1 else ''} the candidate returned exactly what the baseline "
                "returned. No shopper saw the candidate.")
    return {**out, "top1_differs": round(top1 / n, 4), "top10_differs": round(top10 / n, 4), "overlap": round(ov, 4),
            "queries": worst, "ms": ms, "candidate_errors": errors, "text": text}


def shadow(ctx: Context, pid: str, traffic: str | None = None) -> dict:
    sh = shadow_get(ctx, pid)
    if not sh:
        return {"project": pid, "status": "none", "shadow": None,
                "text": "Shadow mode is off. In shadow mode your site keeps showing the baseline's results while the "
                        "candidate ranks the same requests silently."}
    rec = state(ctx, pid, sh["env"])
    if rec and rec.get("status") == "running":
        try:
            call(ctx, rec, "flush", {})
        except Exception:  # noqa: BLE001
            pass
    return {"project": pid, "started_at": sh["started_at"], **shadow_compute(sh, L.read(ctx, pid, sh["env"], sh["stage"]), traffic)}


# ---- how different is the candidate, on the project's own queries ---------------------------------


def sample_queries(ctx: Context, pid: str, n: int = 40) -> list[str]:
    """Queries to compare two versions on: the ones the project's log searches most, then its pinned queries."""
    from ..ops import rows_of

    doc = PS.get(ctx, pid)
    out: list[str] = []
    ev = PS.pinned(doc, "events")
    if ev:
        try:
            _, rows, _ = rows_of(ctx, ev)
            seen: dict[str, set] = {}
            for r in rows:
                seen.setdefault(" ".join(str(r.get("query") or "").lower().split()), set()).add(r.get("session_id") or r.get("ts"))
            out += [q for _, q in sorted(((len(v), q) for q, v in seen.items() if q), reverse=True)]
        except Exception:  # noqa: BLE001
            pass
    for role in ("holdout", "queries"):
        ref_ = PS.pinned(doc, role)
        if ref_ and len(out) < n:
            try:
                _, rows, _ = rows_of(ctx, ref_)
                out += [" ".join(str(r.get("query") or "").lower().split()) for r in rows if r.get("query")]
            except Exception:  # noqa: BLE001
                pass
    return list(dict.fromkeys(q for q in out if q))[:n]


def _reason(ctx: Context, pipeline: str, exp: dict | None) -> str:
    """Why a candidate would mostly reproduce the baseline, from what is known about how it was trained."""
    simulated = "simulated" in str((exp or {}).get("note") or "").lower()
    try:
        ts = (exp or {}).get("training_set")
        if ts:
            m, _ = ctx.registry.resolve("dataset", ts)
            simulated = simulated or str((m.tags or {}).get("synthetic")).lower() == "true" or bool((m.provenance.params or {}).get("summary", {}).get("simulated"))
    except Exception:  # noqa: BLE001
        pass
    if simulated:
        return "it learned from events that keyword search generated"
    try:
        from ..engines import artifact_reader
        from ..features import model as FM

        pm, _ = ctx.registry.resolve("pipeline", pipeline)
        rr = (pm.spec or {}).get("rerank") or {}
        if not rr.get("model"):
            return "it has no trained model yet, so it ranks in keyword order"
        imp = FM.load(artifact_reader(ctx, pm)(rr["model"])).importance()
        hist = sum(v for k, v in imp.items() if k.startswith(("pop_", "qp_")))
        kw = sum(v for k, v in imp.items() if k.startswith(("bm25_", "title_", "any_")))
        if hist >= 0.5:
            return "most of what it learned is how often a product was shown before, which is zero for a query the logs never saw"
        if kw >= 0.5:
            return "most of what it learned is the keyword score itself"
    except Exception:  # noqa: BLE001
        pass
    return "on these queries its model changes nothing"


def compare_offline(ctx: Context, pid: str, candidate: str, n: int = 40, k: int = 10) -> dict:
    """How often a candidate's top results match the baseline's on a sample of the project's queries, computed here
    with both versions' own files (no endpoint). {checked, same, same_share, mostly_same, text, queries}."""
    from .. import ops
    from ..engines import artifact_reader, make_engine

    doc = PS.get(ctx, pid)
    champ = doc.get("champion")
    exp = next((e for e in doc.get("experiments") or [] if e["id"] == candidate or e["pipeline"] == candidate), None)
    cand = exp["pipeline"] if exp else candidate
    if not champ or champ == cand:
        return {"checked": 0, "same": 0, "same_share": None, "mostly_same": False, "text": None, "queries": []}
    qs = sample_queries(ctx, pid, n)
    engines = {}
    for name, r in (("baseline", champ), ("candidate", cand)):
        pm, _ = ctx.registry.resolve("pipeline", r)
        engines[name] = make_engine(pm.engine, pm.spec, ops.products_of(ctx, pm.catalog) if pm.catalog else None,
                                    artifact=artifact_reader(ctx, pm))
    rows = [(q, engines["baseline"].search(q, k), engines["candidate"].search(q, k)) for q in qs]
    return _differ_out(ctx, cand, exp, rows)


def _differ_out(ctx: Context, cand: str, exp: dict | None, rows: list[tuple[str, list, list]]) -> dict:
    checked = len(rows)
    same = sum(1 for _, b, c in rows if b == c)
    diff = sorted(({"query": q, "differs": round(1 - _overlap(c, b), 3), "moved": sum(1 for i, x in enumerate(c) if i >= len(b) or b[i] != x)}
                   for q, b, c in rows if b != c), key=lambda x: (-x["differs"], -x["moved"], x["query"]))[:10]
    share = same / checked if checked else None
    mostly = bool(checked >= 5 and share is not None and share >= SAME_SHARE)
    text = None
    if mostly:
        text = (f"This candidate mostly reproduces the baseline: {_reason(ctx, cand, exp)}. "
                f"It returned the same top results on {same} of {checked} queries checked.")
    elif checked:
        text = f"It ranks differently from the baseline on {checked - same} of {checked} queries checked."
    return {"checked": checked, "same": same, "same_share": round(share, 4) if share is not None else None,
            "mostly_same": mostly, "text": text, "queries": diff,
            "none_differ": (f"None of the {checked} queries checked rank differently. {text}" if checked and not diff else None)}


def differ(ctx: Context, pid: str, env: str = ENV, n: int = 40) -> dict:
    """"Queries where these two differ most", for the Try it box: the staged candidate against the baseline on a
    sample of the project's own queries."""
    rec = _running(ctx, pid, env)
    arms = rec.get("arms") or {}
    if not {"baseline", "candidate"} <= set(arms):
        return {"checked": 0, "same": 0, "same_share": None, "mostly_same": False, "text": None, "queries": [], "none_differ": None}
    doc = PS.get(ctx, pid)
    exp = next((e for e in doc.get("experiments") or [] if e["pipeline"] == arms["candidate"]), None)
    try:
        out = compare_offline(ctx, pid, arms["candidate"], n)
    except Exception:  # noqa: BLE001  a wrapped endpoint has no files here: ask the endpoint itself
        rows = []
        for q in sample_queries(ctx, pid, min(n, 20)):
            b = call(ctx, rec, "rank", {"query": q, "k": 10, "traffic": "test"}, arm="baseline")["ids"]
            c = call(ctx, rec, "rank", {"query": q, "k": 10, "traffic": "test"}, arm="candidate")["ids"]
            rows.append((q, b, c))
        out = _differ_out(ctx, arms["candidate"], exp, rows)
    if exp and out["checked"]:
        try:
            PS.experiment_record(ctx, pid, exp["id"], sameness={k: out[k] for k in ("checked", "same", "same_share", "mostly_same", "text")})
        except Exception:  # noqa: BLE001
            pass
    return out
