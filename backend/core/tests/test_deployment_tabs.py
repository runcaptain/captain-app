"""The deployment page's Copies, Logs, Settings, Cost and Metrics tabs, and the Deployments list."""
from __future__ import annotations

import pytest

from captain_search import staging as SG
from captain_search.staging import observe as OB
from captain_search.staging import traffic as TF

from test_staging import keychain, ws  # noqa: F401  (fixtures)


def _site(ws, n, start=0, **extra):  # noqa: F811
    rec = SG.state(ws, "shop")
    qs = ["jacket", "tent", "boots"]
    return [SG.call(ws, rec, "rank", {"query": qs[i % 3], "k": 5, "session_id": f"s{start + i}", "traffic": "real", **extra}) for i in range(n)]


def test_three_states_are_said_apart_stopped_idle_and_handling(ws):  # noqa: F811
    assert OB.live(ws, "shop")["state"] == "stopped" and OB.copies(ws, "shop")["rows"] == []
    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 3)
    lv = OB.live(ws, "shop")
    assert lv["state"] == "handling" and lv["label"] == "Handling requests" and lv["line"] == "Copies: 1 live. Requests: 0 running."
    # quiet for a while: up and idle, which is said as fine, never as a fault
    idle = OB.activity(SG.state(ws, "shop"), {"running": 0, "idle_s": 600, "stops_in_s": 3000})
    assert idle["state"] == "idle" and idle["text"].startswith("Idle and ready: nothing has called it for 10 min.") and "stops by itself in 50 min" in idle["text"]
    SG.unstage(ws, "shop")
    assert OB.live(ws, "shop")["state"] == "stopped" and OB.live(ws, "shop")["copies"] == 0


def test_copies_lists_the_one_copy_with_its_startup_time_and_what_it_handled(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 4)
    c = OB.copies(ws, "shop")
    row = c["rows"][0]
    assert len(c["rows"]) == 1 and row["requests"] == 4 and row["model_version"] == SG.state(ws, "shop")["model_version"]
    assert row["where"] == "Running on this computer." and row["ready_s"] is not None and row["ready_text"].startswith("Ready to answer ")
    assert row["status"] == "Handling requests" and row["memory_mb"] > 0 and len(row["copy"]) == 8
    assert c["minimum"] == 0 and c["maximum"] == 1 and "Stopping the copy stops the endpoint." in c["text"]


def test_the_log_has_a_line_per_request_to_the_millisecond_and_can_be_filtered_and_followed(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    got = _site(ws, 3)
    rec = SG.state(ws, "shop")
    SG.call(ws, rec, "outcome", {"request_id": got[0]["request_id"], "event": "click", "product_id": got[0]["ids"][0]})
    lg = OB.logs(ws, "shop")
    texts = [x["text"] for x in lg["rows"]]
    assert texts[0].startswith("Ready in ") and lg["streaming"] and "interleave" in lg["note"]
    assert sum(t.startswith("Ranked 'jacket' with the candidate in ") for t in texts) == 1 and any(t.startswith("Outcome: click of ") for t in texts)
    assert all(len(x["ts"]) == 24 and x["ts"].endswith("Z") for x in lg["rows"])  # 2026-11-11T16:05:49.231Z: milliseconds
    assert [x["ts"] for x in lg["rows"]] == sorted(x["ts"] for x in lg["rows"])
    only = OB.logs(ws, "shop", q="TENT")
    assert only["matching"] == 1 and only["total"] == lg["total"] and "tent" in only["rows"][0]["text"] and only["text"] == f"1 of {lg['total']} lines match."
    # the lines of one request, which is what the Requests panel shows
    mine = OB.logs(ws, "shop", request_id=got[0]["request_id"])
    assert [x["text"].split(" ")[0] for x in mine["rows"]] == ["Ranked", "Outcome:"]
    one = TF.request(ws, "shop", got[0]["request_id"])
    assert [x["text"].split(" ")[0] for x in one["lines"]] == ["Ranked", "Outcome:"] and one["site_ms"] is None
    assert one["time_text"].endswith("Your site didn't report how long it took.")
    # following: only what came after the last line seen
    _site(ws, 1, start=10)
    more = OB.logs(ws, "shop", since=lg["last"])
    assert len(more["rows"]) == 1 and more["rows"][0]["text"].startswith("Ranked ")
    # a refused request and a stop are in the log too
    with pytest.raises(Exception):
        SG.call(ws, rec, "rank", {"query": ""})
    SG.unstage(ws, "shop")
    done = OB.logs(ws, "shop")
    assert any(x["level"] == "warn" and x["text"].startswith("Refused a request:") for x in done["rows"])
    assert not done["streaming"] and done["text"].endswith("The endpoint is stopped, so the log is complete.")


def test_settings_are_grouped_and_the_idle_time_can_be_changed_while_it_runs(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    st = OB.settings(ws, "shop")
    assert [g["title"] for g in st["groups"]] == ["Where it runs", "Copies", "Stopping when idle", "Time limits", "Served-features check"]
    assert st["can_edit"] and st["idle_minutes"] == 240 and st["idle_choices"] == [15, 60, 240, 720, 1440]
    assert ["Stops after", "4 h with no requests"] in st["groups"][2]["rows"] and st["groups"][2]["edit"] == "idle"
    out = OB.set_idle(ws, "shop", 60)
    assert out["saved"] == "It now stops after 1 h with no requests." and out["idle_minutes"] == 60
    assert SG.state(ws, "shop")["idle_minutes"] == 60 and SG.control(ws, "shop")["idle_minutes"] == 60
    assert SG.call(ws, SG.state(ws, "shop"), "status", {})["idle_minutes"] == 60  # the endpoint took it, with no restart
    assert any(x["text"].startswith("Traffic changed:") and "Stops after 60 idle minutes." in x["text"] for x in OB.logs(ws, "shop")["rows"])
    with pytest.raises(ValueError, match="between 5 minutes and 7 days"):
        OB.set_idle(ws, "shop", 1)
    SG.unstage(ws, "shop")
    with pytest.raises(ValueError, match="Nothing is staged"):
        OB.set_idle(ws, "shop", 60)


def test_cost_of_a_local_endpoint_is_none_and_says_so(ws):  # noqa: F811
    assert OB.cost(ws, "shop")["text"] == "Nothing is staged, so nothing is costing anything."
    SG.stage(ws, "shop", target="local", allow_large=True)
    c = OB.cost(ws, "shop")
    assert c["free"] and c["text"] == "Runs on this computer. No cloud cost." and c["month_usd"] == 0 and c["hourly_usd"] is None


def test_cost_of_a_cloud_endpoint_is_this_month_the_last_day_and_the_rate_by_machine(ws, monkeypatch):  # noqa: F811
    import datetime as dt

    from captain_search import cost_estimates as CE

    SG.stage(ws, "shop", target="local", allow_large=True)
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda t: t.isoformat(timespec="seconds")  # noqa: E731
    # two earlier cloud stages this month, one of them inside the last 24 hours, and one last month
    led = [{"kind": "staging", "project": "shop", "where": "aws-serving", "actual": {"usd": 0.66, "seconds": 36000, "at": iso(now - dt.timedelta(hours=2)), "machine": "ml.t2.medium"}},
           {"kind": "staging", "project": "shop", "where": "aws-serving", "actual": {"usd": 0.132, "seconds": 7200, "at": iso(now - dt.timedelta(hours=30)), "machine": "ml.t2.medium"}},
           {"kind": "training", "project": "shop", "where": "aws", "actual": {"usd": 9.0, "seconds": 60, "at": iso(now)}}]
    monkeypatch.setattr(CE, "ledger", lambda ctx, project=None, limit=400: led)
    c = OB.cost(ws, "shop")
    if now.day > 2:  # both stages fall in this month
        assert c["month_usd"] == pytest.approx(0.792, abs=1e-3) and c["rows"][0]["machine"] == "ml.t2.medium" and c["rows"][0]["hours"] == pytest.approx(12, abs=0.01)
    assert c["day_usd"] == pytest.approx(0.66, abs=1e-3) and c["day_text"] == "$0.66" and not c["free"]
    assert "in the last 24 hours" in c["text"] and c["note"].endswith("Your cloud's bill is the final word.")


def test_metrics_give_median_and_95th_percentile_at_the_endpoint_and_as_the_site_sees_it(ws):  # noqa: F811
    assert OB._pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5 and OB._pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert OB._pct(list(range(1, 101)), 95) == 95 and OB._pct([7], 95) == 7 and OB._pct([], 50) is None
    SG.stage(ws, "shop", target="local", allow_large=True)
    got = _site(ws, 20)
    rec = SG.state(ws, "shop")
    m = OB.metrics(ws, "shop", span="15m")
    assert len(m["buckets"]) == 30 and sum(b["requests"] for b in m["buckets"]) == 20 and m["summary"]["requests"] == 20
    assert m["buckets"][-1]["requests"] == 20 and m["buckets"][-1]["p50"] <= m["buckets"][-1]["p95"]
    assert m["summary"]["p50"] is not None and m["summary"]["errors"] == 0 and m["summary"]["copies"] == 1 and m["summary"]["copies_max"] == 1
    assert m["summary"]["site_p50"] is None and m["summary"]["site_text"].startswith("As your site sees it: not reported yet.")
    assert m["markers"] and m["markers"][0]["text"].startswith("Version 1 staged: ") and m["markers"][0]["slot"] is not None
    assert m["text"].startswith("20 requests in the last 15 minutes, no errors. At the endpoint: median ")
    # the site reports what it saw: with an outcome, or by itself
    for i, r in enumerate(got[:10]):
        SG.call(ws, rec, "outcome", {"request_id": r["request_id"], "event": "click", "product_id": r["ids"][0], "client_ms": 100 + i})
    SG.call(ws, rec, "outcome", {"request_id": got[10]["request_id"], "event": "timing", "ms": 300})
    m = OB.metrics(ws, "shop", span="1h")
    assert m["summary"]["site_reported"] == 11 and m["summary"]["site_p50"] == 105 and m["summary"]["site_p95"] == 300
    assert m["summary"]["site_text"] == "As your site sees it: median 105 ms, 95th percentile 300 ms, from 11 requests your site timed."
    assert TF.request(ws, "shop", got[0]["request_id"])["site_ms"] == 100
    assert TF.request(ws, "shop", got[0]["request_id"])["time_text"].endswith("100 ms as your site saw it.")
    from captain_search.staging import ab as AB  # a timing is not an outcome: nothing counts it as a shopper's action

    from captain_search.staging import logs as L

    assert len(L.read(ws, "shop", "staging", rec["id"])["outcomes"]) == 10
    assert AB  # imported to show the readout still loads beside timings
    with pytest.raises(ValueError, match="15m, 1h, 24h or 7d"):
        OB.metrics(ws, "shop", span="2h")
    with pytest.raises(Exception, match="number of milliseconds"):
        SG.call(ws, rec, "outcome", {"request_id": got[0]["request_id"], "event": "timing", "ms": "soon"})


def test_the_deployments_list_shows_live_and_stopped_across_projects(ws):  # noqa: F811
    assert OB.deployments(ws)["rows"] == [] and OB.deployments(ws)["text"].startswith("Nothing is staged in any project.")
    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 5)
    d = OB.deployments(ws, "live")
    row = d["rows"][0]
    assert len(d["rows"]) == 1 and row["project"] == "shop" and row["status"] == "Handling requests" and row["copies"] == 1
    assert row["requests_24h"] == 5 and sum(row["series"]) == 5 and len(row["series"]) == 30 and row["since_text"].startswith("Staged by ")
    assert row["where"] == "Running on this computer." and row["line"] == "Copies: 1 live. Requests: 0 running."
    assert OB.deployments(ws, "stopped")["rows"] == [] and OB.deployments(ws, "stopped")["text"] == "Nothing has been stopped yet."
    SG.unstage(ws, "shop")
    assert OB.deployments(ws, "live")["rows"] == []
    s = OB.deployments(ws, "stopped")["rows"][0]
    assert s["state"] == "stopped" and s["since_text"].startswith("Stopped ") and s["requests_24h"] == 5  # stopped ones stay findable
    with pytest.raises(ValueError, match="live, stopped or all"):
        OB.deployments(ws, "old")


def test_the_commands_are_named_like_the_pages(ws):  # noqa: F811
    from typer.testing import CliRunner

    from captain_search.cli.main import app

    run = lambda *a: CliRunner().invoke(app, list(a))  # noqa: E731
    assert "Nothing is staged in any project." in run("deployments", "list").output
    out = run("deploy", "shop", "--target", "local", "--allow-large")
    assert out.exit_code == 0, out.output
    url = SG.state(ws, "shop")["endpoint"]["url"]
    assert out.output.strip().splitlines()[-1] == url  # the address is the last line, so a script can take it
    _site(ws, 2)
    ls = run("deployments", "list").output
    assert "Handling requests" in ls and "copies 1" in ls and "Staged by" in ls and "(2 requests in 24 h)" in ls
    hist = run("deployments", "history", "shop").output
    assert hist.startswith("v1 ") and hist.strip().endswith("Live")
    lg = run("deployments", "logs", "shop", "--filter", "jacket").output
    assert "Ranked 'jacket' with the candidate" in lg and "tent" not in lg
    assert "no earlier version" in run("deployments", "rollback", "shop").output
    # the old names still work, and the gated deploy commands are still there
    assert run("projects", "staging", "shop").exit_code == 0 and "promote" in run("deploy", "--help").output
    assert run("deploy", "history", "--env", "shop-staging").exit_code == 0
    assert "Staging stopped." in run("deployments", "stop", "shop").output
    assert "Stopped" in run("deployments", "list", "--stopped").output and run("deployments", "list").output.strip().startswith("Nothing is staged")


def test_reading_a_page_never_wakes_a_host_that_sleeps(ws, monkeypatch):  # noqa: F811
    """The request list, the readout, shadow mode and the traffic count are read again and again by the page. On a
    host that sleeps when idle they must not send anything to an endpoint with no copy up."""
    from captain_search.staging import ab as AB

    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 3)
    sent = []
    real_call = SG.call
    for mod in (SG, TF, AB, OB):
        monkeypatch.setattr(mod, "call", lambda ctx, rec, op, body, *a, **k: (sent.append(op), real_call(ctx, rec, op, body, *a, **k))[1])
    TF.requests(ws, "shop")
    assert sent == ["flush"]  # an ordinary host is asked to write its log first
    # the same endpoint, on a host that sleeps, with no copy up
    SG._save(ws, "shop", "staging", scale_to_zero=True)
    monkeypatch.setattr(SG, "_awake", lambda ctx, rec: False, raising=False)
    sent.clear()
    assert TF.requests(ws, "shop")["total"] == 3  # what was already written is still read
    AB.traffic_seen(ws, "shop"); AB.readout(ws, "shop"); TF.shadow(ws, "shop")
    assert "flush" not in sent, sent
    monkeypatch.setattr(SG, "_awake", lambda ctx, rec: True, raising=False)  # a copy is up: it is asked again
    TF.requests(ws, "shop")
    assert sent[-1] == "flush"
