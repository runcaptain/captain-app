"""`cst weights program`: Captain starts an agent on a network, follows it, and gives the verdict itself.

Every test uses a fake agent: a small script named `claude` on PATH that runs the `cst` commands a plan file lists.
No model is called."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import changes as CH
from captain_search import server
from captain_search import workspace as wsp
from captain_search.cli.main import S as CLI_STATE
from captain_search.cli.main import app as cli
from captain_search.weights import net as N
from captain_search.weights import program as P
from captain_search.weights import session as S
from captain_search.weights import store as WS

R = CliRunner()

FAKE = r'''#!%(python)s
"""A stand-in for an agent's command line. It runs the cst commands its plan lists, and nothing thinks."""
import json, os, subprocess, sys, time
here = os.environ["FAKE_AGENT_DIR"]
json.dump({"argv": sys.argv[1:], "session": os.environ.get("CST_WEIGHTS_SESSION"), "cwd": os.getcwd()}, open(os.path.join(here, "started.json"), "w"))
results = []
for step in json.load(open(os.path.join(here, "plan.json"))):
    if step[0] == "sleep":
        time.sleep(float(step[1]))
        continue
    r = subprocess.run(step, capture_output=True, text=True)
    results.append({"cmd": step, "code": r.returncode, "out": (r.stdout + r.stderr)[-600:]})
    json.dump(results, open(os.path.join(here, "results.json"), "w"))
print("I am done. I made the edits the plan listed.")
'''


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace `cst` in another process can open, with a broken copy of the tiny network, and a fake `claude`."""
    CLI_STATE.json = False
    assert R.invoke(cli, ["init", "demo", "--storage", str(tmp_path / "bucket")]).exit_code == 0
    ctx = wsp.open_context("demo")
    # tiny@v2: the output's bias pushed far down, so it never says yes. Putting it back is the fix.
    sid = S.open_session(ctx, "tiny")["id"]
    good = float(S.read(ctx, sid, "layer2.bias")["values"][0])
    S.edit(ctx, sid, {"op": "set", "layer": 2, "neuron": 0, "from": None, "value": -60.0})
    S.save(ctx, sid, note="broken on purpose")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE % {"python": sys.executable})
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setenv("FAKE_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("PYTHONPATH", str(Path(server.__file__).parents[1]))

    class W:
        pass

    w = W()
    w.ctx, w.good, w.agent_dir, w.bin = ctx, good, agent_dir, bin_dir
    w.plan = lambda steps: (agent_dir / "plan.json").write_text(json.dumps(steps))
    w.results = lambda: json.loads((agent_dir / "results.json").read_text())
    w.started = lambda: json.loads((agent_dir / "started.json").read_text())
    return w


LONG = "600"    # how long a fake agent "hangs". Every test that uses it ends the agent, so it is never waited out.


def _until(cond, what: str, limit: float = 180.0):
    """Wait for something to be true in the record. The limit only stops a broken run from hanging the suite: no
    test passes or fails by how long a step took."""
    t0 = time.time()
    while True:
        got = cond()
        if got:
            return got
        if time.time() - t0 > limit:
            raise AssertionError(f"never happened within {limit:.0f} s: {what}")
        time.sleep(0.1)


def _job_done(c, hd, jid):
    return _until(lambda: (lambda j: j if j["status"] in ("done", "failed") else None)(c.get(f"/api/jobs/{jid}", headers=hd).json()),
                  "the job finished")


FIX = ["cst", "weights", "edit", "set", "--layer", "2", "--neuron", "1", "--value", "{good}", "--why", "the output bias keeps it off"]
GOAL = "The network never says yes. Make it say yes when exactly one input is high"


def _fix(w):
    return [a.replace("{good}", repr(w.good)) for a in FIX]


def _go(w, steps, **kw):
    w.plan(steps)
    said = []
    run_kw = {k: kw.pop(k) for k in ("keep", "stop_event", "save") if k in kw}
    plan = P.prepare(w.ctx, kw.pop("model", "tiny@v2"), GOAL, target_input=[0.9, 0.1], target_output=[1], **kw)
    return P.run(w.ctx, plan, say=said.append, **run_kw), said, plan


def test_a_good_result_is_kept_and_saved_and_the_record_says_captain_started_the_agent(ws):
    r, said, plan = _go(ws, [["cst", "weights", "layers"], ["cst", "weights", "note", "The output bias is far below the rest."], _fix(ws)])
    assert r["kept"] is True and r["saved"] == "tiny@v3" and r["taken_back"] == 0 and r["reason"].startswith("Kept. The target improved")
    assert r["scores"]["target"]["right_before"] == 0 and r["scores"]["target"]["right_after"] == 1 and r["scores"]["keep"]["broke"] == 0
    assert r["scores"]["test"]["right_after"] > r["scores"]["test"]["right_before"]
    assert [e["op"] for e in r["edits"]] == ["set"] and r["edits"][0]["why"] == "the output bias keeps it off" and r["changed"] == 1
    assert "(layer 2, neuron 1)" in r["edits"][0]["sentence"] and r["notes"] == ["The output bias is far below the rest."]
    _, t = WS.load(ws.ctx, "tiny", 3)
    assert float(t["layer2.bias"][0]) == pytest.approx(ws.good)

    # The session was opened as an agent, and the record says Captain started it and how it was held.
    s = S.report(ws.ctx, r["session"])
    assert s["who"] == {"kind": "agent", "name": "claude, started by Captain"} and s["closed"] is True
    kinds = [(e["kind"], e["by"]) for e in s["events"]]
    assert ("program", "captain") in kinds and ("edit", "agent") in kinds and ("note", "agent") in kinds and ("save", "captain") in kinds
    start = next(e for e in s["events"] if e["kind"] == "program" and e["op"] == "start")
    assert start["program"]["agent"] == "claude" and start["program"]["restricted"] is True and "held to cst weights commands" in start["sentence"]
    assert next(e for e in s["events"] if e["kind"] == "note")["sentence"] == "The agent thinks: The output bias is far below the rest."
    assert next(e for e in s["events"] if e["kind"] == "program" and e["op"] == "end")["sentence"].startswith("Captain's verdict: Kept.")
    # What was printed as it went: one line per thing the agent did.
    assert any("The agent set 1 number in layer2.bias" in line for line in said) and any("The agent thinks:" in line for line in said)
    # The agent got the goal, the playbook and its session, and was started with its own allowed-commands setting.
    started = ws.started()
    argv = started["argv"]
    assert argv[0] == "-p" and GOAL in argv[1] and "Read before you change anything" in argv[1] and r["session"] in argv[1]
    assert argv[argv.index("--allowedTools") + 1] == "Bash(cst weights *)" and argv[argv.index("--tools") + 1] == "Bash"
    assert argv[argv.index("--permission-prompts") + 1] == "none" and started["session"] == r["session"]
    text = P.report_text(r)
    for words in ("Goal: ", "Agent: claude, started by Captain", "Held to cst weights commands: yes", "Edits:", "Changed: 1 of 13 numbers",
                  "target: cost", "should not change: cost", "held out: cost", "Label: ", "Verdict: Kept.", "Saved as: tiny@v3"):
        assert words in text, words


def test_only_weights_commands_on_its_own_session_were_possible(ws):
    other = S.open_session(ws.ctx, "tiny")["id"]
    r, _, _ = _go(ws, [
        ["cst", "projects", "list"], ["cst", "weights", "open", "tiny"], ["cst", "weights", "save"], ["cst", "weights", "close"],
        ["cst", "weights", "sets", "--target", "1"], ["cst", "weights", "export", "/tmp/x"], ["cst", "weights", "program", "tiny", "--goal", "x"],
        ["cst", "weights", "layers", "--session", other], ["cst", "--json", "weights", "session"], _fix(ws),
        ["cst", "weights", "edit", "scale", "--layer", "1", "--neuron", "1", "--factor", "1.01", "--why", "one too many"],
    ], max_edits=1)
    res = ws.results()
    for i in range(7):
        assert res[i]["code"] == 2 and res[i]["out"].startswith("Refused. In this run only these work: cst weights session, report"), res[i]
    assert res[7]["code"] == 1 and "works on session" in res[7]["out"] and "and no other" in res[7]["out"]
    assert res[8]["code"] == 0 and '"closed": false' in res[8]["out"], "--json is allowed before the command"
    assert res[9]["code"] == 0 and res[10]["code"] == 3 and "may make 1 edit and has made them" in res[10]["out"]
    assert len(r["edits"]) == 1 and any("may make 1 edit" in x for x in r["refused"])
    assert len(S.session(ws.ctx, other)["events"]) == 1, "the other session was not touched"
    assert WS.versions(ws.ctx, "tiny") == [1, 2, 3], "only Captain saved"


def test_a_harmful_result_is_taken_back_whole_and_nothing_is_saved(ws):
    harm = ["cst", "weights", "edit", "set", "--layer", "2", "--neuron", "1", "--value", "60", "--why", "push it on"]
    r, _, _ = _go(ws, [harm, ["cst", "weights", "edit", "zero", "--layer", "1", "--neuron", "2", "--why", "and this"]])
    assert r["kept"] is False and r["saved"] is None and r["taken_back"] == 2 and len(r["edits"]) == 2
    assert r["reason"].startswith("Taken back whole, because") and "were right and are now wrong" in r["reason"]
    assert r["scores"]["target"]["right_after"] == 1, "it did fix the target, and still it is not kept"
    assert WS.versions(ws.ctx, "tiny") == [1, 2]
    rep = S.report(ws.ctx, r["session"])
    assert rep["ledger"]["changed"] == 0 and [e["state"] for e in rep["edits"]] == ["undone", "undone"]
    assert [e["by"] for e in rep["events"] if e["kind"] == "undo"] == ["captain", "captain"]
    # Asked to keep it whatever the scores say, Captain keeps it and says what it cost.
    r2, _, _ = _go(ws, [harm], keep=True)
    assert r2["kept"] is True and r2["saved"] == "tiny@v3" and r2["reason"].startswith("Kept because it was asked for, although")


def test_an_edit_that_does_not_help_the_target_is_taken_back_and_no_edit_is_said_plainly(ws):
    r, _, _ = _go(ws, [["cst", "weights", "edit", "add", "--layer", "1", "--neuron", "1", "--value", "0.001", "--why", "a guess"]])
    assert r["kept"] is False and "the target did not improve" in r["reason"]
    r, _, _ = _go(ws, [["cst", "weights", "layers"]])
    assert r["kept"] is False and r["reason"] == "The agent made no edit, so there is nothing to keep." and r["saved"] is None
    r, _, _ = _go(ws, [_fix(ws), ["cst", "weights", "undo"]])
    assert r["reason"] == "The agent took back every edit it made, so there is nothing to keep."


def test_a_blind_run_keeps_from_the_weights_alone_and_the_agent_saw_no_score(ws):
    r, said, _ = _go(ws, [["cst", "weights", "read", "--layer", "2", "--bias"], _fix(ws), ["cst", "--json", "weights", "evaluate"]], blind=True)
    res = ws.results()
    assert "Cost" not in res[1]["out"] and "blind" in res[1]["out"] and json.loads(res[2]["out"])["scores"] == "hidden"
    assert not any("Cost on" in line for line in said), "nothing printed while it ran showed a score"
    assert r["kept"] is True and r["label"] == "from the weights alone" and r["weights_alone"] is True and r["allowed"]["blind"] is True
    assert WS.manifest(ws.ctx, "tiny", 3)["record"]["ledger"]["weights_alone"] is True
    assert "Label: from the weights alone" in P.report_text(r)
    # A run that was shown its scores, or allowed to run the network, does not get the label.
    r, _, _ = _go(ws, [_fix(ws)])
    assert r["weights_alone"] is False and r["label"] == "from the weights, with scores shown along the way"
    r, _, _ = _go(ws, [["cst", "weights", "forward", "-i", "0.9,0.1"], _fix(ws)], blind=True, passes=2)
    assert r["weights_alone"] is False and r["label"] == "with 1 of 2 forward passes" and r["allowed"]["forward_used"] == 1


def _stop_after_first_edit(ws, **run_kw):
    stop, box = threading.Event(), {}
    ws.plan([_fix(ws), ["sleep", LONG], ["cst", "weights", "edit", "zero", "--layer", "1", "--neuron", "1", "--why", "never reached"]])
    plan = P.prepare(ws.ctx, "tiny@v2", GOAL, target_input=[0.9, 0.1], target_output=[1])
    t = threading.Thread(target=lambda: box.update(r=P.run(ws.ctx, plan, stop_event=stop, **run_kw)))
    t.start()
    _until(lambda: S.session(ws.ctx, plan["session"])["edits"], "the agent's first edit is in the record")
    stop.set()
    t.join(240)
    assert not t.is_alive() and "r" in box, "the run ended once it was asked to stop, long before the agent's own sleep"
    return box["r"]


def test_a_stopped_run_takes_its_edits_back_and_saves_nothing_even_when_the_edit_was_good(ws):
    r = _stop_after_first_edit(ws)
    assert r["stopped"] is True and r["kept"] is False and r["saved"] is None and r["taken_back"] == 1
    assert r["reason"] == "Stopped. Nothing was kept." and "Verdict: Stopped. Nothing was kept." in P.report_text(r)
    assert r["scores"]["target"]["right_after"] == 1, "the edit it had made was a good one, and it is still not kept"
    rep = S.report(ws.ctx, r["session"])
    assert [e["state"] for e in rep["edits"]] == ["undone"] and rep["ledger"]["changed"] == 0 and rep["closed"] is True
    assert [e["by"] for e in rep["events"] if e["kind"] == "undo"] == ["captain"], "the edit stays in the record, taken back"
    doc = S._load(ws.ctx, r["session"]).doc["program"]
    assert doc["active"] is False and doc["verdict"]["stopped"] is True and doc["verdict"]["kept"] is False
    assert WS.versions(ws.ctx, "tiny") == [1, 2]
    # Only --keep keeps what a stopped run did.
    r = _stop_after_first_edit(ws, keep=True)
    assert r["stopped"] is True and r["kept"] is True and r["saved"] == "tiny@v3"


def test_ctrl_c_on_the_command_stops_the_agent_takes_its_edit_back_and_exits(ws, tmp_path):
    ws.plan([_fix(ws), ["sleep", LONG]])
    p = subprocess.Popen([sys.executable, "-c", "from captain_search.cli.main import main; main()", "weights", "program", "tiny@v2", "--goal", GOAL,
                          "--target-input", "0.9,0.1", "--target-output", "1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=dict(os.environ))
    def edited():
        # The interrupt is sent only once the agent's edit is in the record, so the command is in its watching loop.
        mine = [x for x in S.sessions(ws.ctx, "tiny") if x["who"]["name"] == "claude, started by Captain"]
        return mine and S.session(ws.ctx, mine[0]["id"])["edits"]

    try:
        _until(edited, "the agent's edit is in the record")
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=240)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out
    assert "Stopped. The agent's command line was ended." in out and "Verdict: Stopped. Nothing was kept." in out and "Saved as: nothing" in out
    sid = next(s for s in S.sessions(ws.ctx, "tiny") if s["who"]["name"] == "claude, started by Captain")["id"]
    # The command has exited, so the take-back and the close are already written: read, do not wait.
    rep = S.report(ws.ctx, sid)
    assert [e["kind"] for e in rep["events"]][-3:] == ["undo", "program", "close"], [e["kind"] for e in rep["events"]]
    assert rep["closed"] is True and S._load(ws.ctx, sid).doc["program"]["active"] is False
    assert rep["saved"] == [] and rep["ledger"]["changed"] == 0 and [e["state"] for e in rep["edits"]] == ["undone"]
    assert WS.versions(ws.ctx, "tiny") == [1, 2]
    _until(lambda: subprocess.run(["pgrep", "-f", str(ws.bin / "claude")], capture_output=True, text=True).stdout.strip() == "",
           "the agent's process is gone", 30)


def test_the_time_limit_stops_an_agent_that_does_not_finish(ws):
    r, said, _ = _go(ws, [_fix(ws), ["sleep", LONG]], time_limit_s=6)
    assert r["timed_out"] is True and r["kept"] is False and any("The time limit of 6 seconds is up." in line for line in said)
    # On a busy machine the fake agent may not have made its edit inside 6 seconds. Either way nothing is left applied.
    assert r["reason"] == "Stopped at the time limit. Nothing was kept." and r["taken_back"] == len(r["edits"]) and r["saved"] is None
    assert S.report(ws.ctx, r["session"])["ledger"]["changed"] == 0
    assert WS.versions(ws.ctx, "tiny") == [1, 2]
    assert "The agent ran out of time." in P.report_text(r)


def test_an_agent_that_says_it_is_done_and_does_not_exit_is_ended_and_judged_as_finished(ws):
    """Seen on two real runs: the agent finished in about half a minute, and its command line stayed open for six more.
    It writes its answer only as it exits, so the signal is its own last command, `cst weights done`."""
    ws.plan([_fix(ws), ["cst", "weights", "done", "I set the output bias back. One edit, not confirmed by a run."], ["sleep", LONG]])
    plan = P.prepare(ws.ctx, "tiny@v2", GOAL, target_input=[0.9, 0.1], target_output=[1])
    assert 'Your last command must be `cst weights done' in plan["prompt"] and "Stop, and say you are done" in plan["prompt"]
    plan["grace_s"] = 2
    said = []
    r = P.run(ws.ctx, plan, say=said.append)     # it returns at all only because Captain ended the agent: the agent sleeps for LONG
    assert r["ended_after_done"] is True and r["timed_out"] is False and r["stopped"] is False
    assert r["kept"] is True and r["saved"] == "tiny@v3" and r["agent_exit"] == 0, "a finished run, judged and kept"
    assert r["agent_said"] == "I set the output bias back. One edit, not confirmed by a run."
    assert any("The agent says it is done: I set the output bias back." in line for line in said)
    assert any("said it was done and its command line was still open 2 seconds later" in line for line in said)
    assert "The agent said it was done and its command line stayed open, so Captain ended it." in P.report_text(r)
    assert [e["kind"] for e in S.report(ws.ctx, r["session"])["events"]].count("done") == 1
    # An agent that never says it is done runs to the time limit, and that is a stopped run: nothing is kept.
    ws.plan([_fix(ws), ["sleep", LONG]])
    plan = P.prepare(ws.ctx, "tiny@v2", GOAL, target_input=[0.9, 0.1], target_output=[1], time_limit_s=6)
    plan["grace_s"] = 2
    r = P.run(ws.ctx, plan)
    assert r["ended_after_done"] is False and r["timed_out"] is True and r["kept"] is False and r["taken_back"] == len(r["edits"])
    # One that says it is done and exits by itself needs no ending.
    ws.plan([_fix(ws), ["cst", "weights", "done", "Done."]])
    plan = P.prepare(ws.ctx, "tiny@v2", GOAL, target_input=[0.9, 0.1], target_output=[1])
    r = P.run(ws.ctx, plan)
    assert r["ended_after_done"] is False and r["kept"] is True and r["agent_said"] == "Done."


def test_a_dry_run_prints_the_plan_and_the_instructions_and_starts_nothing(ws):
    before = len(S.sessions(ws.ctx, "tiny"))
    CLI_STATE.json = False
    r = R.invoke(cli, ["--dry-run", "weights", "program", "tiny@v2", "--goal", GOAL, "--target-input", "0.9,0.1", "--target-output", "1", "--blind"])
    CLI_STATE.dry_run = False
    assert r.exit_code == 0, r.output
    for words in ("Would start claude", "on tiny@v2", "Goal: " + GOAL, "scores hidden from the agent", "Nothing was started.",
                  "--allowedTools Bash(cst weights *)", "Read before you change anything", "cst weights note"):
        assert words in r.output, words
    assert len(S.sessions(ws.ctx, "tiny")) == before and not (ws.agent_dir / "started.json").exists()
    assert WS.versions(ws.ctx, "tiny") == [1, 2]


def test_a_missing_agent_command_line_is_one_plain_sentence_saying_what_to_install(ws, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(N.WeightsError) as e:
        P.prepare(ws.ctx, "tiny@v2", GOAL)
    assert str(e.value) == ("No agent command line was found. Install Claude Code (`claude`) or Codex (`codex`), check that it runs in a "
                            "terminal, and try again.")
    CLI_STATE.json = False
    r = R.invoke(cli, ["weights", "program", "tiny@v2", "--goal", GOAL, "--agent", "codex"])
    assert r.exit_code == 1 and "The codex command line was not found. Install it" in r.output and "Traceback" not in r.output
    assert len([s for s in S.sessions(ws.ctx, "tiny") if s["who"]["kind"] == "agent"]) == 0, "no session was opened for it"
    with pytest.raises(N.WeightsError, match="claude or codex"):
        P.find_agent("gemini")


def test_codex_is_started_in_its_sandbox_and_the_record_says_it_could_not_be_held_to_named_commands(ws):
    cmd, restricted, how = P.agent_command("codex", "/x/codex", "PROMPT", "/work", ["/store"])
    assert cmd[:4] == ["/x/codex", "exec", "--sandbox", "workspace-write"] and cmd[-1] == "PROMPT" and "--add-dir" in cmd
    assert restricted is False and "not held to `cst weights` commands by its own settings" in how


def test_the_command_prints_progress_and_the_report_and_json_gives_the_report_alone(ws):
    ws.plan([["cst", "weights", "note", "Trying the bias."], _fix(ws)])
    CLI_STATE.json = False
    r = R.invoke(cli, ["weights", "program", "tiny@v2", "--goal", GOAL, "--target-input", "0.9,0.1", "--target-output", "1", "--max-edits", "2"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert out.index("Started claude on session") < out.index("The agent thinks: Trying the bias.") < out.index("The agent set 1 number") < out.index("Goal: ")
    assert "Verdict: Kept." in out and "Saved as: tiny@v3" in out and "What the agent said at the end:" in out and "I am done." in out
    ws.plan([_fix(ws)])
    r = R.invoke(cli, ["--json", "weights", "program", "tiny@v2", "--goal", GOAL, "--target-input", "0.9,0.1", "--target-output", "1"])
    CLI_STATE.json = False
    d = json.loads(r.output)
    assert d["kept"] is True and d["saved"] == "tiny@v4" and set(d) >= {"goal", "agent", "allowed", "edits", "share", "scores", "label", "reason", "deployed"}


def test_the_page_starts_and_stops_a_run_as_a_job_on_the_feed(ws, monkeypatch):
    CH.reset_for_tests()
    monkeypatch.setattr(server, "ctx", lambda *a, **k: (CH.watch(ws.ctx.store), ws.ctx)[1])
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    sid = S.open_session(ws.ctx, "tiny@v2")["id"]       # the page's own session, a person's
    c.post(f"/api/weights/sessions/{sid}/sets", headers=hd, json={"target": {"inputs": [[0.9, 0.1]], "targets": [1]}})
    ws.plan([_fix(ws)])
    seq = c.get("/api/changes", headers=hd).json()["seq"]
    r = c.post(f"/api/weights/sessions/{sid}/program", headers=hd, json={"goal": GOAL})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["session"] == sid and job["agent"] == "claude"
    j = _job_done(c, hd, job["job"])
    assert j["status"] == "done", j
    assert any("The agent set 1 number" in line for line in j["log"])
    # From the page a good result is kept in the session and nothing is saved until the person saves it.
    assert j["result"]["kept"] is True and j["result"]["saved"] is None and j["result"]["kept_in_session"] is True
    assert WS.versions(ws.ctx, "tiny") == [1, 2] and S.session(ws.ctx, sid)["ledger"]["changed"] == 1 and S.session(ws.ctx, sid)["saved"] == []
    assert "Saved as: nothing yet. The edits are in the session, for a person to save" in P.report_text(j["result"])
    assert c.post(f"/api/weights/sessions/{sid}/save", headers=hd, json={}).json()["ref"] == "tiny@v3"
    told = {t["topic"] for t in c.get("/api/changes", headers=hd, params={"since": seq, "wait": 0, "topics": f"jobs,weights-session:{sid},job:{job['job']}"}).json()["changed"]}
    assert told == {"jobs", f"weights-session:{sid}", f"job:{job['job']}"}
    s = S.session(ws.ctx, sid)
    assert s["closed"] is False and [e["by"] for e in s["events"] if e["kind"] == "edit"] == ["agent"], "the person's session, with the agent's edit marked as the agent's"
    # Stop: a run that would go on is ended, and Captain's verdict follows.
    ws.plan([_fix(ws), ["sleep", LONG]])
    sid2 = S.open_session(ws.ctx, "tiny@v2")["id"]
    job = c.post(f"/api/weights/sessions/{sid2}/program", headers=hd, json={"goal": GOAL}).json()
    _until(lambda: S.session(ws.ctx, sid2)["edits"], "the agent's edit is in the record")
    assert c.post(f"/api/weights/sessions/{sid2}/program/stop", headers=hd).json() == {"session": sid2, "stopping": True}
    res = _job_done(c, hd, job["job"])["result"]
    assert res["stopped"] is True and res["kept"] is False and res["reason"] == "Stopped. Nothing was kept." and res["saved"] is None
    assert S.session(ws.ctx, sid2)["ledger"]["changed"] == 0, "back to how the session was before the run"
    # Asked to save, the route saves a good result at once.
    ws.plan([_fix(ws)])
    sid3 = S.open_session(ws.ctx, "tiny@v2")["id"]
    job3 = c.post(f"/api/weights/sessions/{sid3}/program", headers=hd, json={"goal": GOAL, "save": True, "target_input": [0.9, 0.1], "target_output": [1]}).json()
    assert _job_done(c, hd, job3["job"])["result"]["saved"] == "tiny@v4"
    assert c.post(f"/api/weights/sessions/{sid2}/program/stop", headers=hd).status_code == 400
    plan = c.post(f"/api/weights/sessions/{sid2}/program", headers=hd, json={"goal": GOAL, "dry_run": True}).json()
    assert plan["dry_run"] is True and plan["agent"] == "claude"
    CH.reset_for_tests()
