"""Sample rows for the Deployments screen's pictures: props for `captain-deployments:<view>`, as JSON on stdout.
Nothing is deployed and no cloud is asked: the screen draws what it is handed, and its buttons reach no service.
Views: empty, leftover, stopped, one, several, failed, found, model. Demo names only."""
import datetime as dt
import json
import math
import sys

view = sys.argv[1] if len(sys.argv) > 1 else "several"
now = dt.datetime.now(dt.timezone.utc)
ago = lambda **kw: (now - dt.timedelta(**kw)).isoformat(timespec="seconds")  # noqa: E731


def row(pid, name, version, cloud, label, region, machine, state, text, **kw):
    where = ", ".join(x for x in (label, region, machine) if x)
    return {"id": f"{pid}:staging", "project": pid, "project_name": name, "env": "staging", "model_version": version, "cloud": cloud, "cloud_label": label,
            "login": kw.pop("login", "work-" + cloud), "region": region, "machine": machine, "where": where, "name": f"captain-{pid}-staging",
            "copies": {"now": kw.pop("copies", 1), "min": None, "max": None}, "state": state, "state_label": state.capitalize(), "state_text": text,
            "since": kw.pop("since", ago(hours=5)), "by": kw.pop("by", "dana"), "link": kw.pop("link", "linked"), "corrected": kw.pop("corrected", []),
            "sleeps": cloud != "aws", "address": kw.pop("address", None), "stopped_at": kw.pop("stopped_at", None), "can": [], **kw}


def numbers(span, per_minute, cloud=True, errors=0.0):
    seconds, period = {"15m": (900, 60), "1h": (3600, 60), "24h": (86400, 300), "7d": (604800, 3600)}[span]
    n = seconds // period
    minutes = period / 60
    b = []
    for i in range(n):
        wave = 0.55 + 0.45 * math.sin(i / max(1, n) * 6.28 * 2 + 1) + (0.25 if i % 17 == 0 else 0)
        req = round(per_minute * minutes * max(0.05, wave))
        b.append({"t": str(i), "requests": req, "errors": round(req * errors) if i % 9 == 0 else 0, "p50_ms": round(2.6 + wave, 2), "p95_ms": round(8 + 6 * wave, 2),
                  "cpu": round(14 + 22 * wave, 1) if cloud else None, "memory": round(5 + wave, 1) if cloud else None, "copies": None})
    total = sum(x["requests"] for x in b)
    errs = sum(x["errors"] for x in b)
    words = {"15m": "the last 15 minutes", "1h": "the last hour", "24h": "the last 24 hours", "7d": "the last 7 days"}[span]
    return {"range": span, "range_text": words, "bucket_seconds": period, "buckets": b,
            "source": "cloud" if cloud else "log",
            "source_text": "From AWS's own metrics (CloudWatch). They arrive a minute or two late." if cloud else "From Captain's request log, which the model writes every few seconds.",
            "not_read": [] if cloud else ["Modal keeps no history of an app's requests that Captain can read. CPU, memory and the cost so far are not read yet."],
            "summary": {"requests": total, "errors": errs, "per_minute": round(total / (seconds / 60), 1), "per_minute_peak": round(max(x["requests"] for x in b) / minutes, 1),
                        "p50_ms": 3.1, "p95_ms": 13.6, "cpu": 24.0 if cloud else None, "cpu_peak": 61.0 if cloud else None, "memory": 5.4 if cloud else None,
                        "memory_peak": 6.2 if cloud else None, "copies_now": 1 if cloud else None, "copies_min": 1 if cloud else None, "copies_max": 3 if cloud else None,
                        "copies_text": "1 copy up now. It may run 1 to 3." if cloud else None},
            "cost": {"hourly_usd": 0.066 if cloud else 0.063, "rate_usd": 0.066 if cloud else 0.063, "today_usd": 0.92 if cloud else 0.11, "month_usd": 14.3 if cloud else 1.86,
                     "text": ("About $0.07 an hour for each copy while it is up. Today: about $0.92. This month: about $14.30. Worked out from Captain's own record of "
                              "when it was up, at the list price for ml.t2.medium. Your AWS bill is the final word.") if cloud else
                             "About $0.06 an hour while a copy is awake, and nothing while it sleeps. Worked out from Captain's own record of when it was up, not from the cloud."},
            "text": f"{total:,} requests in {words}, " + (f"{errs:,} errors" if errs else "no errors") + ". Median 3.1 ms, 95th percentile 13.6 ms at the endpoint."}


SHOP = row("shop", "Demo shop", "shop/starter-ranker@v2", "aws", "AWS", "us-west-1", "ml.t2.medium", "running", "Running on 1 copy.")
TRAIL = row("trail", "Trail gear", "trail/starter-ranker@v1", "modal", "Modal", None, "1 CPU, 2 GB", "asleep",
            "Asleep. It wakes on the next request and bills nothing until then.", login="modal", copies=0, address="https://demo--captain-trail-staging.modal.run", since=ago(days=2))
GARDEN = row("garden", "Garden centre", "garden/keyword-baseline@v1", "gcp", "Google Cloud", "us-central1", "1 vCPU, 2 GB", "running", "Running.", by="sam", since=ago(hours=30), copies=None)
KIDS = row("kids", "Kids range", "kids/starter-ranker@v4", "azure", "Azure", "eastus", "0.75 vCPU, 1.5 GB", "stopped", "Stopped: no requests for 240 minutes.", stopped_at=ago(days=3), by="sam")
GIFTS = row("gifts", "Gift finder", "gifts/starter-ranker@v3", "aws", "AWS", "us-west-1", "ml.t2.medium", "failed",
            "Failed: The primary container did not pass the ping health check.", since=ago(minutes=40),
            corrected=["Captain's record said it was running. The cloud says it failed, so the record now says failed."])
FOUND = {**row("an-older-project", None, None, "aws", "AWS", "us-west-1", "ml.m5.large", "running", "Found in your AWS account, not linked to a project.", link="unlinked", by=None, since=ago(days=19)),
         "id": "cloud:aws:work-aws:us-west-1:captain-old-experiment-staging", "name": "captain-old-experiment-staging"}
ASKED = [{"cloud": "aws", "cloud_label": "AWS", "login": "work-aws", "region": "us-west-1", "ok": True, "error": None, "found": 2}]
CREATED = {"places": [
    {"cloud": "aws", "cloud_label": "AWS", "login": "work-aws", "region": "us-west-1", "in_use_by": ["Demo shop"], "can_remove": False, "text": "", "things": [
        {"kind": "role", "kind_label": "Role", "name": "captain-serving", "what_for": "What a running model is allowed to do: read its own files and write its request log. Nothing else."},
        {"kind": "bucket", "kind_label": "Bucket", "name": "captain-serving-us-west-1", "what_for": "Holds the model's files and its request log."},
        {"kind": "ecr-repository", "kind_label": "Image repository", "name": "captain-serving", "what_for": "Holds the model's image in your account."},
        {"kind": "codebuild-project", "kind_label": "Image build", "name": "captain-serving-build", "what_for": "Builds the model's image inside your account. It runs only when an image is built."}]},
    {"cloud": "azure", "cloud_label": "Azure", "login": "work-azure", "region": "eastus", "in_use_by": [], "can_remove": True, "text": "", "things": [
        {"kind": "resource-group", "kind_label": "Resource group", "name": "captain-serving", "what_for": "The folder in your account that holds everything else here, so it can be removed whole."},
        {"kind": "container-registry", "kind_label": "Image registry", "name": "captainserving", "what_for": "Holds the model's image in your account."}]}], "text": ""}

KIDS_AWS = row("kids", "Kids range", "kids/starter-ranker@v1", "aws", "AWS", "us-west-1", "ml.t2.medium", "stopped", "Stopped.", stopped_at=ago(minutes=50), by="dana")
LEFT = {"cloud": "aws", "cloud_label": "AWS", "login": "aws", "login_shown": None, "region": "us-west-1", "in_use_by": [], "can_remove": True, "text": "", "things": [
    {"kind": "s3-bucket", "kind_label": "Bucket", "name": "captain-serving-000000000000-us-west-1", "short": "captain-serving-000000000000-us-west-1", "what_for": "Holds the model's files and its request log."},
    {"kind": "iam-role", "kind_label": "Role", "name": "captain-serving", "short": "captain-serving", "what_for": "What a running model is allowed to do: read its own files and write its request log. Nothing else."},
    {"kind": "ecr-repository", "kind_label": "Image repository", "name": "captain-serving", "short": "captain-serving", "what_for": "Holds the model's image in your account."},
    {"kind": "iam-role", "kind_label": "Role for the image build", "name": "captain-serving-build", "short": "captain-serving-build", "what_for": "Lets the image build read its files and save the image. Nothing else."},
    {"kind": "s3-object", "kind_label": "Build package", "name": "s3://captain-serving-000000000000-us-west-1/build/serving.zip", "short": "serving.zip", "what_for": "The files the model's image is built from. Kept so the image can be built again without sending them."},
    {"kind": "codebuild-project", "kind_label": "Image build", "name": "captain-serving-build", "short": "captain-serving-build", "what_for": "Builds the model's image inside your account. It runs only when an image is built."},
    {"kind": "log-group", "kind_label": "Build log", "name": "/aws/codebuild/captain-serving-build", "short": "captain-serving-build", "what_for": "What the image build printed while it ran. Kept so a build that failed can be read."}]}

sets = {
    "empty": ([], "Nothing is deployed. Deploy a model from a project's page to see it here.", None, "all"),
    # What Edgar's screen held on 2026-11-12: one model, stopped, and what its first deploy left in the account.
    "stopped": ([KIDS_AWS], "0 deployed, 1 not running.", {"places": [LEFT], "text": ""}, "all"),
    # Nothing deployed, with what an earlier deploy left in the account: the page still has something to manage.
    "leftover": ([], "Nothing is deployed.", {"places": [{**CREATED["places"][0], "in_use_by": [], "can_remove": True}], "text": ""}, "running"),
    "one": ([SHOP], "1 deployed.", CREATED, "running"),
    "several": ([SHOP, GARDEN, TRAIL, KIDS], "3 deployed, 1 not running.", CREATED, "all"),
    "failed": ([GIFTS, SHOP, KIDS], "1 deployed, 2 not running.", None, "all"),
    "found": ([SHOP, FOUND], "1 deployed, 1 found in your cloud with no project.", None, "running"),
    "model": ([SHOP], "1 deployed.", None, "running"),
}
rows, text, created, show = sets[view]
demo = {"deployed": {"rows": rows, "asked": ASKED if rows else [], "text": text, "read_at": now.isoformat(timespec="seconds")}, "created": created, "show": show,
        "numbers": {"shop:staging": numbers("24h", 6.0), "garden:staging": numbers("24h", 1.4, cloud=False), "trail:staging": numbers("24h", 0.3, cloud=False),
                    "gifts:staging": numbers("24h", 2.0, errors=0.4)}}
if view == "model":
    demo["open"] = "shop:staging"
    demo["model"] = {"numbers": {k: numbers(k, 6.0) for k in ("15m", "1h", "24h", "7d")},
                     "requests": {"rows": [{"ts": ago(seconds=20 * i), "query": q, "model_version": "shop/starter-ranker@v2", "ms": 2.8 + i * 0.3}
                                           for i, q in enumerate(["trail running shoes", "waterproof jacket", "kids rain boots", "camping stove"])]},
                     "log": {"rows": [{"ts": ago(minutes=300), "text": "Loaded 480 products.", "level": "info"}]},
                     "versions": {"rows": [{"n": 2, "model_version": "shop/starter-ranker@v2", "at": ago(hours=5), "by": "dana", "live": True},
                                           {"n": 1, "model_version": "shop/keyword-baseline@v1", "at": ago(days=6), "by": "dana", "live": False, "can_roll_back": True}]},
                     "scale": {"text": "Keeps 1 up at all times. It never sleeps, and bills for each copy by the hour. 1 up now."}}
print(json.dumps({"demo": demo}))
