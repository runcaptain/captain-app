"""A deploy runs on the smallest machine the model fits on, on every host: the need comes from the fit check, the
machine must have twice that and at least 1 GB to spare, and the cheapest size with that much is picked. A person
can choose a bigger one in Settings. Nothing here calls a real cloud."""
import pytest

from captain_search import compute as C
from captain_search import settings as ST
from captain_search.staging import deploy as DP
from captain_search.staging import fit as FT
from captain_search.staging import sizing as SZ

from test_deploy_one_call import LOGINS, _events, logins  # noqa: F401  (fixtures)
from test_hosting_clouds import azure, google  # noqa: F401  (fixtures)
from test_hosting_modal import modal  # noqa: F401  (fixtures)
from test_serving_setup import Fake as FakeAwsSetup
from test_staging import keychain, ws  # noqa: F401  (fixtures)

REGION = {"aws": "us-west-1", "modal": None, "gcp": "us-central1", "azure": "eastus"}
SMALL = 205.0  # about what the demo store's ranker beside its baseline needs


def test_the_room_is_twice_the_need_and_at_least_a_gigabyte_to_spare():
    assert SZ.room_mb(205) == 1229 and SZ.room_mb(1024) == 2048 and SZ.room_mb(3000) == 6000


@pytest.mark.parametrize("host,name,memory", [("aws", "ml.t2.medium", 4096), ("modal", "1 CPU, 2 GB", 2048), ("gcp", "1 vCPU, 2 GB", 2048),
                                              ("azure", "0.75 vCPU, 1.5 GB", 1536)])
def test_a_small_model_gets_the_cheapest_size_with_room(ws, host, name, memory):  # noqa: F811
    m = SZ.pick(ws, host, REGION[host], SMALL)
    assert (m["name"], m["memory_mb"], m["by"]) == (name, memory, "smallest") and m["memory_mb"] >= SZ.room_mb(SMALL)
    assert m["text"] == f"{name}: this model needs about 205 MB, and this has {FT._gb(memory)}. The smallest that leaves room to spare."
    # Nothing cheaper has the room.
    assert all(s["memory_mb"] < SZ.room_mb(SMALL) for s in SZ.sizes(host, REGION[host]) if s["hourly_usd"] < m["hourly_usd"] - 1e-5)


@pytest.mark.parametrize("host,name", [("aws", "ml.m5.large"), ("modal", "2 CPU, 8 GB"), ("gcp", "2 vCPU, 8 GB"), ("azure", "3 vCPU, 6 GB")])
def test_a_bigger_model_moves_up_a_size(ws, host, name):  # noqa: F811
    assert SZ.pick(ws, host, REGION[host], 3000.0)["name"] == name


@pytest.mark.parametrize("host,need", [("aws", 20000.0), ("modal", 20000.0), ("gcp", 20000.0), ("azure", 5000.0)])
def test_a_model_too_big_for_the_largest_size_is_refused_in_a_sentence(ws, host, need):  # noqa: F811
    with pytest.raises(SZ.TooBig) as e:
        SZ.pick(ws, host, REGION[host], need)
    assert f"needs about {FT._gb(need)}" in str(e.value) and f"The largest Captain offers on {SZ.WHERE[host]}" in str(e.value)
    assert "Pick another place for it." in str(e.value)


def test_on_aws_the_cheapest_is_by_price_in_that_region_not_by_name(ws, monkeypatch):  # noqa: F811
    from captain_search import price_table as PT

    monkeypatch.setitem(PT.SAGEMAKER_HOSTING, "xx-test-1", {"ml.t2.medium": 0.50, "ml.c5.large": 0.10, "ml.m5.large": 0.20})
    assert SZ.pick(ws, "aws", "xx-test-1", SMALL)["name"] == "ml.c5.large"


def test_a_person_can_choose_a_bigger_machine_and_not_one_too_small(ws):  # noqa: F811
    ST.set_serving_machine(ws, "aws", "ml.m5.large")
    m = SZ.pick(ws, "aws", "us-west-1", SMALL)
    assert (m["name"], m["by"]) == ("ml.m5.large", "setting") and m["text"].endswith("Your choice in Settings.")
    with pytest.raises(ValueError, match="Choose a bigger one, or clear the choice"):
        SZ.pick(ws, "aws", "us-west-1", 5000.0)  # wants 10 GB, and ml.m5.large has 8
    ST.set_serving_machine(ws, "aws", "")
    assert SZ.pick(ws, "aws", "us-west-1", SMALL)["name"] == "ml.t2.medium"
    with pytest.raises(ValueError, match="is not a size Captain offers on Modal"):
        ST.set_serving_machine(ws, "modal", "ml.m5.large")
    with pytest.raises(ValueError, match="the host is one of"):
        ST.set_serving_machine(ws, "local", "x")


def test_the_place_and_the_plan_say_which_machine_and_price_it(ws, logins, modal, monkeypatch):  # noqa: F811
    from captain_search import cloud
    from captain_search.staging import serving_setup as SS

    f = FakeAwsSetup()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: f)
    monkeypatch.setattr(SS, "docker_answers", lambda timeout=5.0: False)
    ws.workspace.storage_uri = "s3://team-bucket/captain/shop"
    logins.extend([LOGINS["aws"], LOGINS["modal"]])
    need = DP._need_mb(ws, {"candidate": ws.candidate, "baseline": ws.baseline})
    hosts = {h["host"]: h for h in DP.offers(ws, "shop")["hosts"]}
    a, m = hosts["aws"], hosts["modal"]
    assert a["machine"]["name"] == "ml.t2.medium" and a["machine"]["need_mb"] == round(need) and a["machine"]["by"] == "smallest"
    assert m["machine"]["name"] == "1 CPU, 2 GB" and [x["name"] for x in a["machines"]][:2] == ["ml.t2.medium", "ml.c5.large"]
    # The card's price is the picked machine's, not ml.m5.large's.
    assert a["estimate"]["hourly_usd"] == pytest.approx(a["machine"]["hourly_usd"], rel=0.25) and a["estimate"]["hourly_usd"] < 0.10
    p = DP.plan(ws, "shop", "modal")
    line = p["creates"]["groups"][-1]["items"][-1]
    assert line["resource"] == "Machine" and line["name"] == "1 CPU, 2 GB" and line["why"] == p["machine"]["text"]
    assert p["machine"]["config"] == {"cpu": "1", "memory_mb": "2048"} and p["creates"]["count"] == 4


def test_a_model_too_big_for_a_host_is_not_deployable_there_and_says_why(ws, logins, modal, monkeypatch):  # noqa: F811
    logins.append(LOGINS["modal"])
    monkeypatch.setattr(DP, "_need_mb", lambda ctx, arms: 20000.0)
    m = next(h for h in DP.offers(ws, "shop")["hosts"] if h["host"] == "modal")
    assert not m["can_deploy"] and m["machine"] is None and "The largest Captain offers on Modal is 8 CPU, 32 GB" in m["why_not"]
    with pytest.raises(ValueError, match="The largest Captain offers on Modal"):
        DP.plan(ws, "shop", "modal")


def test_the_deploy_starts_the_model_on_the_picked_machine(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    seen, emit = _events()
    DP.run(ws, "shop", "modal", confirm=True, on_event=emit)
    t = C.get(ws, next(h for h in DP.offers(ws, "shop")["hosts"] if h["host"] == "modal")["target"])
    assert (t["config"]["cpu"], t["config"]["memory_mb"]) in (("1", "2048"), ("1.0", "2048"))
    # A bigger choice in Settings is used on the next deploy, and said in the run.
    ST.set_serving_machine(ws, "modal", "1 CPU, 4 GB")
    seen, emit = _events()
    DP.run(ws, "shop", "modal", confirm=True, on_event=emit)
    assert C.get(ws, t["name"])["config"]["memory_mb"] == "4096"
    assert any("Using 1 CPU, 4 GB" in str(d) for _k, d in seen)


def test_a_workspace_name_prefix_names_what_a_first_deploy_makes(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    with pytest.raises(ValueError, match="3 to 26 lower-case letters"):
        ST.set_serving_prefix(ws, "Bad Name!")
    ST.set_serving_prefix(ws, "team-two")
    p = DP.plan(ws, "shop", "modal")
    made = [x["name"] for g in p["creates"]["groups"] for x in g["items"] if x["resource"] != "Machine"]
    # The volume and the app carry it. The compute target ("modal-serving") is a name inside Captain, not in the cloud.
    assert "team-two-serving" in made and "team-two-{project}-staging" in made
    DP.run(ws, "shop", "modal", confirm=True)
    assert modal.volumes and all(any(str(part).startswith("team-two") for part in v) for v in modal.volumes)
    ST.set_serving_prefix(ws, "")
    assert "name_prefix" not in (ST.get(ws).get("serving") or {})


def test_on_aws_the_prefix_names_the_serving_bucket_and_the_endpoint_too(ws, logins, monkeypatch):  # noqa: F811
    from captain_search import cloud
    from captain_search.staging import serving_setup as SS

    f = FakeAwsSetup()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: f)
    monkeypatch.setattr(SS, "docker_answers", lambda timeout=5.0: False)
    logins.append(LOGINS["aws"])
    ST.set_serving_prefix(ws, "team-two")
    p = DP.plan(ws, "shop", "aws")
    made = {x["resource"]: x["name"] for g in p["creates"]["groups"] for x in g["items"]}
    # A workspace kept on this computer gets a serving bucket: named with the prefix, like the roles and the repository.
    assert made["S3 bucket"].startswith("team-two-serving-") and made["SageMaker endpoint"] == "team-two-{project}-staging"
    assert made["ECR repository"] == "team-two-serving" and made["CodeBuild project"] == "team-two-serving-build"
    assert SS._serving_uri(None, "000000000000", "us-west-1", "w") == "s3://captain-serving-000000000000-us-west-1/w"
