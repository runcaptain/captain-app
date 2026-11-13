"""Signing in to a cloud with the browser, from Captain (cloud_signin.py).

Every test here runs a STAND-IN tool: a small script named like the cloud's own tool, which prints a link, waits,
then succeeds or fails. No test starts a real cloud tool: `cloud_signin.TOOL_DIRS` is pointed at the stand-ins'
folder, and nothing outside it is looked in. Modal's settings file is a made-up one in a temporary folder."""
from __future__ import annotations

import os
import stat
import time
import tomllib

import pytest
from fastapi.testclient import TestClient

from captain_search import changes, cloud, cloud_signin as SI, server

# What Modal's own tool does, as far as Captain reads it: print the link and the code, wait for the browser, then
# write the token under the profile it was given. It marks that profile active only when asked to, or when the
# file held no profile (Modal's own rule). `STANDIN` says how this run ends.
MODAL = r'''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
open(os.environ["STANDIN_SEEN"], "a").write(" ".join(args) + "|BROWSER=" + os.environ.get("BROWSER", "") + "\n")
mode = os.environ.get("STANDIN", "ok")
if mode == "nolink":
    print("Something went wrong before any link."); sys.exit(1)
if mode == "silent":
    time.sleep(30); sys.exit(0)
print("Was not able to launch web browser")
print("Please go to this URL manually and complete the flow:\n")
print("\x1b[4mhttps://modal.example/token-flow/tf-demo123\x1b[0m\n")
print("Enter this code: \x1b[33mABCD-1234\x1b[0m\n", flush=True)
go = os.environ["STANDIN_GO"]
for _ in range(600):
    if os.path.exists(go): break
    time.sleep(0.02)
else:
    sys.exit(3)
if mode == "fail":
    print("AuthError: Traceback (most recent call last): token flow was rejected"); sys.exit(1)
profile = args[args.index("--profile") + 1]
path = os.environ["MODAL_CONFIG_PATH"]
old = open(path).read() if os.path.exists(path) else ""
activate = "--no-activate" not in args or not old.strip()
open(path, "a").write(f'\n[{profile}]\ntoken_id = "ak-new"\ntoken_secret = "as-new"\n' + ("active = true\n" if activate else ""))
print("Web authentication finished successfully!")
print("Token is connected to the acme workspace.")
print(f"Token written to {path} in profile {profile}.")
'''

EXISTING = '[other]\ntoken_id = "ak-old"\ntoken_secret = "as-old"\nactive = true\n'


@pytest.fixture
def standin(tmp_path, monkeypatch):
    """A folder holding the stand-in tools, and a made-up Modal settings file that already holds a sign-in."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    tool = bin_ / "modal"
    tool.write_text(MODAL)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    cfg = tmp_path / "modal.toml"
    cfg.write_text(EXISTING)
    monkeypatch.setattr(SI, "TOOL_DIRS", [str(bin_)])
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STANDIN_GO", str(tmp_path / "go"))
    monkeypatch.setenv("STANDIN_SEEN", str(tmp_path / "seen"))
    monkeypatch.setenv("STANDIN", "ok")
    told: list = []
    monkeypatch.setattr(changes, "bump", lambda *t: told.append(t) or 1)
    monkeypatch.setattr(SI, "_JOBS", {})

    class W:
        pass

    w = W()
    w.bin, w.cfg, w.told, w.tmp = bin_, cfg, told, tmp_path
    w.finish = lambda: (tmp_path / "go").write_text("")
    w.seen = lambda: (tmp_path / "seen").read_text() if (tmp_path / "seen").exists() else ""
    yield w
    for j in list(SI._JOBS.values()):
        SI.stop(j["id"])


def ended(job_id, within=10.0):
    got = SI.wait(job_id, within)
    assert got["state"] not in ("starting", "waiting"), got
    return got


def test_modal_gives_the_link_and_the_code_and_never_the_tools_words(standin):
    job = SI.start("modal")
    assert job["state"] == "waiting" and job["label"] == "Modal"
    assert job["link"] == "https://modal.example/token-flow/tf-demo123", "the link, with the colour codes gone"
    assert job["code"] == "ABCD-1234"
    assert job["text"] is None and set(job) == {"id", "provider", "label", "state", "link", "code", "text", "result", "install"}
    assert SI.get(job["id"])["state"] == "waiting"
    # How the real tool is run: a profile of Captain's own, not made the active one, and its own browser tab off.
    assert standin.seen().strip() == "token new --no-activate --profile captain|BROWSER=true"


def test_modal_success_keeps_the_new_sign_in_beside_the_old_one_and_tells_the_feed(standin):
    job = SI.start("modal")
    assert not standin.told, "nothing is told while it waits"
    standin.finish()
    got = ended(job["id"])
    assert got["state"] == "done" and got["text"] == "Signed in to Modal, workspace acme."
    assert got["result"] == {"profile": "captain", "workspace": "acme", "active": False}
    assert ("logins", f"signin:{job['id']}") in standin.told, "the screen hears it on the change feed"
    after = tomllib.loads(standin.cfg.read_text())
    assert standin.cfg.read_text().startswith(EXISTING), "what was there is byte for byte as it was"
    assert after["other"] == {"token_id": "ak-old", "token_secret": "as-old", "active": True}, "and still the active one"
    assert "active" not in after["captain"]
    assert SI.modal_profiles() == [{"name": "other", "active": True, "signed_in": True}, {"name": "captain", "active": False, "signed_in": True}]
    assert "ak-" not in str(got) and "as-" not in str(got), "no token in what is handed back"
    # Captain looks for the sign-in it kept, by its profile.
    assert cloud._test_modal("captain")["ok"] and not cloud._test_modal("captain-9")["ok"]


def test_a_second_modal_sign_in_never_reuses_a_profile_name(standin):
    standin.cfg.write_text(EXISTING + '\n[captain]\ntoken_id = "ak-mine"\ntoken_secret = "as-mine"\n')
    before = standin.cfg.read_text()
    job = SI.start("modal")
    standin.finish()
    got = ended(job["id"])
    assert got["result"]["profile"] == "captain-2"
    assert standin.cfg.read_text().startswith(before) and tomllib.loads(standin.cfg.read_text())["captain"]["token_id"] == "ak-mine"


def test_the_first_modal_sign_in_on_a_computer_becomes_the_one_in_use(standin):
    standin.cfg.unlink()
    job = SI.start("modal")
    standin.finish()
    got = ended(job["id"])
    assert got["result"] == {"profile": "captain", "workspace": "acme", "active": True}


def test_modal_failure_is_one_plain_sentence(standin, monkeypatch):
    monkeypatch.setenv("STANDIN", "fail")
    job = SI.start("modal")
    standin.finish()
    got = ended(job["id"])
    assert got["state"] == "failed" and got["text"] == "Modal did not finish the sign-in. Try again."
    assert "Traceback" not in str(got) and "AuthError" not in str(got)
    assert standin.cfg.read_text() == EXISTING
    assert ("logins", f"signin:{job['id']}") in standin.told


def test_a_tool_that_gives_no_link_fails_at_once(standin, monkeypatch):
    monkeypatch.setenv("STANDIN", "nolink")
    job = SI.start("modal")
    assert job["state"] == "failed" and job["link"] is None and job["text"] == "Modal did not finish the sign-in. Try again."
    monkeypatch.setenv("STANDIN", "silent")
    job = SI.start("modal", link_wait_s=0.3)
    assert job["state"] == "failed" and job["text"] == "Modal did not give a sign-in link. Try again."


def test_modal_times_out_with_one_sentence_and_the_tool_is_ended(standin):
    job = SI.start("modal", timeout_s=0.4)
    assert job["state"] == "waiting"
    got = ended(job["id"])
    assert got["state"] == "timed_out" and got["text"] == "The sign-in was not finished within 1 minute. Try again."
    proc = SI._JOBS[job["id"]]["_proc"]
    proc.wait(5)
    assert proc.poll() is not None, "the tool is not left running"
    assert standin.cfg.read_text() == EXISTING and ("logins", f"signin:{job['id']}") in standin.told


def test_stop_waiting_ends_the_tool_and_keeps_nothing(standin):
    job = SI.start("modal")
    got = SI.stop(job["id"])
    assert got["state"] == "stopped" and got["text"] is None
    proc = SI._JOBS[job["id"]]["_proc"]
    proc.wait(5)
    time.sleep(0.1)
    assert SI.get(job["id"])["state"] == "stopped", "the tool's own exit does not turn a stop into a failure"
    assert standin.cfg.read_text() == EXISTING
    assert SI.stop("nope") is None and SI.get("nope") is None


def test_starting_again_stops_the_sign_in_that_was_waiting(standin):
    first = SI.start("modal")
    second = SI.start("modal")
    assert SI.get(first["id"])["state"] == "stopped" and second["state"] == "waiting"


def test_a_missing_tool_says_what_captain_needs_and_the_line_that_installs_it(standin):
    os.remove(standin.bin / "modal")
    job = SI.start("modal")
    assert job["state"] == "missing_tool" and job["text"] == "Captain needs the Modal tool to sign in."
    assert job["install"] == "pip install modal" and job["link"] is None


def test_no_test_can_reach_a_real_tool(standin):
    assert SI._find("modal") == str(standin.bin / "modal")
    assert SI._find("python3") is None, "only the stand-ins' folder is looked in"


def test_an_unknown_cloud_is_refused():
    with pytest.raises(ValueError, match="no browser sign-in"):
        SI.start("nimbus")


def test_the_form_is_told_a_task_and_never_a_command(standin, monkeypatch):
    monkeypatch.setattr(cloud, "_test_modal", lambda profile=None: {"ok": profile == "captain"})
    out = cloud.signin_found("modal", "local")
    assert out == {"found": False, "text": "No Modal sign-in on this computer yet.", "why": "Sign in to Modal first.", "browser": True}
    assert cloud.signin_found("modal", "local", "captain")["found"] is True
    for words in (out["text"], out["why"], cloud.MODAL_SIGNED_OUT):
        assert "token new" not in words and "terminal" not in words
    modal = next(p for p in cloud.PROVIDERS if p["provider"] == "modal")
    assert any(f["name"] == "modal_profile" and f.get("hidden") for f in modal["auth"][0]["fields"])


def test_the_addresses_the_form_calls(standin, tmp_path, monkeypatch):
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.post("/api/cloud/signin/start", json={"provider": "modal"}, headers=hd)
    assert r.status_code == 200 and r.json()["state"] == "waiting" and r.json()["code"] == "ABCD-1234"
    jid = r.json()["id"]
    assert c.get(f"/api/cloud/signin/jobs/{jid}", headers=hd).json()["state"] == "waiting"
    standin.finish()
    ended(jid)
    got = c.get(f"/api/cloud/signin/jobs/{jid}", headers=hd).json()
    assert got["state"] == "done" and got["result"]["profile"] == "captain"
    assert c.get("/api/cloud/signin", params={"provider": "modal", "auth": "local", "profile": "captain"}, headers=hd).json()["found"] is True
    assert c.get("/api/cloud/signin/jobs/nope", headers=hd).status_code == 404
    assert c.post("/api/cloud/signin/jobs/nope/stop", headers=hd).status_code == 404
    os.remove(tmp_path / "go")   # the next one waits on the browser again
    r = c.post("/api/cloud/signin/start", json={"provider": "modal"}, headers=hd)
    assert c.post(f"/api/cloud/signin/jobs/{r.json()['id']}/stop", headers=hd).json()["state"] == "stopped"


def test_cst_cloud_login_prints_the_link_and_waits(standin):
    from typer.testing import CliRunner

    from captain_search.cli.main import app

    standin.finish()   # the stand-in's browser step is already done, so the command returns
    r = CliRunner().invoke(app, ["cloud", "login", "modal"])
    out = r.output
    assert r.exit_code == 0, out
    assert "https://modal.example/token-flow/tf-demo123" in out and "ABCD-1234" in out
    assert "Signed in to Modal, workspace acme." in out
    os.remove(standin.bin / "modal")
    r = CliRunner().invoke(app, ["cloud", "login", "modal"])
    assert r.exit_code == 2 and "Captain needs the Modal tool to sign in." in r.output and "pip install modal" in r.output
