"""Traffic and numbers for a deployment. On AWS: requests, errors and response time from CloudWatch, processor and
memory when AWS has published them, and hours and cost from Captain's own record of when the model was up. Against
the stand-ins. Nothing here calls a real cloud."""
import datetime as dt

import pytest

from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import numbers as NB
from captain_search.staging import sizing as SZ

from test_deploy_one_call import LOGINS, logins  # noqa: F401  (fixtures)
from test_hosting_modal import modal  # noqa: F401  (fixtures)
from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)

REQUESTS = {("Invocations", "Sum"), ("Invocation4XXErrors", "Sum"), ("Invocation5XXErrors", "Sum"), ("ModelLatency", "p50"), ("ModelLatency", "p95"),
            ("OverheadLatency", "p50")}


@pytest.fixture(autouse=True)
def nothing_kept():
    NB.forget()
    yield
    NB.forget()


def _reads(aws):  # noqa: F811
    return [c for c, _ in aws.calls].count("get_metric_data")


def _as_the_real_account(aws):  # noqa: F811
    """What AWS held on 2026-11-12 for a real endpoint that ran for an hour on ml.t2.medium: every request number,
    by EndpointName and VariantName, and nothing at all under /aws/sagemaker/Endpoints. Processor and memory were
    never published for it."""
    full = aws.metric
    aws.metric = lambda name, stat, t, period: full(name, stat, t, period) if (name, stat) in REQUESTS else None


def test_requests_errors_and_response_time_come_from_cloudwatch_and_never_from_the_model(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    asked = [c for c, _ in aws.calls].count("invoke_endpoint")
    n = NB.numbers(ws, "shop", span="1h")
    assert n["source"] == "cloud" and "AWS's own metrics" in n["source_text"] and n["not_read"] == []
    assert len(n["buckets"]) == 60 and n["bucket_seconds"] == 60 and n["ranges"] == ["15m", "1h", "24h", "7d"]
    b = n["buckets"][0]
    # ModelLatency arrives in microseconds: 3000 and 9000 are 3 ms and 9 ms.
    assert (b["requests"], b["errors"], b["p50_ms"], b["p95_ms"], b["cpu"], b["memory"]) == (120, 0, 3.0, 9.0, 20.0, 5.0)
    s = n["summary"]
    assert (s["requests"], s["errors"], s["per_minute"], s["per_minute_peak"], s["p50_ms"], s["p95_ms"], s["overhead_ms"]) == (7200, 0, 120.0, 120.0, 3.0, 9.0, 0.8)
    assert (s["cpu"], s["cpu_peak"], s["memory"], s["memory_peak"]) == (20.0, 35.0, 5.0, 6.0)
    assert n["text"] == "7,200 requests in the last hour, no errors. Median 3.0 ms, 95th percentile 9.0 ms at the endpoint."
    assert [c for c, _ in aws.calls].count("invoke_endpoint") == asked  # reading the numbers sent the model nothing
    day = NB.numbers(ws, "shop", span="24h")
    assert len(day["buckets"]) == 288 and day["bucket_seconds"] == 300 and day["summary"]["per_minute"] == 120.0 and day["buckets"][0]["requests"] == 600
    with pytest.raises(ValueError, match="15m, 1h, 24h or 7d"):
        NB.numbers(ws, "shop", span="2h")


def test_an_endpoint_aws_published_no_processor_or_memory_numbers_for_says_so(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    _as_the_real_account(aws)
    n = NB.numbers(ws, "shop", span="1h")
    s = n["summary"]
    # The request numbers are all there and are used.
    assert n["source"] == "cloud" and (s["requests"], s["errors"], s["p50_ms"], s["p95_ms"]) == (7200, 0, 3.0, 9.0)
    # Processor and memory are not, and that is said in a sentence, never drawn as an empty chart or a zero.
    assert s["cpu"] is None and s["memory"] is None and all(b["cpu"] is None and b["memory"] is None for b in n["buckets"])
    assert n["not_read"] == ["AWS has not published processor and memory numbers for this endpoint."]
    # And nothing is counted from them: the cost is there all the same.
    assert n["cost"]["source"] in ("record", "times") and n["cost"]["month_usd"] is not None


def test_copies_are_the_clouds_count_now_and_its_scaling_settings(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    _as_the_real_account(aws)
    s = NB.numbers(ws, "shop")["summary"]
    assert (s["copies_now"], s["copies_min"], s["copies_max"]) == (1, 1, 1) and s["copies_text"] == "1 copy up now. It always runs 1."
    assert all(b["copies"] is None for b in NB.numbers(ws, "shop")["buckets"])  # no history of copies is made up
    SG.scale(ws, "shop", min_copies=1, max_copies=3)
    NB.forget()
    assert NB.numbers(ws, "shop")["summary"]["copies_text"] == "1 copy up now. It may run 1 to 3."


def test_errors_and_a_slow_minute_show(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    quiet = aws.metric

    def busy(name, stat, t, period):
        late = t.minute % 10 == 0  # one minute in ten: errors and a slow tail
        if (name, stat) == ("Invocation5XXErrors", "Sum"):
            return 6.0 if late else 0.0
        if (name, stat) == ("ModelLatency", "p95"):
            return 250000.0 if late else 9000.0
        return quiet(name, stat, t, period)

    aws.metric = busy
    s = NB.numbers(ws, "shop", span="1h")["summary"]
    # The failures are counted. The 95th percentile is the range's own, asked of AWS as one: not the slowest minute's 250 ms.
    assert s["errors"] == 36 and round(s["error_rate"], 4) == 0.005 and s["p50_ms"] == 3.0 and s["p95_ms"] != 250.0


def test_hours_and_cost_come_from_captains_own_record_at_the_list_price(ws, aws):  # noqa: F811
    """A real endpoint that ran for an hour sent AWS no processor reports, and the hours were counted from those: an
    hour of running came out as nothing. Captain knows when it started and stopped a model. That is the count."""
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    _as_the_real_account(aws)
    hourly = next(s for s in SZ.sizes("aws", "us-west-1") if s["name"] == "ml.t2.medium")["hourly_usd"]
    now = dt.datetime.now(dt.timezone.utc)
    start = max(now - dt.timedelta(hours=1, minutes=20), now.replace(hour=0, minute=0, second=0, microsecond=0))
    ran = (min(now - dt.timedelta(minutes=20), now) - start).total_seconds() / 3600 if now - start > dt.timedelta(minutes=20) else 0
    SG._save(ws, "shop", "staging", status="stopped", started_at=(now - dt.timedelta(hours=1, minutes=20)).isoformat(timespec="seconds"),
             stopped_at=(now - dt.timedelta(minutes=20)).isoformat(timespec="seconds"), stop_reason="stopped by dana@example.com")
    NB.forget()
    before = _reads(aws)
    c = NB.cost(ws, "shop")
    assert _reads(aws) == before  # the cost asks AWS nothing
    assert c["source"] == "times" and c["hourly_usd"] == 0.0 and c["rate_usd"] == pytest.approx(hourly, abs=1e-4) and c["machine"] == "ml.t2.medium"
    assert c["month_usd"] == pytest.approx(hourly, abs=2e-3) and c["month_usd"] > 0  # an hour at the list price, not nothing
    assert c["today_usd"] == pytest.approx(ran * hourly, abs=2e-3) or c["today_usd"] <= c["month_usd"]
    assert c["text"].startswith("Stopped, so it costs nothing now.") and "This month: about $0.07." in c["text"]
    assert "from when Captain started and stopped it, at the list price for ml.t2.medium" in c["text"] and c["text"].endswith("Your AWS bill is the final word.")
    # While it runs: the rate, and the hours so far.
    SG._save(ws, "shop", "staging", status="running", started_at=(now - dt.timedelta(minutes=30)).isoformat(timespec="seconds"), stopped_at=None)
    NB.forget()
    c = NB.cost(ws, "shop")
    assert c["hourly_usd"] == pytest.approx(hourly, abs=1e-3) and c["text"].startswith("About $0.07 an hour for each copy while it is up.")
    assert 0 < c["month_usd"] <= hourly


def test_the_numbers_are_kept_for_a_minute_because_each_read_costs(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    NB.numbers(ws, "shop")
    first = _reads(aws)
    NB.numbers(ws, "shop")
    NB.numbers(ws, "shop")
    assert _reads(aws) == first == 2  # the range by the minute and the range as a whole, and none for the cost
    NB.numbers(ws, "shop", span="15m")
    assert _reads(aws) == 4 and 30 <= NB.KEEP_S <= 120  # another range is another read


def test_when_cloudwatch_cannot_be_read_the_request_log_answers_and_says_so(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    for q in ("jacket", "tent", "boots"):
        SG.query(ws, "shop", q)
    SG.call(ws, SG.state(ws, "shop"), "flush", {})
    aws.fail_on = "get_metric_data"
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] == 3 and n["summary"]["cpu"] is None
    assert "Captain couldn't read AWS's own metrics just now" in n["not_read"][0] and "from the request log" in n["not_read"][0]
    assert n["cost"]["source"] in ("record", "times")  # the cost never needed AWS
    aws.fail_on = None


def test_modal_says_what_it_does_not_read_and_uses_the_request_log(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] >= 5  # the deploy's own test requests
    assert n["not_read"] == [NB.NOT_READ["modal"]] and n["summary"]["cpu"] is None and all(b["cpu"] is None for b in n["buckets"])
    assert n["cost"]["source"] in ("record", "times") and "Not from the cloud's own numbers." in n["cost"]["text"] and "the most it can have cost" in n["cost"]["text"]


def test_nothing_deployed_and_this_computer(ws):  # noqa: F811
    n = NB.numbers(ws, "shop")
    assert n["source"] == "none" and n["text"] == "Nothing is deployed, so there is nothing to count." and n["buckets"] == []
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    SG.query(ws, "shop", "jacket")
    SG.call(ws, SG.state(ws, "shop"), "flush", {})
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] == 1 and n["not_read"] == []
    SG.unstage(ws, "shop")


def test_requests_are_counted_from_the_request_log_not_from_every_call_aws_sees(ws, aws):  # noqa: F811
    """On a real endpoint, 24 test searches read as "160 requests in the last hour". AWS counts every call, and
    Captain calls the endpoint itself: to ask whether it is well, and to write its log. Those were most of the 160."""
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    for q in ("jacket", "tent", "boots"):
        SG.query(ws, "shop", q)
    SG.call(ws, SG.state(ws, "shop"), "flush", {})
    n = NB.numbers(ws, "shop", span="1h")
    s = n["summary"]
    assert n["source"] == "cloud" and s["counted_from"] == "log"
    assert s["requests"] == 3 and s["errors"] == 0 and s["calls"] == 7200  # three requests for results, of 7,200 calls AWS counted
    assert s["calls_text"] == ("7,200 calls reached the endpoint in the last hour. That counts Captain's own checks (asking the model whether it is "
                               "well, and to write its log) as well as requests for results.")
    assert n["text"].startswith("3 requests in the last hour, no errors.") and "request log, which holds only requests for results" in n["source_text"]
    assert s["p50_ms"] is not None and s["p50_ms"] != 3.0  # the time of those three requests, not of every call


def test_with_no_request_log_aws_gives_the_ranges_own_percentiles(ws, aws):  # noqa: F811
    """The worst minute's 95th percentile is not the range's. AWS is asked for the percentile over the whole range."""
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    quiet = aws.metric

    def spiky(name, stat, t, period):
        if name == "ModelLatency" and period > 60:   # asked over the whole range
            return {"p50": 3100.0, "p95": 12000.0}[stat]
        if (name, stat) == ("ModelLatency", "p95"):  # by the minute: one slow minute in ten
            return 250000.0 if t.minute % 10 == 0 else 9000.0
        return quiet(name, stat, t, period)

    aws.metric = spiky
    s = NB.numbers(ws, "shop", span="1h")["summary"]
    assert s["counted_from"] == "cloud" and (s["p50_ms"], s["p95_ms"]) == (3.1, 12.0)  # not 250 ms, the slowest minute's
    assert s["requests"] == s["calls"] == 7200 and "Captain's own checks" in s["calls_text"]
