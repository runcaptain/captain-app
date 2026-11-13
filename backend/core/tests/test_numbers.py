"""Traffic and numbers for a deployment, from the cloud's own metrics, with the request log as the second source.
Against the stand-ins. Nothing here calls a real cloud."""
import pytest

from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import numbers as NB
from captain_search.staging import sizing as SZ

from test_deploy_one_call import LOGINS, logins  # noqa: F401  (fixtures)
from test_hosting_modal import modal  # noqa: F401  (fixtures)
from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def nothing_kept():
    NB.forget()
    yield
    NB.forget()


def _reads(aws):  # noqa: F811
    return [c for c, _ in aws.calls].count("get_metric_data")


def test_aws_numbers_come_from_cloudwatch_and_never_from_the_model(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    asked = [c for c, _ in aws.calls].count("invoke_endpoint")
    n = NB.numbers(ws, "shop", span="1h")
    assert n["source"] == "cloud" and "AWS's own metrics" in n["source_text"] and n["not_read"] == []
    assert len(n["buckets"]) == 60 and n["bucket_seconds"] == 60 and n["ranges"] == ["15m", "1h", "24h", "7d"]
    b = n["buckets"][0]
    assert (b["requests"], b["errors"], b["p50_ms"], b["p95_ms"], b["cpu"], b["memory"], b["copies"]) == (120, 0, 3.0, 9.0, 20.0, 5.0, 1)
    s = n["summary"]
    assert (s["requests"], s["errors"], s["per_minute"], s["per_minute_peak"], s["p50_ms"], s["p95_ms"]) == (7200, 0, 120.0, 120.0, 3.0, 9.0)
    assert (s["cpu"], s["cpu_peak"], s["memory"], s["memory_peak"], s["copies_now"], s["copies_peak"]) == (20.0, 35.0, 5.0, 6.0, 1, 1)
    assert n["text"] == "7,200 requests in the last hour, no errors. Median 3.0 ms, 95th percentile 9.0 ms at the endpoint."
    assert [c for c, _ in aws.calls].count("invoke_endpoint") == asked  # reading the numbers sent the model nothing
    day = NB.numbers(ws, "shop", span="24h")
    assert len(day["buckets"]) == 288 and day["bucket_seconds"] == 300 and day["summary"]["per_minute"] == 120.0 and day["buckets"][0]["requests"] == 600
    with pytest.raises(ValueError, match="15m, 1h, 24h or 7d"):
        NB.numbers(ws, "shop", span="2h")


def test_errors_slow_minutes_and_more_copies_show(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    quiet = aws.metric

    def busy(name, stat, t, period):
        late = t.minute % 10 == 0  # one minute in ten: errors, a slow tail, and a second copy
        if (name, stat) == ("Invocation5XXErrors", "Sum"):
            return 6.0 if late else 0.0
        if (name, stat) == ("ModelLatency", "p95"):
            return 250000.0 if late else 9000.0
        if (name, stat) == ("CPUUtilization", "SampleCount"):
            return 2.0 * period / 60 if late else period / 60.0
        return quiet(name, stat, t, period)

    aws.metric = busy
    s = NB.numbers(ws, "shop", span="1h")["summary"]
    assert s["errors"] == 36 and round(s["error_rate"], 4) == 0.005 and s["p50_ms"] == 3.0 and s["p95_ms"] == 250.0 and s["copies_peak"] == 2


def test_aws_cost_is_the_hours_a_copy_was_up_at_the_list_price(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    c = NB.numbers(ws, "shop")["cost"]
    hourly = next(s for s in SZ.sizes("aws", "us-west-1") if s["name"] == "ml.t2.medium")["hourly_usd"]
    assert c["source"] == "cloud" and c["machine"] == "ml.t2.medium" and c["hourly_usd"] == round(hourly, 4)
    # The stand-in reports one copy up for every hour of the month so far.
    assert c["copy_hours_month"] >= c["copy_hours_today"] > 0 and c["month_usd"] == pytest.approx(c["copy_hours_month"] * hourly, abs=1e-3)
    assert c["today_usd"] == pytest.approx(c["copy_hours_today"] * hourly, abs=1e-3) and c["copy_hours_today"] <= 25
    assert "Your AWS bill is the final word." in c["text"] and "at the list price for ml.t2.medium" in c["text"]


def test_the_numbers_are_kept_for_a_minute_because_each_read_costs(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    NB.numbers(ws, "shop")
    first = _reads(aws)
    NB.numbers(ws, "shop")
    NB.numbers(ws, "shop")
    assert _reads(aws) == first == 2  # one read for the range, one for the month's cost
    NB.numbers(ws, "shop", span="15m")
    assert _reads(aws) == 3 and 30 <= NB.KEEP_S <= 120  # another range is another read, and the cost is still kept


def test_when_cloudwatch_cannot_be_read_the_request_log_answers_and_says_so(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    for q in ("jacket", "tent", "boots"):
        SG.query(ws, "shop", q)
    SG.call(ws, SG.state(ws, "shop"), "flush", {})
    aws.fail_on = "get_metric_data"
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] == 3 and n["summary"]["cpu"] is None
    assert "Captain couldn't read AWS's own metrics just now" in n["not_read"][0] and "from the request log" in n["not_read"][0]
    assert n["cost"]["source"] == "record"
    aws.fail_on = None


def test_modal_says_what_it_does_not_read_and_uses_the_request_log(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] >= 5  # the deploy's own test requests
    assert n["not_read"] == [NB.NOT_READ["modal"]] and n["summary"]["cpu"] is None and all(b["cpu"] is None for b in n["buckets"])
    assert n["cost"]["source"] == "record" and "not from the cloud" in n["cost"]["text"]


def test_nothing_deployed_and_this_computer(ws):  # noqa: F811
    n = NB.numbers(ws, "shop")
    assert n["source"] == "none" and n["text"] == "Nothing is deployed, so there is nothing to count." and n["buckets"] == []
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    SG.query(ws, "shop", "jacket")
    SG.call(ws, SG.state(ws, "shop"), "flush", {})
    n = NB.numbers(ws, "shop")
    assert n["source"] == "log" and n["summary"]["requests"] == 1 and n["not_read"] == []
    SG.unstage(ws, "shop")
