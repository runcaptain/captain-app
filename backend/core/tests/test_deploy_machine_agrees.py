"""The price on a place's card and the machine it names are one fact: they come from the same choice, on the card,
in the confirm and after a size is picked in Settings."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from captain_search import server
from captain_search import settings as ST
from captain_search.staging import deploy as DP
from captain_search.staging import sizing as SZ

from test_deploy_one_call import logins  # noqa: F401  (fixture)
from test_staging import keychain, ws  # noqa: F401  (fixtures)

AWS = {"name": "aws-test", "provider": "aws", "auth": "default", "region": "us-west-1", "fields": {},
       "last_test": {"ok": True, "identity": "arn:aws:iam::000000000000:user/dev"}}


def _aws(ctx):
    return next(h for h in DP.offers(ctx, "shop")["hosts"] if h["host"] == "aws")


def test_the_card_prices_the_machine_it_names_and_the_confirm_names_the_same_one(ws, logins):  # noqa: F811
    logins.append(dict(AWS))
    h = _aws(ws)
    m = h["machine"]
    assert m["by"] == "smallest" and m["name"] in m["text"], "the sentence names the machine"
    assert h["estimate"]["hourly_usd"] == pytest.approx(m["hourly_usd"], abs=1e-3), "the card's price is that machine's price"
    p = DP.plan(ws, "shop", "aws", "aws-test")
    assert p["machine"]["name"] == m["name"] and p["machine"]["text"] == m["text"], "the confirm names the same machine, in the same words"
    assert p["cost"]["hourly_usd"] == pytest.approx(m["hourly_usd"], abs=1e-3), "and prices the same one"


def test_a_bigger_machine_chosen_in_settings_changes_the_name_and_the_price_together(ws, logins, monkeypatch):  # noqa: F811
    logins.append(dict(AWS))
    before = _aws(ws)
    bigger = next(x for x in before["machines"] if x["hourly_usd"] > before["machine"]["hourly_usd"])
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    aws = next(x for x in c.get("/api/serving/machines", headers=hd).json()["hosts"] if x["host"] == "aws")
    assert aws["chosen"] is None and aws["label"] == "AWS"
    assert [m["name"] for m in aws["machines"]] == [m["name"] for m in SZ.offered("aws", None)]
    assert all({"name", "memory_mb", "hourly_usd"} <= set(m) for m in aws["machines"])
    assert c.put("/api/settings/serving", headers=hd, json={"host": "aws", "machine": bigger["name"]}).status_code == 200
    assert next(x for x in c.get("/api/serving/machines", headers=hd).json()["hosts"] if x["host"] == "aws")["chosen"] == bigger["name"]
    after = _aws(ws)
    assert after["machine"]["name"] == bigger["name"] and after["machine"]["by"] == "setting"
    assert after["estimate"]["hourly_usd"] == pytest.approx(after["machine"]["hourly_usd"], abs=1e-3)
    assert after["estimate"]["hourly_usd"] > before["estimate"]["hourly_usd"], "the price moved with the machine"
    assert DP.plan(ws, "shop", "aws", "aws-test")["machine"]["name"] == bigger["name"]
    assert c.put("/api/settings/serving", headers=hd, json={"host": "aws", "machine": "ml.nonsense"}).status_code == 400
    # back to the smallest that fits
    assert c.put("/api/settings/serving", headers=hd, json={"host": "aws", "machine": ""}).status_code == 200
    assert _aws(ws)["machine"]["name"] == before["machine"]["name"]
    assert (ST.get(ws).get("serving") or {}).get("machine", {}).get("aws") is None
