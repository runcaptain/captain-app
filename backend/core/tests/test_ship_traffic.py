"""Getting real traffic to a staged model: where it runs and who can reach it, the request counters, shadow mode,
an A/B form that only opens once a site is calling, how a test ends, and saying so when a candidate just
reproduces keyword search. All on this computer."""
import json

import pytest
from typer.testing import CliRunner

from captain_search import ops
from captain_search import project_state as PS
from captain_search import staging as SG
from captain_search import starter as ST
from captain_search.cli.main import app
from captain_search.staging import ab as AB
from captain_search.staging import logs as L
from captain_search.staging import traffic as TF

from test_staging import _log, _runtime, keychain, ws  # noqa: F401  (fixtures and helpers)


def _real(ctx, n=30, start=0):
    rec = SG.state(ctx, "shop")
    qs = ["jacket", "boots", "tent", "waterproof jacket", "boots under 50"]
    return [SG.call(ctx, rec, "rank", {"traffic": "real", "query": qs[i % len(qs)], "k": 10, "session_id": f"shopper-{start + i}"}) for i in range(n)]


# ---- where it runs, and connecting a site --------------------------------------------------------


def test_the_stage_sentence_says_where_it_runs_and_who_can_reach_it(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    assert "is running on this computer. Only this computer can reach it." in SG.status(ws, "shop")["text"]
    assert TF.where({"kind": "aws-sagemaker", "endpoint": {"name": "captain-shop-staging", "region": "us-west-1"}}) == {
        "reachable": True, "address": "captain-shop-staging",
        "text": "Running in your AWS account at captain-shop-staging (us-west-1). Your site can call it."}
    step = PS.stages(ws, "shop")["ship"]["steps"][0]
    assert step["status"].endswith("is running on this computer. Only this computer can reach it.")


def test_connect_counts_only_real_requests_and_never_shows_the_key(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    c = TF.connect(ws, "shop")
    assert not c["reachable"] and not c["connected"] and c["live"] == "Waiting for the first request."
    assert "A site can't reach an endpoint on this computer" in c["blocked"] and "deploy it to a cloud" in c["blocked"]
    key = SG._key(SG.state(ws, "shop"))
    text = json.dumps(c)
    assert key not in text and "CAPTAIN_STAGING_KEY" in text and "keychain" in c["key_note"]
    # snippets for the two calls, with a timeout and the fallback said in them
    assert set(c["snippets"]) == {"curl", "javascript", "python"}
    for code in c["snippets"].values():
        assert "/rank" in code and "/outcome" in code and "request_id" in code and "CAPTAIN_STAGING_KEY" in code
    assert "--max-time 0.3" in c["snippets"]["curl"] and "AbortSignal.timeout(300)" in c["snippets"]["javascript"]
    assert "timeout=0.3" in c["snippets"]["python"] and "fall back to your own search" in c["snippets"]["python"]
    assert "300 ms" in c["fallback"] and '{"detail": "..."}' in c["fallback"]

    # the console and a test request are not a site: still waiting
    SG.query(ws, "shop", "jacket")
    t = TF.test_request(ws, "shop", "boots")
    assert t["ok"] and t["results"] == 5 and "test traffic" in t["text"]
    c = TF.connect(ws, "shop")
    assert c["real"] == 0 and c["test"] == 3 and c["live"] == "Waiting for the first request." and not c["connected"]
    assert PS.stages(ws, "shop")["ship"]["steps"][2]["id"] == "connect" and not PS.stages(ws, "shop")["ship"]["steps"][2]["done"]
    # simulated shoppers neither
    rec = SG.state(ws, "shop")
    SG.call(ws, rec, "rank", {"query": "tent", "traffic": "simulated", "session_id": "sim-1"})
    assert TF.connect(ws, "shop")["simulated"] == 1 and not TF.connect(ws, "shop")["connected"]
    # a real request: counted, with when
    _real(ws, 12)
    c = TF.connect(ws, "shop")
    assert c["blocked"].startswith("These requests came from this computer.")
    assert c["connected"] and c["real"] == 12 and c["live"].startswith("Received 12 requests, the last one ") and c["live"].endswith(" s ago.")
    assert SG.state(ws, "shop")["connected_at"] and PS.stages(ws, "shop")["ship"]["steps"][2]["done"]
    assert "the first real request reached staging" in " | ".join(h["what"] for h in PS.get(ws, "shop")["history"])
    assert TF.key_for_copy(ws, "shop") == {"key": key, "name": "CAPTAIN_STAGING_KEY"}  # for the clipboard only

    out = CliRunner().invoke(app, ["projects", "staging-connect", "shop"])
    assert out.exit_code == 0 and "Received 12 requests" in out.output and key not in out.output
    code = CliRunner().invoke(app, ["projects", "staging-connect", "shop", "--code", "python"])
    assert code.exit_code == 0 and "def rank(query, session_id)" in code.output and key not in code.output
    assert CliRunner().invoke(app, ["projects", "staging-test", "shop"]).exit_code == 0


def test_cloud_snippets_call_sagemaker_with_a_timeout():
    s = TF.snippets({"kind": "aws-sagemaker", "endpoint": {"name": "captain-shop-staging", "region": "us-west-1"}})
    assert set(s) == {"aws cli", "javascript", "python"}
    assert "invoke-endpoint --region us-west-1 --endpoint-name captain-shop-staging" in s["aws cli"]
    assert '"op": "rank"' in s["python"] and '"op": "outcome"' in s["python"] and "read_timeout=0.3" in s["python"]
    assert "InvokeEndpointCommand" in s["javascript"] and "fall back to your own search" in s["javascript"]


# ---- shadow mode ---------------------------------------------------------------------------------


def test_shadow_mode_answers_with_the_baseline_and_logs_what_the_candidate_would_do(ws):  # noqa: F811
    assert TF.shadow(ws, "shop")["status"] == "none"
    SG.stage(ws, "shop", baseline=True, target="local")
    with pytest.raises(ValueError, match="needs the candidate and the baseline"):
        TF.shadow_start(ws, "shop")
    SG.stage(ws, "shop", target="local")
    sh = TF.shadow_start(ws, "shop")
    assert sh["status"] == "running" and SG.control(ws, "shop")["shadow"] == sh["id"]
    assert SG.state(ws, "shop")["model_version"] == ws.candidate  # what is staged doesn't change, only who answers
    assert "Shadow mode is on: every request is answered with the baseline's results." in SG.status(ws, "shop")["text"]
    assert TF.shadow(ws, "shop")["requests"] == 0 and "No shopper saw the candidate" not in TF.shadow(ws, "shop")["text"]

    # every shopper gets the baseline's ranking, exactly
    from captain_search.engines import make_engine

    pm, _ = ws.registry.resolve("pipeline", ws.baseline)
    base = make_engine(pm.engine, pm.spec, ops.products_of(ws, pm.catalog))
    got = _real(ws, 30)
    for i, r in enumerate(got):
        q = ["jacket", "boots", "tent", "waterproof jacket", "boots under 50"][i % 5]
        assert r["arm"] == "baseline" and r["model_version"] == ws.baseline and r["ids"] == base.search(q, 10)
    SG.query(ws, "shop", "jacket")  # the console asks one arm by name: not shadowed, not counted

    r = TF.shadow(ws, "shop")
    assert r["requests"] == 30 and r["evidence"] == "REAL_TRAFFIC" and r["label"] == "Real traffic"
    assert 0 <= r["top1_differs"] <= r["top10_differs"] <= 1 and 0 < r["overlap"] <= 1
    assert r["ms"]["baseline"]["p95"] is not None and r["ms"]["candidate"]["p95"] is not None
    assert r["text"].startswith("Real traffic. On 30 requests ") and "No shopper saw the candidate." in r["text"]
    logged = [x for x in L.read(ws, "shop", "staging", SG.state(ws, "shop")["id"])["requests"] if x.get("shadow")]
    assert len(logged) == 30 and all(x["arm"] == "baseline" and x["shadow"]["model_version"] == ws.candidate for x in logged)
    if r["top10_differs"]:
        assert r["queries"] and r["queries"][0]["differs"] >= r["queries"][-1]["differs"] and r["queries"][0]["query"] in ("jacket", "boots", "tent", "waterproof jacket", "boots under 50")

    run = CliRunner()
    out = run.invoke(app, ["projects", "shadow", "shop"])
    assert out.exit_code == 0 and "Real traffic. On 30 requests" in out.output and "Response time" in out.output

    # a split ends shadow mode: it comes before, never during
    AB.start(ws, "shop", split="5%")
    assert TF.shadow_get(ws, "shop")["status"] == "ended" and SG.control(ws, "shop")["shadow"] is None
    with pytest.raises(ValueError, match="Shadow mode comes before a split"):
        TF.shadow_start(ws, "shop")
    AB.rollback(ws, "shop")
    assert run.invoke(app, ["projects", "shadow-start", "shop"]).exit_code == 0
    assert run.invoke(app, ["projects", "shadow-stop", "shop"]).exit_code == 0
    assert SG.control(ws, "shop")["shadow"] is None


def test_shadow_readout_numbers_and_labels():
    sh = {"id": "sh-1", "status": "running", "arms": {"baseline": "b@v1", "candidate": "c@v1"}}

    def req(i, q, base, cand, traffic="real", ms=(4.0, 9.0)):
        return {"request_id": f"r{i}", "query": q, "ids": base, "ms": ms[0], "traffic": traffic,
                "shadow": {"id": "sh-1", "ids": cand, "ms": ms[1], "model_version": "c@v1"}}

    same = list("abcdefghij")
    swapped = ["b", "a"] + same[2:]          # top result differs, same ten products
    other = same[:5] + list("vwxyz")         # top result the same, half the page different
    reqs = [req(i, "jacket", same, same) for i in range(6)] + [req(10 + i, "boots", same, swapped) for i in range(2)] \
        + [req(20 + i, "tent", same, other) for i in range(2)]
    r = TF.shadow_compute(sh, {"requests": reqs, "outcomes": [], "names": {}})
    assert r["requests"] == 10 and r["top1_differs"] == 0.2 and r["top10_differs"] == 0.4 and r["overlap"] == 0.9
    assert [q["query"] for q in r["queries"]] == ["tent", "boots"] and r["queries"][0]["differs"] == 0.5 and r["queries"][1]["differs"] == 0.0
    # the same ten in another order: no other products, but two places moved. Said as places, never as "0% differs".
    assert r["queries"][1]["moved"] == 2 and r["queries"][0]["moved"] == 5
    assert r["ms"] == {"baseline": {"p95": 4.0, "mean": 4.0}, "candidate": {"p95": 9.0, "mean": 9.0}}
    assert "changed the top result on 20% and the top 10 on 40%" in r["text"] and "90% of the top 10 is the same" in r["text"]
    # identical everywhere: said plainly
    r = TF.shadow_compute(sh, {"requests": reqs[:6], "outcomes": [], "names": {}})
    assert r["top10_differs"] == 0 and r["queries"] == [] and "returned exactly what the baseline returned" in r["text"]
    # kinds of traffic are never mixed, and only real requests read REAL_TRAFFIC
    mixed = reqs + [req(100 + i, "tent", same, other, "simulated") for i in range(50)]
    assert TF.shadow_compute(sh, {"requests": mixed, "outcomes": [], "names": {}})["requests"] == 10
    sim = TF.shadow_compute(sh, {"requests": mixed, "outcomes": [], "names": {}}, "simulated")
    assert sim["evidence"] == "SIMULATED" and sim["requests"] == 50 and sim["text"].startswith("Simulated shoppers. ")
    assert TF.shadow_compute(sh, {"requests": [], "outcomes": [], "names": {}})["text"].startswith("No traffic yet. No requests yet.")  # never "real" before a request
    # two versions that both find nothing agree: an empty page is not a difference
    empty = TF.shadow_compute(sh, {"requests": [req(1, "nothing here", [], [])], "outcomes": [], "names": {}})
    assert empty["overlap"] == 1.0 and empty["top10_differs"] == 0


# ---- the A/B form: gated, honest, and how it ends ------------------------------------------------


def test_the_ab_form_opens_only_once_a_site_is_calling_and_says_what_a_share_means(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    o = AB.options(ws, "shop")
    assert not o["ready"] and o["real"] == 0 and "a split would divide nothing" in o["text"]
    assert [s["text"] for s in o["shares"]] == ["1% of sessions", "5% of sessions", "25% of sessions", "50% of sessions"]
    with pytest.raises(ValueError, match="nothing to split. Connect your site first"):
        AB.start(ws, "shop")
    SG.query(ws, "shop", "jacket")
    AB.simulate(ws, "shop", sessions=20)
    assert not AB.options(ws, "shop")["ready"]  # the console and simulated shoppers don't open it
    _real(ws, 40)
    o = AB.options(ws, "shop")
    assert o["ready"] and o["real"] == 40 and o["sessions"] == 40 and o["per_day"] == 960  # 40 sessions in under an hour
    assert o["shares"][1] == {"split": 0.05, "label": "5%", "text": "5%: about 48 of the 960 sessions a day this endpoint sees"}
    ab = AB.start(ws, "shop")
    assert ab["split"] == 0.05 and not ab["practice"]  # the default share matches the form: 5%


def test_practice_on_simulated_shoppers_is_labelled_and_cannot_ship(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    ab = AB.start(ws, "shop", split=0.5, primary="clicks", min_sessions=50, practice=True)
    assert ab["practice"]
    empty = AB.readout(ws, "shop")
    assert empty["evidence"] is None and empty["label"] == "No traffic yet" and empty["text"].startswith("No traffic yet. ")
    AB.simulate(ws, "shop", sessions=200)
    r = AB.readout(ws, "shop")
    assert r["practice"] and r["evidence"] == "SIMULATED" and r["enough"] and not r["can_ship"]
    assert r["cannot_ship"] == "This is a practice test on simulated shoppers. It can't go to launch review."
    with pytest.raises(ValueError, match="practice test on simulated shoppers"):
        AB.decide(ws, "shop", "ship")
    assert AB.decide(ws, "shop", "keep-testing")["decision"]["label"] == "Keep testing" and AB.get(ws, "shop")["status"] == "running"
    with pytest.raises(ValueError, match="the choice is one of"):
        AB.decide(ws, "shop", "maybe")


def test_sessions_still_needed_and_days_at_this_rate():
    ab = {"id": "ab-1", "status": "running", "mode": "captain", "split": 0.5, "primary": "clicks", "guardrails": [],
          "min_sessions": 100, "arms": {"baseline": "b@v1", "candidate": "c@v1"}, "ramps": []}
    # 20% against 22% clicks on 500 sessions an arm: a 2 point difference nobody can tell from chance yet
    rows = [("baseline", f"b{i}", "real", i % 5 == 0, 0) for i in range(500)] + [("candidate", f"c{i}", "real", i % 50 < 11, 0) for i in range(500)]
    out = AB.compute(ab, _log(ab, rows))
    assert out["primary"]["verdict"] == "no clear difference" and abs(out["primary"]["diff"] - 0.02) < 1e-9
    n = AB.needed(ab, out, per_day=1000)
    # the textbook number: 2 * (1.96 + 0.84)^2 * p(1-p) / 0.02^2 with p about 0.21, per arm
    assert 6300 < n["per_arm"] < 6700 and n["more"] == n["per_arm"] - 500 and n["difference"] == 0.02
    assert n["days"] == -(-n["more"] // 500)  # half the sessions a day go to each arm
    assert n["text"] == f"About {n['more']:,} more sessions needed in the smaller arm to tell a difference of 2.0 points, roughly {n['days']} days at this rate."
    # a 5% share fills the candidate arm ten times more slowly
    assert AB.needed({**ab, "split": 0.05}, out, per_day=1000)["days"] == -(-n["more"] // 50)
    assert AB.needed(ab, out, per_day=None)["text"].endswith("to tell a difference of 2.0 points.")  # no rate known: no days
    # once the difference is clear there is nothing more to need
    clear = AB.compute(ab, _log(ab, [("baseline", f"b{i}", "real", i % 5 == 0, 0) for i in range(1000)] + [("candidate", f"c{i}", "real", i % 10 < 3, 0) for i in range(1000)]))
    assert AB.needed(ab, clear, 1000) is None


def test_ship_it_takes_the_readout_and_its_label_to_launch_review(ws):  # noqa: F811
    PS.experiment_backtest(ws, "shop", ws.exp)
    SG.stage(ws, "shop", target="local")
    _real(ws, 5)
    AB.start(ws, "shop", split=0.5, primary="clicks", min_sessions=20)
    with pytest.raises(ValueError, match="Not enough data yet"):
        AB.decide(ws, "shop", "ship")
    rec = SG.state(ws, "shop")
    for r in _real(ws, 120, start=100):
        if r["arm"] == "candidate":
            SG.call(ws, rec, "outcome", {"request_id": r["request_id"], "event": "click", "product_id": r["ids"][0]})
    ro = AB.readout(ws, "shop")
    assert ro["evidence"] == "REAL_TRAFFIC" and ro["enough"] and ro["can_ship"] and ro["cannot_ship"] is None
    assert ro["primary"]["verdict"] == "candidate ahead" and ro["needed"] is None
    d = AB.decide(ws, "shop", "ship", note="clicks are clearly up")
    assert d["decision"]["choice"] == "ship" and d["decision"]["evidence"] == "REAL_TRAFFIC"
    assert d["launch_review"] == {"pipeline": ws.candidate, "readout": ro["text"], "evidence": "REAL_TRAFFIC"}
    assert AB.readout(ws, "shop")["decision"]["label"] == "Ship it"
    assert "decided on the A/B test: ship it (clicks are clearly up)" in " | ".join(h["what"] for h in PS.get(ws, "shop")["history"])
    # launch review: the readout rides with the candidate, labelled
    ops.env_create(ws, "prod", "live", {"type": "record"})
    dep = ops.promote(ws, ws.candidate, "prod", confirm=True, override_reason="test: criteria overridden")
    from captain_search import handoff as HO

    ev = [e for e in HO.get(ws, dep.handoff)["links"] if e.get("kind") == "ab_readout"] if "links" in HO.get(ws, dep.handoff) else \
        [e for e in HO.listing(ws, ws.candidate)[0]["links"] if e.get("kind") == "ab_readout"]
    assert len(ev) == 1 and ev[0]["evidence"] == "REAL_TRAFFIC" and ev[0]["readout"] == ro["text"] and ev[0]["project"] == "shop"


def test_keep_the_baseline_rolls_back_and_the_old_readout_is_filed(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    _real(ws, 3)
    first = AB.start(ws, "shop", split="25%")
    _real(ws, 40, start=50)
    out = AB.decide(ws, "shop", "keep-baseline")
    ab = out["ab"]
    assert ab["status"] == "rolled_back" and ab["decision"]["label"] == "Keep the baseline"
    line = ab["rolled_back"]["text"]
    assert line == (f"Rolled back {ws.candidate} from 25% of traffic: the decision was to keep the baseline. Every shopper gets "
                    f"{ws.baseline} again. Next: fix what went wrong and start a new test, or keep the baseline.")
    assert AB.readout(ws, "shop")["rolled_back"]["text"] == line
    assert SG.state(ws, "shop")["model_version"] == ws.baseline
    # a reason given by the person is kept
    second = AB.start(ws, "shop", split=0.05)
    assert [a["id"] for a in PS.get(ws, "shop")["ab_history"]] == [first["id"]]  # the earlier test is filed
    assert AB.get(ws, "shop")["id"] == second["id"] and AB.get(ws, "shop")["rolled_back"] is None
    AB.rollback(ws, "shop", reason="checkout errors went up.")
    assert "from 5% of traffic: checkout errors went up. Every shopper" in AB.get(ws, "shop")["rolled_back"]["text"]
    cli = CliRunner().invoke(app, ["projects", "ab-readout", "shop"])
    assert cli.output.startswith("Rolled back ") and "filed with the test, not current" in cli.output


def test_shares_are_written_the_way_people_write_them():
    assert [AB.share(x) for x in (5, "5", "5%", 0.05, "0.05", "100", 1, "1%")] == [0.05, 0.05, 0.05, 0.05, 0.05, 1.0, 1.0, 0.01]
    for bad in ("0", "150", "-5", "half"):
        with pytest.raises(ValueError):
            AB.share(bad)


# ---- Try it: what moved, why it is there, and a candidate that reproduces keyword search -----------


def test_try_it_marks_what_moved_and_says_why_a_result_is_there(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    r = SG.query(ws, "shop", "jacket", k=10)
    base = [x["id"] for x in r["baseline"]["results"]]
    for i, x in enumerate(r["staged"]["results"]):
        want = "new" if x["id"] not in base else "same" if base.index(x["id"]) == i else "up" if base.index(x["id"]) > i else "down"
        assert x["move"] == want and (x["was"] is None) == (x["id"] not in base)
    assert (r["moved"] == 0) == all(x["move"] == "same" for x in r["staged"]["results"])
    why = r["staged"]["results"][0]["why"]
    assert why and why[0]["signal"] == "price_log" and why[0]["label"] == "its price" and why[0]["weight"] == 1.0  # the test model has one split
    assert isinstance(why[0]["value"], float) and r["staged"]["why_note"] is None
    assert r["baseline"]["results"][0]["why"] is None and "keyword match alone" in r["baseline"]["why_note"]
    assert TF.signal_label("qp_impressions_log") == "how often it was shown for this query before"
    assert TF.signal_label("bm25_description") == "how well its description matches the query's words"


def test_a_candidate_that_reproduces_keyword_search_is_called_that_with_the_reason(ws, monkeypatch, tmp_path):  # noqa: F811
    # this fixture's trained candidate reorders by price, so it differs from keyword search
    d = TF.compare_offline(ws, "shop", ws.exp)
    assert d["checked"] >= 5 and not d["mostly_same"] and d["queries"] and d["queries"][0]["differs"] >= d["queries"][-1]["differs"]
    assert d["text"].startswith("It ranks differently from the baseline on ")
    # a candidate whose model has no split changes nothing: same order as keyword search everywhere
    from test_starter_ranker import FAKE_TRAINER
    from captain_search import trainers as TR

    flat = FAKE_TRAINER.replace('"threshold": 4.0', '"threshold": 1e9')  # every product goes the same way
    (tmp_path / "flat.py").write_text(flat)
    monkeypatch.setattr(TR, "command", lambda name="lightgbm-ltr": f'"$CST_PYTHON" {tmp_path / "flat.py"}')
    r = ST.train(ws, "shop")
    same = r["sameness"]
    assert same["mostly_same"] and same["same"] == same["checked"] and same["same_share"] == 1.0
    assert same["text"].startswith("This candidate mostly reproduces the baseline: it learned from events that keyword search generated. ")
    assert same["none_differ"].startswith(f"None of the {same['checked']} queries checked rank differently.")
    row = next(e for e in PS.experiments(ws, "shop")["experiments"] if e["id"] == r["experiment"])
    assert row["sameness"]["mostly_same"] and row["sameness"]["text"] == same["text"]  # on the candidate's row
    # staged, the Try it list says so too
    SG.stage(ws, "shop", candidate=r["experiment"], target="local")
    live = TF.differ(ws, "shop")
    assert live["mostly_same"] and live["queries"] == [] and "None of the" in live["none_differ"]
    # before training, the starter form warns when the events were simulated from keyword search
    assert any("generated from keyword search" in w and "mostly reproduces keyword search" in w for w in ST.plan(ws, "shop")["warnings"])


# ---- will it fit on this computer ------------------------------------------------------------------


def test_the_local_choice_is_checked_against_what_this_computer_has_free(ws, monkeypatch):  # noqa: F811
    from captain_search.staging import fit as FT

    arms = {"candidate": ws.candidate, "baseline": ws.baseline}
    n = len(ops.products_of(ws, ws.registry.resolve("pipeline", ws.candidate)[0].catalog))
    assert FT._products(ws, ws.candidate) == (n, "ranker") and FT._products(ws, ws.baseline) == (n, "keyword")
    assert FT._products(ws, "https://our-own/search") == (0, "wrapped")
    # fits: the demo store on a laptop with room
    ok = FT.check(ws, arms, {"memory_mb": 16000, "disk_mb": 50000})
    assert ok["outcome"] == "fits" and ok["need"]["memory_mb"] < 250 and ok["text"].startswith("Needs about ") and "this computer has 16 GB free" in ok["text"]
    # tight: allowed, with a warning
    tight = FT.check(ws, arms, {"memory_mb": ok["need"]["memory_mb"] * 1.2, "disk_mb": 50000})
    assert tight["outcome"] == "tight" and "It will run, but other work on this computer may slow down." in tight["text"]
    # won't fit: memory, then disk
    no = FT.check(ws, arms, {"memory_mb": 64, "disk_mb": 50000})
    assert no["outcome"] == "wont-fit" and no["text"].endswith("and this computer has 64 MB free. Deploy it to a cloud.")
    assert FT.check(ws, arms, {"memory_mb": 16000, "disk_mb": 10})["text"].endswith("of disk, and this computer has 10 MB free. Deploy it to a cloud.")
    # can't read the machine: say so, never guess
    assert FT.check(ws, arms, {"memory_mb": None, "disk_mb": None})["outcome"] == "unknown"
    # the measured figures: a 200,000 product catalog with both versions needs what was measured, plus headroom
    big = FT.need([(200_000, "ranker"), (200_000, "keyword")])
    assert 3300 < big["memory_mb"] < 3600 and 6 < big["load_s"] < 9 and 100 < big["query_ms"] < 130
    assert FT.need([(900_000, "ranker"), (900_000, "keyword")])["memory_mb"] > 14_000
    real = FT.free()
    assert real["disk_mb"] > 0 and (real["memory_mb"] is None or real["memory_mb"] > 0)

    # the plan carries it, and staging here is refused cleanly when it won't fit, before anything starts
    monkeypatch.setattr(FT, "free", lambda: {"memory_mb": 64, "disk_mb": 50000})
    p = SG.plan(ws, "shop", target="local")
    assert p["local_fit"]["outcome"] == "wont-fit"
    with pytest.raises(ValueError, match="this computer has 64 MB free. Deploy it to a cloud."):
        SG.stage(ws, "shop", target="local")
    assert SG.state(ws, "shop") is None
    monkeypatch.setattr(FT, "free", lambda: {"memory_mb": 16000, "disk_mb": 50000})
    assert SG.stage(ws, "shop", target="local")["status"] == "running"


def test_an_endpoint_that_dies_while_loading_says_it_ran_out_of_memory(ws, monkeypatch):  # noqa: F811
    import subprocess as sp
    import sys

    real = sp.Popen
    monkeypatch.setattr(SG.subprocess, "Popen", lambda argv, **kw: real([sys.executable, "-c", "import sys; sys.exit(137)"], **{k: v for k, v in kw.items() if k != "env"}))
    with pytest.raises(ValueError, match="most likely because this computer ran out of memory"):
        SG.stage(ws, "shop", target="local")
    assert SG.state(ws, "shop")["status"] == "failed"


# ---- the deployment page's Requests and Versions --------------------------------------------------


def test_requests_newest_first_and_one_request_in_full(ws):  # noqa: F811
    assert TF.requests(ws, "shop")["rows"] == []
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    assert TF.requests(ws, "shop")["text"] == "No requests yet. Try a query, or send a test request."
    a = SG.call(ws, rec, "rank", {"traffic": "real", "query": "jacket", "k": 5, "session_id": "s1", "request_id": "first"})
    SG.call(ws, rec, "rank", {"query": "boots", "k": 3, "traffic": "test", "request_id": "second"})
    SG.call(ws, rec, "outcome", {"request_id": "first", "event": "click", "product_id": a["ids"][1]})
    r = TF.requests(ws, "shop")
    assert [x["request_id"] for x in r["rows"]] == ["second", "first"] and r["total"] == 2 and r["text"] == "2 requests since this version was staged."
    top = r["rows"][1]
    assert top["query"] == "jacket" and top["arm"] == "candidate" and top["model_version"] == ws.candidate and top["results"] == 5 and top["status"] == "answered"
    assert r["rows"][0]["traffic"] == "test" and top["traffic"] == "real" and top["ms"] >= 0
    one = TF.request(ws, "shop", "first")
    assert [x["id"] for x in one["results"]] == a["ids"] and one["results"][0]["title"] and one["session_id"] == "s1"
    assert one["results"][1]["events"] == ["click"] and one["results"][0]["events"] == []
    sig = one["results"][0]["signals"]
    assert sig and sig[0]["label"] and isinstance(sig[0]["value"], float) and any(x["signal"] == "price_log" for x in sig)
    assert one["shadow"] is None
    with pytest.raises(ValueError, match="No request nope"):
        TF.request(ws, "shop", "nope")


def test_versions_list_and_roll_back_to_one(ws):  # noqa: F811
    assert TF.versions(ws, "shop")["rows"] == []
    SG.stage(ws, "shop", baseline=True, target="local")
    SG.stage(ws, "shop", candidate=ws.exp, target="local")
    v = TF.versions(ws, "shop")
    assert [(r["n"], r["model_version"], r["live"]) for r in v["rows"]] == [(2, ws.candidate, True), (1, ws.baseline, False)]
    assert v["rows"][1]["can_roll_back"] and not v["rows"][0]["can_roll_back"] and v["rows"][0]["by"] and v["rows"][0]["at"]
    out = TF.rollback_to(ws, "shop", 1)
    assert out["rolled_back_to"] == ws.baseline and SG.state(ws, "shop")["model_version"] == ws.baseline
    v = TF.versions(ws, "shop")
    assert [(r["n"], r["live"]) for r in v["rows"]] == [(3, True), (2, False), (1, False)] and v["rows"][0]["model_version"] == ws.baseline
    assert "rolled staging back to version 1" in " | ".join(h["what"] for h in PS.get(ws, "shop")["history"])
    with pytest.raises(ValueError, match="There is no version 9"):
        TF.rollback_to(ws, "shop", 9)


def test_a_model_over_the_size_limit_is_not_staged_on_this_computer_even_with_memory_free(ws):  # noqa: F811
    from captain_search import settings as ST
    from captain_search.staging import fit as FT

    arms = {"candidate": ws.candidate, "baseline": ws.baseline}
    roomy = {"memory_mb": 64_000, "disk_mb": 500_000}
    assert FT.limit_mb(ws) == 1024  # the default: 1 GB
    need = FT.check(ws, arms, roomy)["need"]["memory_mb"]
    assert FT.check(ws, arms, roomy)["outcome"] == "fits" and need < 1024  # the demo store is under it
    # over: the smallest limit is below what even the demo store needs with both versions and headroom
    ST.set_local_limit(ws, 250) if need > 250 else None
    big = FT.need([(200_000, "ranker"), (200_000, "keyword")])
    assert big["memory_mb"] > 1024  # what a 200,000 product catalog needs: over the default
    from unittest import mock

    with mock.patch.object(FT, "need", return_value={**big, "products": 200_000}):
        ST.set_local_limit(ws, 1024)
        r = FT.check(ws, arms, roomy)
        assert r["outcome"] == "over-limit" and r["limit_mb"] == 1024
        assert r["text"] == (f"This model needs about {big['memory_mb'] / 1024:.1f} GB to serve, which is more than the 1 GB limit for staging on "
                             "this computer. Deploy it to a cloud.")
        with pytest.raises(ValueError, match="more than the 1 GB limit for staging on this computer.*raise the limit in Settings, or pass --allow-large"):
            SG.stage(ws, "shop", target="local")
        assert SG.state(ws, "shop") is None
        out = CliRunner().invoke(app, ["projects", "stage", "shop", "--target", "local"])
        assert out.exit_code == 1 and "1 GB limit for staging on this computer" in out.output
        # the limit is a setting: raise it and the same model is offered, with the free-memory test still applied
        ST.set_local_limit(ws, 4096)
        assert FT.check(ws, arms, roomy)["outcome"] == "fits" and FT.check(ws, arms, {"memory_mb": 2000, "disk_mb": 500_000})["outcome"] == "wont-fit"
        ST.set_local_limit(ws, 0)  # no limit
        assert FT.check(ws, arms, roomy)["outcome"] == "fits"
        ST.set_local_limit(ws, 1024)
        # --allow-large skips the limit, never the free-memory test
        assert FT.check(ws, arms, roomy, allow_large=True)["outcome"] == "fits"
        assert FT.check(ws, arms, {"memory_mb": 2000, "disk_mb": 500_000}, allow_large=True)["outcome"] == "wont-fit"
    with pytest.raises(ValueError, match="250 MB, 500 MB, 1 GB, 2 GB, 4 GB or no limit"):
        ST.set_local_limit(ws, 300)
    assert SG.stage(ws, "shop", target="local", allow_large=True)["status"] == "running"


def test_a_request_with_no_label_is_never_called_real(ws):  # noqa: F811
    """A script or this computer can send requests too. Only a site that says "traffic": "real" counts as shoppers."""
    SG.stage(ws, "shop", target="local", allow_large=True)
    rec = SG.state(ws, "shop")
    for i in range(6):
        SG.call(ws, rec, "rank", {"query": "jacket", "k": 5, "session_id": f"script-{i}"})
    k = TF.connect(ws, "shop")
    assert k["real"] == 0 and k["unlabelled"] == 6 and k["evidence_label"] == "Unlabelled" and not k["connected"]
    assert k["live"].startswith("Received 6 unlabelled requests.") and 'send "traffic": "real" with each request' in k["live"]
    assert len(k["snippets"]) == 3 and all('"traffic": "real"' in v or 'traffic: "real"' in v for v in k["snippets"].values())  # the site code labels shoppers
    opts = AB.options(ws, "shop")
    assert not opts["ready"] and "with no label" in opts["text"]
    with pytest.raises(ValueError, match="No real request has reached this endpoint yet"):
        AB.start(ws, "shop")
    assert TF.requests(ws, "shop")["rows"][0]["traffic"] == "unlabelled"
    # a practice test over the same unlabelled requests can never be real evidence
    AB.start(ws, "shop", split=0.5, practice=True)
    for i in range(20):
        SG.call(ws, SG.state(ws, "shop"), "rank", {"query": "jacket", "k": 5, "session_id": f"script-b{i}"})
    r = AB.readout(ws, "shop", traffic="unlabelled")
    assert r["evidence"] == "UNLABELLED" and r["label"] == "Unlabelled" and not r["can_ship"]
    assert "can't go to launch review" in r["cannot_ship"]
    # the moment the site labels them, they count
    SG.call(ws, SG.state(ws, "shop"), "rank", {"query": "jacket", "k": 5, "session_id": "shopper-1", "traffic": "real"})
    assert TF.connect(ws, "shop")["real"] == 1 and TF.connect(ws, "shop")["evidence_label"] == "Real traffic"
