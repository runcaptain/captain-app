"""`cst weights program`: Captain starts an agent on a network, watches it, and gives the verdict itself.

A person says the goal in plain words. Captain then:
  1. opens a session as an agent (or uses the one it is given), and writes in the record that Captain started it;
  2. names the three sets the result is judged on, before the agent starts, so the agent cannot move them;
  3. starts the agent's own command line with no window (`claude -p ...` or `codex exec ...`), with the goal, the
     rules (playbook.md) and the session in its environment. The only `cst` it can reach is a small stand-in that
     runs `cst weights` reads, runs, edits and notes on that one session and refuses everything else;
  4. prints each thing the agent does as its event lands in the record;
  5. when the agent stops (or the time is up, or a person stops it), measures the result itself and decides:
     a result that moved "should not change", made the held-out set worse, or did not help the target is taken
     back whole. A result that is kept is saved as a new version.

Every edit goes through the session, so it is a patch in the record and shows on the Weights page as it happens.
The verdict is Captain's. What the agent says about its own work is printed and is not what decides.
"""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from importlib import resources

from . import session as S
from . import store as WS
from .net import WeightsError

AGENTS = ("claude", "codex")
# What the agent may run on its session. Opening, saving, closing, naming sets and moving files are Captain's.
ALLOWED = ("session", "report", "events", "layers", "read", "stats", "heatmap", "template", "forward", "activations", "gradients",
           "trace", "slice", "edit", "undo", "redo", "diff", "evaluate", "note")
TIME_LIMIT_S = 600
# Claude Code prints its answer once, at the end. Seen 2026-11-13 on a real run: it printed the answer 31 seconds in
# and its command line then stayed open until the time limit, 6.5 minutes later. So once it has printed, and nothing
# more is printed and no event lands for this long, the agent is done and Captain ends its command line.
QUIET_S = 60
_STOPS: dict[str, threading.Event] = {}     # session id -> ask the run in this process to stop


def playbook() -> str:
    return resources.files("captain_search").joinpath("weights", "playbook.md").read_text(encoding="utf-8")


def find_agent(agent: str | None) -> tuple[str, str]:
    """(name, path) of the agent's command line: the one asked for, else claude, else codex."""
    if agent and agent not in AGENTS:
        raise WeightsError(f"The agent is claude or codex. {agent!r} is neither.")
    for name in ([agent] if agent else AGENTS):
        path = shutil.which(name)
        if path:
            return name, path
    if agent:
        raise WeightsError(f"The {agent} command line was not found. Install it, check that `{agent} --version` works in a terminal, and try again.")
    raise WeightsError("No agent command line was found. Install Claude Code (`claude`) or Codex (`codex`), check that it runs "
                       "in a terminal, and try again.")


def agent_command(name: str, path: str, prompt: str, workdir: str, writable: list[str]) -> tuple[list[str], bool, str]:
    """The command that starts the agent with no window, whether its own settings hold it to `cst weights`
    commands, and a sentence saying how. The flags are the ones each tool's own --help lists."""
    if name == "claude":
        return ([path, "-p", prompt, "--tools", "Bash", "--allowedTools", "Bash(cst weights *)", "--permission-prompts", "none",
                 "--strict-mcp-config", "--no-session-persistence", "--output-format", "text"], True,
                "Claude Code was started with only its shell tool, allowed only commands that begin `cst weights`, with nobody to "
                "approve anything else and no other tool servers. The person's own Claude settings can allow more.")
    cmd = [path, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral", "-C", workdir]
    for d in writable:
        cmd += ["--add-dir", d]
    return (cmd + [prompt], False,
            "Codex has no setting that limits it to named commands, so it was started in its own sandbox, which may write only to "
            "its working folder and the workspace. It was not held to `cst weights` commands by its own settings.")


def _shim(folder: str) -> str:
    """Write the only `cst` the agent can reach: `cst weights <one of ALLOWED>` on its own session, nothing else."""
    path = os.path.join(folder, "cst")
    body = f'''#!{sys.executable}
import os, sys
ALLOWED = {ALLOWED!r}
args = [a for a in sys.argv[1:] if a != "--json"]
if len(args) < 2 or args[0] != "weights" or args[1] not in ALLOWED:
    did = " ".join(sys.argv[1:3]) or "nothing"
    sys.stderr.write("Refused. In this run only these work: cst weights " + ", ".join(ALLOWED) + ". You ran: cst " + did + "\\n")
    sys.exit(2)
sys.argv[0] = "cst"
from captain_search.cli.main import main
main()
'''
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def prompt_for(ctx, sid: str, goal: str, opts: dict) -> str:
    """The agent's instructions: the playbook, then this run's goal, limits and what it is judged on."""
    s = S.session(ctx, sid)
    m = WS.manifest(ctx, s["model"], s["version"])
    led = s["ledger"]
    sets = s["sets"]
    lines = [
        playbook().strip(), "", "## This run", "",
        f"Goal, in the owner's words: {goal}", "",
        f"The network: {s['base']}, sizes {'-'.join(str(n) for n in m['sizes'])}, {led['total']:,} numbers, {m.get('cost')} cost.",
        f"Inputs: {(m.get('inputs') or {}).get('meaning', 'not stated')}. Outputs: {(m.get('outputs') or {}).get('meaning', 'not stated')}.",
    ]
    if (m.get("data") or {}).get("synthetic"):
        lines.append(f"Its data is synthetic: {(m.get('data') or {}).get('note', '')}")
    lines += [
        "", f"Your session is {sid}. It is already open and every `cst weights` command acts on it. Do not open another.",
        f"You may make at most {opts['max_edits']} edit{'s' if opts['max_edits'] != 1 else ''}. A further edit is refused.",
        f"Forward passes allowed: {led['forward_allowed']}." + (" The network cannot be run: read it." if not led["forward_allowed"] else ""),
        f"Raw weights: {'you may read them' if led['raw_weights'] else 'you may not read them'}. No sandbox: there is nothing to run but `cst weights`.",
        ("This session is blind. An edit and `cst weights evaluate` will say that the scores are hidden. They are measured and kept, "
         "and shown after you finish. Work from the weights.") if not led["feedback"] else
        "Scores are shown after each edit. Seeing one means the result can no longer be called \"from the weights alone\".",
        "", "What the result is judged on, named by Captain before you started. You cannot change these:",
        "- target: " + (f"{sets['target']['of']} example(s), {sets['target']['says']}" if sets["target"]["defined"] else "not named"),
        "- should not change: " + (f"{sets['should_not_change']['of']} examples, {sets['should_not_change']['says']}"
                                   if sets["should_not_change"]["defined"] else "not named"),
        "- held out: " + (f"{sets['held_out']['of']} examples you will never see" if sets["held_out"]["defined"] else "none"),
    ]
    if opts.get("target_shown"):
        lines.append(f"The target, so you can see it: input {opts['target_shown']['input']}, wanted output {opts['target_shown']['output']}.")
    lines += ["", "When you stop, Captain measures the result itself. If the target did not improve, or \"should not change\" or the "
                  "held-out set got worse, every edit you made is taken back. Run nothing but `cst weights` commands."]
    return "\n".join(lines)


def _target_spec(opts: dict):
    if opts.get("target_example") is not None:
        return {"example": int(opts["target_example"])}
    if opts.get("target_input") is not None:
        if opts.get("target_output") is None:
            raise WeightsError("A target given as an input needs the output that is wanted for it.")
        want = opts["target_output"]
        want = want if isinstance(want, (list, tuple)) else [want]
        one = len(want) == 1 and float(want[0]).is_integer()
        return {"inputs": [list(opts["target_input"])], "targets": [int(want[0])] if one else [list(want)]}
    return None


def prepare(ctx, model: str | None, goal: str, *, session: str | None = None, agent: str | None = None, blind: bool = False,
            passes: int = 0, max_edits: int = 5, target_example=None, target_input=None, target_output=None,
            should_not_change: list[int] | None = None, time_limit_s: int = TIME_LIMIT_S, dry_run: bool = False) -> dict:
    """Everything decided before the agent starts: which agent, the session, the three sets, the prompt and the
    command. With `dry_run` nothing is opened or written: the plan is described from the model alone."""
    goal = " ".join(str(goal or "").split())
    if not goal:
        raise WeightsError("Say the goal in plain words: what should the network do differently?")
    if not goal.endswith((".", "!", "?")):
        goal += "."
    if int(max_edits) < 1 or int(passes) < 0 or int(time_limit_s) < 1:
        raise WeightsError("The edit cap and the time limit are at least 1, and forward passes cannot be negative.")
    name, path = find_agent(agent)
    opts = {"max_edits": int(max_edits), "target_example": target_example, "target_input": target_input, "target_output": target_output}
    target = _target_spec(opts)
    plan = {"goal": goal, "agent": name, "agent_path": path, "blind": bool(blind), "passes": int(passes), "max_edits": int(max_edits),
            "time_limit_s": int(time_limit_s), "allowed_commands": ["cst weights " + c for c in ALLOWED]}
    if dry_run:
        m = WS.manifest(ctx, *WS.split_ref(model)) if model else WS.manifest(ctx, S.find(ctx, session))
        cmd, restricted, how = agent_command(name, path, "<the prompt below>", "<a temporary folder>", ["<the workspace>"])
        plan.update(model=WS.ref_of(m["name"], m["version"]), session=session or "a new session", target=target or "not named",
                    should_not_change={"classes": should_not_change} if should_not_change else "every class in the model's evaluation pool",
                    command=cmd, restricted=restricted, restriction=how, playbook=playbook(), dry_run=True)
        return plan
    opened = session is None
    if opened:
        if not model:
            raise WeightsError("Name the model to work on.")
        sid = S.open_session(ctx, model, by="agent", name=f"{name}, started by Captain", forward_passes=passes, feedback=not blind)["id"]
    else:
        sid = session
        S.session(ctx, sid)
    # The three sets, named by Captain before the agent starts.
    keep = {"classes": [int(c) for c in should_not_change]} if should_not_change else None
    if keep is None:
        st = S._load(ctx, sid)
        if st.doc["sets"].get("should_not_change") is None and WS.sets(ctx, st.manifest, "evaluation") is not None:
            n_out = st.manifest["sizes"][-1]
            keep = {"classes": list(range(max(n_out, 2)))}
    if target is not None or keep is not None:
        S.set_sets(ctx, sid, target=target, should_not_change=keep, by="captain")
    if target is not None:
        st = S._load(ctx, sid)
        spec = st.doc["sets"]["target"]
        if len(spec.get("inputs", [])) == 1 and len(spec["inputs"][0]) <= 16:
            opts["target_shown"] = {"input": spec["inputs"][0], "output": spec["targets"][0]}
    plan.update(session=sid, opened=opened, prompt=prompt_for(ctx, sid, goal, opts))
    return plan


def _decide(sc: dict, keep_anyway: bool, held_tolerance: float) -> tuple[bool, str]:
    """Captain's verdict from the three sets. Returns (keep, the reason in one sentence)."""
    scores = sc["scores"]
    applied = [e for e in sc["edits"] if e["state"] == "applied"]
    if not applied:
        return False, ("The agent made no edit, so there is nothing to keep." if not sc["edits"] else
                       "The agent took back every edit it made, so there is nothing to keep.")
    t, k, h = scores.get("target"), scores.get("keep"), scores.get("test")
    harm = []
    if k and k["broke"] > 0:
        harm.append(f"{k['broke']} of {k['of']} answers that should not change were right and are now wrong")
    if h:
        allowed = max(1, int(round(held_tolerance * h["of"])))
        if h["right_before"] - h["right_after"] > allowed:
            harm.append(f"the held-out set went from {h['right_before']} to {h['right_after']} right of {h['of']}, which is worse by more than {allowed}")
    helped = None if not t else (t["right_after"] > t["right_before"] or t["after"] < t["before"])
    if t and not helped:
        harm.append(f"the target did not improve (cost {t['before']:.4g} to {t['after']:.4g})")
    if harm and not keep_anyway:
        return False, "Taken back whole, because " + ", and ".join(harm) + "."
    if harm:
        return True, "Kept because it was asked for, although " + ", and ".join(harm) + "."
    if t:
        return True, (f"Kept. The target improved (cost {t['before']:.4g} to {t['after']:.4g}, {t['right_before']} to {t['right_after']} "
                      f"right of {t['of']}), and the rest held.")
    return True, "Kept. No target was named, and nothing that should hold got worse."


def stop(sid: str) -> bool:
    """Ask a run started in this process to stop. True if there was one."""
    ev = _STOPS.get(sid)
    if ev:
        ev.set()
    return bool(ev)


def run(ctx, plan: dict, *, keep: bool | None = None, save: bool = True, held_tolerance: float = 0.01, say=None, stop_event=None,
        decide_on_stop=None) -> dict:
    """Start the agent the plan names, follow it, then judge the result. `say(text)` gets one line per thing that
    happens. `stop_event` (or `stop(session)`) ends the agent early. `decide_on_stop()` may answer "keep" or "undo"
    when a person stopped it. Returns the report."""
    say = say or (lambda text: None)
    sid, name = plan["session"], plan["agent"]
    stop_event = stop_event or threading.Event()
    _STOPS[sid] = stop_event
    work = tempfile.mkdtemp(prefix="cst-weights-run-")
    out_path, err_path = os.path.join(work, "agent-output.txt"), os.path.join(work, "agent-errors.txt")
    proc, exit_code, stopped, timed_out, lingered, transcript = None, None, False, False, False, ""
    quiet_s = float(plan.get("quiet_s") or QUIET_S)
    try:
        _shim(work)
        writable = [d for d in (getattr(ctx.store, "root", None), os.environ.get("CST_HOME")) if d]
        cmd, restricted, how = agent_command(name, plan["agent_path"], plan["prompt"], work, writable)
        S.program_begin(ctx, sid, {"agent": name, "goal": plan["goal"], "max_edits": plan["max_edits"], "restricted": restricted,
                                   "restriction": how, "time_limit_s": plan["time_limit_s"],
                                   "command": [c if c != plan["prompt"] else "<the prompt>" for c in cmd]})
        env = {**os.environ, "PATH": work + os.pathsep + os.environ.get("PATH", ""), "CST_WEIGHTS_SESSION": sid, "CST_WEIGHTS_BY": "agent",
               "CST_WORKSPACE": ctx.workspace.name, "CST_BIN": os.path.join(work, "cst"),
               "PYTHONPATH": os.pathsep.join([os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                              os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
        say(f"Started {name} on session {sid}. {how}")
        seen = S.events(ctx, sid)["last"]
        t0 = time.time()
        with open(out_path, "w") as out, open(err_path, "w") as err:
            proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            last, size = time.time(), 0
            while True:
                code = proc.poll()
                for ev in S.events(ctx, sid, seen)["events"]:
                    seen = ev["seq"]
                    last = time.time()
                    say(f"{ev['id']:>4}  {ev['sentence']}")
                if code is not None:
                    exit_code = code
                    break
                now_size = os.path.getsize(out_path)
                if now_size != size:
                    size, last = now_size, time.time()
                if stop_event.is_set():
                    stopped = True
                elif time.time() - t0 > plan["time_limit_s"]:
                    timed_out = True
                elif name == "claude" and size > 0 and time.time() - last > quiet_s:
                    lingered = True     # it has given its answer and gone quiet: see QUIET_S
                if stopped or timed_out or lingered:
                    _end(proc)
                    exit_code = proc.returncode
                    break
                time.sleep(0.3)
    except KeyboardInterrupt:
        stopped = True
        if proc is not None:
            _end(proc)
            exit_code = proc.returncode
    finally:
        _STOPS.pop(sid, None)
        for path in (out_path, err_path):
            try:
                with open(path, errors="replace") as f:
                    transcript = (transcript + "\n" + f.read()).strip()
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)
    if stopped:
        say("Stopped. The agent's command line was ended. Every edit it had made is in the record.")
    if timed_out:
        say(f"The time limit of {plan['time_limit_s']} seconds is up. The agent's command line was ended.")
    if lingered:
        say(f"The agent gave its answer and did nothing more for {quiet_s:.0f} seconds, so Captain ended its command line.")
    for ev in S.events(ctx, sid, seen)["events"]:
        say(f"{ev['id']:>4}  {ev['sentence']}")

    # The verdict is Captain's.
    sc = S.program_scores(ctx, sid)
    keep_anyway = bool(keep)
    if stopped and keep is None and decide_on_stop is not None:
        answer = decide_on_stop()
        if answer == "keep":
            keep_anyway = True
        elif answer == "undo":
            S.program_undo(ctx, sid)
            sc = S.program_scores(ctx, sid)
    kept, reason = _decide(sc, keep_anyway, held_tolerance)
    if stopped and not sc["edits"]:
        reason = "The run was stopped before the agent made an edit."
    taken_back = 0
    if not kept:
        taken_back = S.program_undo(ctx, sid)
    label_before = sc["ledger"]["label"]
    alone = sc["ledger"]["weights_alone"]
    S.program_end(ctx, sid, {"kept": kept, "reason": reason, "scores": sc["scores"], "stopped": stopped, "timed_out": timed_out,
                             "agent_exit": 0 if lingered else exit_code})
    saved = None
    if kept and save:
        saved = S.save(ctx, sid, note=f"{name}: {plan['goal']}"[:200], by="captain")["ref"]
    if plan.get("opened") and save:
        S.close(ctx, sid, by="captain")
    led = S.session(ctx, sid)["ledger"]
    report = {
        "goal": plan["goal"], "agent": name, "session": sid, "model": S.session(ctx, sid)["base"],
        "allowed": {"raw_weights": led["raw_weights"], "sandbox": led["sandbox"], "forward_passes": led["forward_allowed"],
                    "forward_used": led["forward_used"], "blind": not led["feedback"], "max_edits": plan["max_edits"],
                    "restricted": bool(S._load(ctx, sid).doc["program"].get("restricted")),
                    "restriction": S._load(ctx, sid).doc["program"].get("restriction")},
        "edits": [{**e, "sentence": sc["sentences"].get(e["id"], "")} for e in sc["edits"]],
        "notes": sc["notes"], "refused": sc["refused"], "changed": sc["changed"], "total": sc["total"], "share": sc["share"],
        "scores": sc["scores"], "label": label_before, "weights_alone": alone, "kept": kept, "reason": reason,
        "taken_back": taken_back, "saved": saved, "deployed": None, "stopped": stopped, "timed_out": timed_out, "agent_exit": 0 if lingered else exit_code,
        "ended_after_answer": lingered, "agent_said": transcript.strip()[-4000:],
    }
    return report


def _end(proc) -> None:
    """End the agent and anything it started."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def report_text(r: dict) -> str:
    """The report a person reads at the end."""
    a = r["allowed"]
    lines = [f"Goal: {r['goal']}", f"Agent: {r['agent']}, started by Captain on {r['model']} (session {r['session']})",
             f"Allowed: raw weights {'yes' if a['raw_weights'] else 'no'}, a sandbox {'yes' if a['sandbox'] else 'no'}, "
             f"{a['forward_used']} of {a['forward_passes']} forward passes used, {a['max_edits']} edits at most, scores "
             f"{'hidden from the agent' if a['blind'] else 'shown to the agent'}",
             f"Held to cst weights commands: {'yes' if a['restricted'] else 'no'}. {a['restriction']}"]
    if r["notes"]:
        lines += ["What the agent wrote down:"] + [f"  {n}" for n in r["notes"]]
    lines.append("Edits:" if r["edits"] else "Edits: none")
    for e in r["edits"]:
        lines.append(f"  {e['id']}  {e['sentence']}" + (f" Why: {e['why']}" if e["why"] else "") + (" (taken back by the agent)" if e["state"] != "applied" else ""))
    for x in r["refused"]:
        lines.append(f"  {x}")
    lines.append(f"Changed: {r['changed']:,} of {r['total']:,} numbers ({r['share'] * 100:.2f}%), before Captain's verdict")
    for key, name in (("target", "target"), ("keep", "should not change"), ("test", "held out")):
        c = r["scores"].get(key)
        lines.append(f"  {name}: " + (f"cost {c['before']:.4g} to {c['after']:.4g}, right {c['right_before']} to {c['right_after']} of {c['of']}, "
                                      f"{c['answers_changed']} answers changed, {c['broke']} that were right are now wrong" if c else "not named"))
    lines += [f"Label: {r['label']}", f"Verdict: {r['reason']}",
              "Saved as: " + (r["saved"] or "nothing"), "Deployed: " + (r["deployed"] or "no")]
    if r["stopped"] or r["timed_out"]:
        lines.append("The agent was stopped by a person." if r["stopped"] else "The agent ran out of time.")
    elif r.get("ended_after_answer"):
        lines.append("The agent gave its answer and its command line stayed open, so Captain ended it.")
    elif r["agent_exit"]:
        lines.append(f"The agent's command line ended with code {r['agent_exit']}.")
    if r["agent_said"]:
        lines += ["What the agent said at the end:", r["agent_said"]]
    return "\n".join(lines)
