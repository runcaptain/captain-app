"""Hosting on Modal, against a stand-in for Modal: what setup makes, what a deploy makes and removes, what happens
when Modal refuses, and that Captain never touches an app it did not make. The stand-in endpoint runs the real
runtime over the real bundle, read from the stand-in volume, so a query goes the whole way."""
import io
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from captain_search import compute as C
from captain_search import staging as SG
from captain_search.compute_providers import ProviderError, serving as SV
from captain_search.compute_providers.modal_serving import ModalServingProvider
from captain_search.staging import hosts as H
from captain_search.staging import runtime as RT
from captain_search.storage import modal_volume as MV
from captain_search.storage.backend import AlreadyExists, Conflict, LocalStore, NotFound
from captain_search.storage.registry import Registry
from captain_search.workspace import Context

from test_staging import keychain, ws  # noqa: F401  (fixtures)

ENV = "captain-search-test"
URL = "https://w--captain-shop-endpoint-web.modal.run"


class NotFoundError(Exception):
    pass


class FakeModal:
    """One Modal workspace: apps, secrets and volumes, by (environment, name)."""

    def __init__(self, ctx, tmp):
        self.ctx, self.tmp = ctx, tmp
        self.apps, self.secrets, self.volumes = {}, {}, {}
        self.fail_deploy = None   # a sentence Modal's deploy fails with
        self.signed_out = False
        self.copies = 1
        self.calls = []           # requests that reached the endpoint
        self.autoscaler = None
        self.clients = {}
        self.stops = []

    # -- what `import modal` gives --
    def module(self):
        w = self
        m = types.ModuleType("modal")

        def gate():
            if w.signed_out:
                raise type("AuthError", (Exception,), {})("Token missing. Could not authenticate client.")

        class App:
            def __init__(self, env, name):
                self.env, self.name = env, name

            @staticmethod
            def lookup(name, environment_name=None, **_kw):
                gate()
                a = w.apps.get((environment_name, name))
                if not a or a["stopped"]:
                    raise NotFoundError(f"App '{name}' not found in environment '{environment_name}'")
                return App(environment_name, name)

            def get_tags(self):
                return dict(w.apps[(self.env, self.name)]["tags"])

            def stop(self):
                w.stops.append(self.name)
                w.apps[(self.env, self.name)]["stopped"] = True

        class Objects:
            def __init__(self, d, kind):
                self.d, self.kind = d, kind

            def create(self, name, *a, environment_name=None, **_kw):
                gate()
                if (environment_name, name) in self.d:
                    raise Exception(f"{self.kind} '{name}' already exists")
                self.d[(environment_name, name)] = a[0] if a else {}

            def delete(self, name, allow_missing=False, environment_name=None, **_kw):
                gate()
                if (environment_name, name) not in self.d and not allow_missing:
                    raise NotFoundError(name)
                self.d.pop((environment_name, name), None)

            def list(self, environment_name="", **_kw):
                return [types.SimpleNamespace(name=n) for (e, n) in self.d if (e or "") == (environment_name or "")]

        class Volume:
            objects = Objects(w.volumes, "Volume")

            @staticmethod
            def from_name(name, environment_name=None, **_kw):
                def hydrate():
                    gate()
                    if (environment_name, name) not in w.volumes:
                        raise NotFoundError(f"Volume '{name}' not found")
                return types.SimpleNamespace(hydrate=hydrate)

        class Secret:
            objects = Objects(w.secrets, "Secret")

        class Fn:
            def __init__(self, env, app):
                self.env, self.app = env, app

            def get_web_url(self):
                return URL

            def get_current_stats(self):
                if (self.env, self.app) not in w.apps or w.apps[(self.env, self.app)]["stopped"]:
                    raise NotFoundError("not found")
                return types.SimpleNamespace(num_total_runners=w.copies, backlog=0)

            def update_autoscaler(self, **kw):
                w.autoscaler = kw

        class Cls:
            @staticmethod
            def from_name(app, name, environment_name=None, **_kw):
                return lambda: types.SimpleNamespace(web=Fn(environment_name, app))

        m.App, m.Volume, m.Secret, m.Cls = App, Volume, Secret, Cls
        return m

    # -- the two commands the provider runs --
    def run(self, argv, env):
        e = next((argv[i + 1] for i, a in enumerate(argv) if a == "-e"), None)
        if "deploy" in argv:
            if self.fail_deploy:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=self.fail_deploy)
            name = env["CST_MODAL_SERVE_APP"]
            tags = {"app": "captain", **dict(p.split("=", 1) for p in env["CST_MODAL_SERVE_TAGS"].split(","))}
            self.apps[(e, name)] = {"tags": tags, "stopped": False, "env": dict(env)}
            told = self.secrets[(e, env["CST_MODAL_SERVE_SECRET"])]  # the endpoint is told what the secret holds
            store = self.store()
            rt = RT.Runtime(Context(self.ctx.workspace, store, Registry(store)), told["CST_STAGING_BUNDLE"], key_sha256=told["CST_STAGING_KEY_SHA256"])
            self.clients[URL] = TestClient(RT.make_app(rt))
            return types.SimpleNamespace(returncode=0, stdout="deployed", stderr="")
        if "list" in argv:
            rows = [{"Description": n, "State": "stopped" if a["stopped"] else "deployed"} for (env_, n), a in self.apps.items() if env_ == e]
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        raise AssertionError(argv)

    def store(self):
        return LocalStore(str(self.tmp / "volume" / "t"))

    def client(self, base):
        w = self

        class Counting:
            def get(self, url, **kw):
                w.calls.append(url)
                return w.clients[base].get(url, headers=kw.get("headers"))

            def post(self, url, **kw):
                w.calls.append(url)
                return w.clients[base].post(url, json=kw.get("json"), headers=kw.get("headers"))

        return Counting()


@pytest.fixture
def modal(ws, monkeypatch, tmp_path):  # noqa: F811
    w = FakeModal(ws, tmp_path)
    monkeypatch.setitem(sys.modules, "modal", w.module())
    monkeypatch.setattr(ModalServingProvider, "_run", lambda self, argv, env: w.run(argv, env))
    monkeypatch.setattr(ModalServingProvider, "READY_POLL_S", 0)
    monkeypatch.setattr(SV, "_client", w.client)
    monkeypatch.setattr(MV, "open_modal", lambda uri: w.store())
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: w.client(URL).get(url, **kw))
    return w


def _setup(ws, **opts):  # noqa: F811
    return H.get("modal").setup(ws, confirm=True, environment=ENV, name_prefix="captain-search-test", **opts)


# ---- setup ---------------------------------------------------------------------------------------


def test_the_plan_names_what_setup_makes_what_it_costs_and_changes_nothing(ws, modal):  # noqa: F811
    p = H.get("modal").plan(ws, environment=ENV, name_prefix="captain-search-test")
    assert [r["resource"] for r in p["creates"]] == ["Modal volume", "Captain compute target"]
    assert [r["resource"] for r in p["each_deploy"]] == ["Modal app", "Modal secret"]
    assert p["storage"] == f"modal://captain-search-test-serving@{ENV}/t"
    assert "nothing while it sleeps" in p["cost"]["text"] and p["cost"]["setup_usd"] == 0
    assert p["login_policy"] is None and "modal token new" in p["login_text"]
    assert not modal.volumes and H.get("modal").status(ws)["status"] == "none"
    with pytest.raises(ValueError, match="Confirm first"):
        H.get("modal").setup(ws, environment=ENV)


def test_setup_makes_the_volume_and_the_target_and_teardown_removes_both(ws, modal):  # noqa: F811
    r = _setup(ws)
    assert r["status"] == "ready" and (ENV, "captain-search-test-serving") in modal.volumes
    t = C.get(ws, "modal-serving")
    assert t["kind"] == "modal-endpoint" and t["config"]["serving_storage"].startswith("modal://") and C.can_stage(ws, t)
    assert _setup(ws)["status"] == "ready"  # safe to run again
    out = H.get("modal").teardown(ws)
    assert out["status"] == "removed" and not modal.volumes and "Nothing of Captain's is left" in out["text"]
    assert C.get(ws, "modal-serving").get("deleted") and H.get("modal").leftovers(ws) == []


def test_setup_refuses_a_volume_of_that_name_it_did_not_make(ws, modal):  # noqa: F811
    modal.volumes[(ENV, "captain-search-test-serving")] = {}
    with pytest.raises(ValueError, match="already exists there and Captain did not make it"):
        _setup(ws)
    assert (ENV, "captain-search-test-serving") in modal.volumes  # and it is still there


def test_a_computer_that_is_not_signed_in_gets_one_sentence(ws, modal):  # noqa: F811
    modal.signed_out = True
    with pytest.raises(ValueError, match="isn't signed in to Modal. Run `modal token new`"):
        _setup(ws)


# ---- stage, query, unstage -----------------------------------------------------------------------


def test_stage_query_and_unstage_leave_nothing_behind(ws, modal):  # noqa: F811
    _setup(ws)
    with pytest.raises(SG.NeedsConfirm):
        SG.stage(ws, "shop", target="modal-serving")
    r = SG.stage(ws, "shop", target="modal-serving", confirm=True)
    name = "captain-search-test-shop"
    assert r["status"] == "running" and r["scale_to_zero"] and r["endpoint"] == {"name": name, "url": URL, "environment": ENV}
    assert {(x["type"], x["name"]) for x in r["resources"]} == {("modal-app", name), ("modal-secret", name)}
    app = modal.apps[(ENV, name)]
    assert app["tags"]["app"] == "captain" and app["tags"]["project"] == "shop" and app["env"]["CST_MODAL_SERVE_MIN"] == "0"
    told = modal.secrets[(ENV, name)]
    key = SG._key(SG.state(ws, "shop"))
    assert key not in json.dumps(told) and len(told["CST_STAGING_KEY_SHA256"]) == 64  # the hash, never the key
    assert told["CST_STORAGE"].startswith("modal://") and told["CST_WORKSPACE"] == "t"
    # the bundle and the log are in the volume, and the workspace's own store has neither
    assert modal.store().list("staging/shop/bundles/") and not ws.store.list("staging/shop/bundles/")
    q = SG.query(ws, "shop", "jacket")
    assert q["staged"]["results"] and q["baseline"]["results"]
    assert SG.status(ws, "shop")["health"]["stops_in_s"] is None  # the host puts it to sleep, the runtime never stops itself
    out = SG.unstage(ws, "shop")
    assert out["status"] == "stopped" and not out["resources"]
    assert modal.apps[(ENV, name)]["stopped"] and (ENV, name) not in modal.secrets
    assert C.tagged(ws, C.get(ws, "modal-serving"), "shop", "staging") == []
    assert [x for x in H.get("modal").leftovers(ws) if x["type"] != "modal-volume"] == []
    # the request log was published into the workspace from this side, since the endpoint only holds the volume
    assert any("staging-logs" in k for k in ws.store.list("datasets/"))


def test_an_asleep_endpoint_is_not_woken_to_ask_how_it_is(ws, modal):  # noqa: F811
    _setup(ws)
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    modal.copies, modal.calls = 0, []
    s = SG.status(ws, "shop")
    assert s["asleep"] and "Asleep" in s["text"] and "wakes on the next request" in s["text"]
    assert modal.calls == []  # nothing was sent to the endpoint
    modal.copies = 1
    assert SG.status(ws, "shop")["health"]["requests"] == 0 and modal.calls


def test_a_deploy_that_fails_removes_the_secret_and_says_why(ws, modal):  # noqa: F811
    _setup(ws)
    modal.fail_deploy = "Image build failed: pip could not resolve dependencies"
    with pytest.raises(ProviderError, match="Modal could not deploy the endpoint: Image build failed"):
        SG.stage(ws, "shop", target="modal-serving", confirm=True)
    assert not modal.secrets and not [a for a in modal.apps.values() if not a["stopped"]]
    rec = SG.state(ws, "shop")
    assert rec["status"] == "failed" and rec["resources"] == []


def test_captain_never_deploys_over_or_stops_an_app_it_did_not_make(ws, modal):  # noqa: F811
    _setup(ws)
    name = "captain-search-test-shop"
    modal.apps[(ENV, name)] = {"tags": {"team": "someone-else"}, "stopped": False}
    with pytest.raises(ProviderError, match="already exists here and Captain did not make it"):
        SG.stage(ws, "shop", target="modal-serving", confirm=True)
    assert modal.stops == [] and not modal.apps[(ENV, name)]["stopped"] and "env" not in modal.apps[(ENV, name)]
    # even handed the name, unstage leaves an app without Captain's tag alone, and says so
    left = C.unstage(ws, C.get(ws, "modal-serving"), [{"type": "modal-app", "name": name, "environment": ENV}])
    assert left and "does not carry Captain's tag" in left[0]["error"] and modal.stops == []


def test_a_secret_of_that_name_it_did_not_make_stops_the_deploy(ws, modal):  # noqa: F811
    _setup(ws)
    modal.secrets[(ENV, "captain-search-test-shop")] = {"THEIRS": "1"}
    with pytest.raises(ProviderError, match="secret named .* already exists here and Captain did not make it"):
        SG.stage(ws, "shop", target="modal-serving", confirm=True)
    assert modal.secrets[(ENV, "captain-search-test-shop")] == {"THEIRS": "1"}


def test_teardown_is_refused_while_a_model_is_staged_there(ws, modal):  # noqa: F811
    _setup(ws)
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    with pytest.raises(ValueError, match="has a model staged on this target"):
        H.get("modal").teardown(ws)
    SG.unstage(ws, "shop")
    assert H.get("modal").teardown(ws)["status"] == "removed"


def test_copies_and_the_idle_time_can_be_changed(ws, modal):  # noqa: F811
    _setup(ws)
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    t, rec = C.get(ws, "modal-serving"), SG.state(ws, "shop")
    assert C.staged_scale(ws, t, rec)["min_copies"] == 0 and "Sleeps when idle" in C.staged_scale(ws, t, rec)["text"]
    out = C.set_staged_scale(ws, t, rec, min_copies=1, max_copies=3, idle_seconds=120)
    assert modal.autoscaler == {"min_containers": 1, "max_containers": 3, "scaledown_window": 120}
    assert out["min_copies"] == 1 and "Keeps 1 up" in out["text"]
    with pytest.raises(ValueError, match="between 2 and 1200 seconds"):
        C.set_staged_scale(ws, t, rec, idle_seconds=5000)
    a = C.staged_address(ws, t, rec)
    assert a["url"] == URL and "Bearer" in a["auth"]


# ---- the volume as a store -----------------------------------------------------------------------


class FakeVolume:
    def __init__(self):
        self.files, self.commits, self.reloads = {}, 0, 0

    def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        yield self.files[path]

    def batch_upload(self, force=False):
        vol = self

        class Up:
            def __enter__(self):
                self.put = []
                return self

            def put_file(self, fh, path):
                self.put.append((path, fh.read()))

            def __exit__(self, *exc):
                for path, data in self.put:
                    if path in vol.files and not force:
                        raise FileExistsError(path)
                    vol.files[path] = data

        return Up()

    def listdir(self, path, recursive=False):
        base = path.rstrip("/") + "/"
        got = [p for p in self.files if p.startswith(base)]
        if not got and path != "/":
            raise FileNotFoundError(path)
        return [types.SimpleNamespace(path=p.lstrip("/"), type=types.SimpleNamespace(name="FILE")) for p in got]

    def commit(self):
        self.commits += 1

    def reload(self):
        self.reloads += 1


def test_the_volume_store_from_this_computer_keeps_the_store_contract():
    v = FakeVolume()
    s = MV.ModalVolumeStore("vol", "env", "ws", vol=v)
    s.put_new("staging/a/bundle.json", b"one")
    with pytest.raises(AlreadyExists):
        s.put_new("staging/a/bundle.json", b"two")
    data, tag = s.get_with_tag("staging/a/bundle.json")
    assert data == b"one" and "/ws/staging/a/bundle.json" in v.files
    with pytest.raises(NotFound):
        s.get("staging/nope")
    with pytest.raises(Conflict):
        s.put_if("staging/a/bundle.json", b"x", "stale")
    s.put_if("staging/a/bundle.json", b"three", tag)
    s.put_if("staging/a/control.json", b"{}", None)
    s.put_new("staging/ab/other", b"z")
    assert s.list("staging/a/") == ["staging/a/bundle.json", "staging/a/control.json"]
    assert s.list("staging/a") == ["staging/a/bundle.json", "staging/a/control.json", "staging/ab/other"]  # by key prefix
    assert s.list("nothing/here/") == []
    assert MV.parse("modal://vol@env/ws") == ("vol", "env", "ws") and MV.parse("modal://vol/ws/x") == ("vol", None, "ws/x")


def test_the_mounted_volume_writes_create_only_without_hard_links_and_commits(tmp_path, monkeypatch):
    import os

    # A Modal volume refuses hard links: "PermissionError: [Errno 1] Operation not permitted" on the real one.
    monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")))
    v = FakeVolume()
    s = MV.MountedVolumeStore(str(tmp_path / "mnt"), v, reload_s=3600)
    s.put_new("staging/logs/requests-1.jsonl", b"{}")
    assert v.commits == 1 and s.get("staging/logs/requests-1.jsonl") == b"{}"
    with pytest.raises(AlreadyExists):
        s.put_new("staging/logs/requests-1.jsonl", b"again")
    assert s.put_new_chunks("staging/logs/requests-2.jsonl", [b"a", b"b"]) == 2 and v.commits == 2
    _, tag = s.get_with_tag("staging/logs/requests-2.jsonl")
    s.put_if("staging/logs/requests-2.jsonl", b"c", tag)
    with pytest.raises(Conflict):
        s.put_if("staging/logs/requests-2.jsonl", b"d", tag)
    assert s.list("staging/logs/") == ["staging/logs/requests-1.jsonl", "staging/logs/requests-2.jsonl"]
    s.reload()
    assert v.reloads == 1


def test_a_reload_never_runs_between_a_write_and_its_commit(tmp_path):
    """A reload throws away what is written and not yet committed. On a real volume a write that raced one failed
    with "No such file or directory": its folder had been made, then reloaded away."""
    import shutil
    import threading

    root = tmp_path / "mnt"
    seen = []

    class Vol(FakeVolume):
        def __init__(self):
            super().__init__()
            self.saved = set()

        def commit(self):
            super().commit()
            self.saved = {str(p) for p in root.rglob("*") if p.is_file()}

        def reload(self):  # what Modal does: anything not committed is gone
            super().reload()
            for p in list(root.rglob("*")):
                if p.is_file() and str(p) not in self.saved:
                    p.unlink()
            for p in sorted((x for x in root.rglob("*") if x.is_dir()), reverse=True):
                if not any(p.iterdir()):
                    shutil.rmtree(p)

    v = Vol()
    s = MV.MountedVolumeStore(str(root), v, reload_s=3600)
    stop = threading.Event()

    def reloader():
        while not stop.is_set():
            s.reload()

    th = threading.Thread(target=reloader)
    th.start()
    try:
        for i in range(300):
            s.put_new(f"staging/logs/stage-{i % 7}/requests-{i}.jsonl", b"x" * 2000)
            seen.append(i)
    finally:
        stop.set()
        th.join()
    assert len(seen) == 300 and len(s.list("staging/logs/")) == 300


def test_every_host_has_the_same_shape():
    cards = {c["cloud"]: c for c in H.listing()}
    assert list(cards) == ["local", "aws", "modal", "gcp", "azure"]
    for c in cards.values():
        assert {"label", "kind", "target", "scale_to_zero", "proven", "storage", "address", "options"} <= set(c)
    assert cards["aws"]["proven"] and not cards["aws"]["scale_to_zero"] and cards["modal"]["scale_to_zero"]
    for name in ("plan", "setup", "status", "teardown", "leftovers", "describe"):
        for cloud in cards:
            assert callable(getattr(H.get(cloud), name))
    from captain_search.compute_providers.aws import SageMakerProvider

    for name in ("stage_plan", "stage", "staged_status", "invoke", "unstage", "tagged", "stage_preflight"):
        assert callable(getattr(SageMakerProvider, name)) and callable(getattr(ModalServingProvider, name))


def test_a_proven_host_that_sleeps_is_priced_by_the_hour_awake(ws, modal):  # noqa: F811
    from captain_search import cost_estimates as CE

    _setup(ws)
    p = SG.plan(ws, "shop", target="modal-serving")
    assert p["cost"]["sleeps"] and "an hour while a copy is awake" in p["cost"]["text"] and "nothing while it sleeps" in p["cost"]["text"]
    assert "keeps billing" not in p["cost"]["text"] and p["needs_confirm"]
    e = CE.estimate(ws, "staging", "modal-serving")
    assert e["available"] and e["low"] == 0 and 0.05 < e["high"] < 0.08 and e["confidence"] == "rough"
    spent = CE.cost_of(ws, C.get(ws, "modal-serving"), 3600, staging=True)
    assert spent["upper_bound"] and spent["text"].startswith("Cost at most")


def test_the_deployment_tabs_say_asleep_and_never_wake_it(ws, modal):  # noqa: F811
    from captain_search.staging import observe as OB

    _setup(ws)
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    SG.query(ws, "shop", "jacket")
    up = OB.live(ws, "shop")
    assert up["state"] in ("handling", "idle") and up["copies"] == 1
    st = OB.settings(ws, "shop")
    assert [g["title"] for g in st["groups"]][1:3] == ["Copies", "Sleeping when idle"] and not st["can_edit"]
    assert st["groups"][2]["rows"][0] == ["An idle copy stays up", "5 min"]
    with pytest.raises(ValueError, match="there is no idle time to set"):
        OB.set_idle(ws, "shop", 60)
    assert "while a copy is awake" in OB.cost(ws, "shop")["rate_text"]
    modal.copies, modal.calls = 0, []
    live = OB.live(ws, "shop")
    assert live["state"] == "asleep" and live["copies"] == 0 and "nothing is billing" in live["text"]
    assert OB.copies(ws, "shop")["rows"] == [] and "asleep" in OB.copies(ws, "shop")["text"]
    OB.logs(ws, "shop")
    OB.metrics(ws, "shop")
    assert modal.calls == []  # reading the log and the metrics did not wake it either
    from captain_search.staging import traffic as TF

    c = TF.connect(ws, "shop")  # the page asks this on every load
    assert c["asleep"] and "Asleep: no copy is up" in c["live"] and "warning" not in c and modal.calls == []


def test_copies_from_the_cli_and_the_api_and_a_plan_with_nothing_to_warn_about(ws, modal, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient as TC
    from typer.testing import CliRunner

    from captain_search import server
    from captain_search.cli.main import app as cli

    _setup(ws)
    p = SG.plan(ws, "shop", target="modal-serving")
    assert p["preflight"]["checked"] and p["preflight"]["note"] is None and p["warnings"] == []
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    out = SG.scale(ws, "shop", max_copies=2)
    assert out["max_copies"] == 2 and SG.state(ws, "shop")["scale"] == {"max": 2} and modal.autoscaler["max_containers"] == 2
    with pytest.raises(ValueError, match="at least 1"):
        SG.scale(ws, "shop", max_copies=0)
    r = CliRunner().invoke(cli, ["projects", "staging-copies", "shop"])
    assert r.exit_code == 0 and "At most 2 at once" in r.output, r.output
    monkeypatch.setattr(server, "ctx", lambda: ws)
    c = TC(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.get("/api/projects/shop/staging/scale", headers=h).json()["max_copies"] == 2
    assert c.post("/api/projects/shop/staging/scale", json={"min_copies": 1}, headers=h).json()["min_copies"] == 1
    assert c.post("/api/projects/shop/staging/scale", json={"idle_seconds": 99999}, headers=h).status_code == 400
    # The Copies tab's own address still answers with the copies that are running: the two never share one.
    rows = c.get("/api/projects/shop/staging/copies", headers=h).json()
    assert "rows" in rows and len(rows["rows"]) == 1 and rows["rows"][0]["model_version"]
    cards = c.get("/api/serving/hosts", headers=h).json()
    assert [x["cloud"] for x in cards] == ["local", "aws", "modal", "gcp", "azure"] and cards[2]["setup"]["status"] == "ready"
    plan = c.get("/api/serving/setup", params={"cloud": "modal", "plan": "true", "environment": "captain-search-test"}, headers=h).json()
    assert plan["status"] == "ready" and plan["plan"]["creates"][0]["resource"] == "Modal volume"
    assert c.post("/api/serving/setup", json={"cloud": "modal"}, headers=h).status_code == 409  # the plan first, never a surprise
    assert c.get("/api/serving/setup", params={"cloud": "nowhere"}, headers=h).status_code == 400


def test_a_site_is_given_the_address_and_the_key_header_not_aws_calls(ws, modal):  # noqa: F811
    from captain_search.staging import ab as AB
    from captain_search.staging import traffic as TF

    _setup(ws)
    SG.stage(ws, "shop", target="modal-serving", confirm=True)
    c = TF.connect(ws, "shop")
    assert c["reachable"] and c["address"] == URL and c["text"].startswith(f"Running on Modal at {URL}")
    assert all(URL in code and "CAPTAIN_STAGING_KEY" in code and "sagemaker" not in code.lower() for code in c["snippets"].values())
    assert c["test_request"].startswith(f"curl -s {URL}/rank")
    assert AB.addresses(SG.state(ws, "shop"))["candidate"] == {"url": f"{URL}/rank/candidate", "auth": "Authorization: Bearer <the endpoint key>"}
