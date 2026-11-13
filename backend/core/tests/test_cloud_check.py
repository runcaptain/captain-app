"""A field of the cloud login form is checked as it is typed: a region that cannot exist is said under the field,
not after the button. Nothing here calls a cloud."""
from captain_search import cloud


def test_an_aws_region_is_checked_against_the_list():
    assert cloud.check_field("aws", "region", "us-west-1") == {"ok": True, "text": None}
    assert cloud.check_field("aws", "region", "") == {"ok": True, "text": None}  # a region is optional
    assert cloud.check_field("aws", "region", "   ") == {"ok": True, "text": None}
    bad = cloud.check_field("aws", "region", "us-west-9")
    assert bad["ok"] is False and bad["text"].startswith("AWS has no region called us-west-9.") and "us-west-1" in bad["text"]
    assert cloud.check_field("aws", "region", "mars-1")["text"] == "AWS has no region called mars-1. A region looks like us-west-1."
    assert cloud.check_field("aws", "region", "us-gov-west-1")["ok"]


def test_google_and_azure_regions_are_checked_by_shape_only():
    assert cloud.check_field("gcp", "region", "us-central1")["ok"] and cloud.check_field("gcp", "region", "europe-west4")["ok"]
    assert cloud.check_field("gcp", "region", "US Central") == {"ok": False, "text": "That does not look like a Google Cloud region. One looks like us-central1."}
    assert cloud.check_field("azure", "region", "eastus")["ok"] and cloud.check_field("azure", "region", "westeurope2")["ok"]
    assert cloud.check_field("azure", "region", "East US")["text"] == "That does not look like an Azure region. One looks like eastus."


def test_modal_has_no_region_and_other_fields_are_not_checked():
    assert cloud.check_field("modal", "region", "us-east") == {"ok": False, "text": "Modal has no regions to choose. Leave this empty."}
    assert cloud.check_field("modal", "region", "")["ok"]
    assert cloud.check_field("aws", "profile", "anything at all")["ok"] and cloud.check_field("nope", "region", "x")["ok"]


def test_the_route_answers_for_the_form(monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server

    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.get("/api/cloud/check", params={"provider": "aws", "field": "region", "value": "us-west-1"}, headers=h).json() == {"ok": True, "text": None}
    r = c.get("/api/cloud/check", params={"provider": "aws", "field": "region", "value": "nowhere-1"}, headers=h).json()
    assert r["ok"] is False and "AWS has no region called nowhere-1." in r["text"]
    assert c.get("/api/cloud/check", params={"provider": "gcp", "field": "region"}, headers=h).json()["ok"]  # no value at all
