"""Signing in to a cloud from Captain, with the cloud's own browser sign-in.

A person presses "Sign in with your browser" in Connect a cloud account (or an agent runs `cst cloud login`).
Captain starts the cloud's own sign-in tool as a job, reads the link (and the short code, where the cloud uses one)
from what the tool prints, and hands them back. The screen opens the link. When the tool ends, the change feed is
told (`logins` and `signin:<id>`), so the screen hears it and nobody waits on a clock.

Two rules every cloud here follows:

- Nothing a tool prints reaches a person. A job ends with one of Captain's own sentences.
- A sign-in never replaces one that is already on this computer. Each cloud's entry below says where the new
  sign-in is kept and why what was there stays as it was.

Modal: `modal token new --no-activate --profile <name>`. The token goes into Modal's own settings file
(`~/.modal.toml`, or the file `MODAL_CONFIG_PATH` names) as a NEW profile whose name no profile there has
(`captain`, else `captain-2`, ...). `--no-activate` leaves the active profile as it is. Modal's one exception is its
own: when the file holds no profile at all, the new one becomes the active one, which is what a first sign-in wants.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid

# Where the tools are looked for. None: on PATH, then beside the Python that runs Captain's services (the Modal tool
# comes with them). A test puts its own folder here, so no test can ever start a real tool.
TOOL_DIRS: list[str] | None = None

LINK_WAIT_S = 20.0     # how long a tool gets to print its link before the sign-in is called failed
TIMEOUT_S = 600.0      # how long a person gets to finish in the browser

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_LINK = re.compile(r"https?://[^\s\"'<>\]]+")


# ---- Modal -------------------------------------------------------------------------------------------------------

def modal_config_path() -> str:
    return os.environ.get("MODAL_CONFIG_PATH") or os.path.expanduser("~/.modal.toml")


def modal_profiles() -> list[dict]:
    """The profiles in Modal's settings file: [{name, active, signed_in}]. Names and two yes/no answers only: a
    token never leaves this function."""
    import tomllib

    try:
        with open(modal_config_path(), "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return []
    return [{"name": n, "active": v.get("active") is True, "signed_in": bool(v.get("token_id") and v.get("token_secret"))}
            for n, v in data.items() if isinstance(v, dict)]


def _free_modal_profile() -> str:
    taken = {p["name"] for p in modal_profiles()}
    name, n = "captain", 1
    while name in taken:
        n += 1
        name = f"captain-{n}"
    return name


def _modal_plan(opts: dict) -> dict:
    profile = _free_modal_profile()
    return {"argv": ["token", "new", "--no-activate", "--profile", profile],
            # Captain's screen opens the link, so the tool is kept from opening a second tab: Python's own
            # `BROWSER` setting, pointed at a command that does nothing. Wide lines so the link is never folded.
            "env": {"BROWSER": "true", "COLUMNS": "500", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"},
            "keep": {"profile": profile}}


def _modal_done(job: dict, lines: list[str]) -> dict:
    text = "\n".join(lines)
    m = re.search(r"connected to the\s+(\S+)\s+workspace", text)
    profile = job["keep"]["profile"]
    active = any(p["name"] == profile and p["active"] for p in modal_profiles())
    return {"profile": profile, "workspace": m.group(1).strip(".") if m else None, "active": active}


def _modal_said(result: dict) -> str:
    return f"Signed in to Modal, workspace {result['workspace']}." if result.get("workspace") else "Signed in to Modal."


SPECS: dict[str, dict] = {
    "modal": {"label": "Modal", "tool": "modal", "install": "pip install modal", "plan": _modal_plan,
              "code": re.compile(r"Enter this code:\s*(\S+)"), "done": _modal_done, "said": _modal_said},
}


# ---- the job ---------------------------------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_SHOWN = ("id", "provider", "label", "state", "link", "code", "text", "result", "install")


def _find(tool: str) -> str | None:
    dirs = TOOL_DIRS
    if dirs is None:
        hit = shutil.which(tool)
        if hit:
            return hit
        dirs = [os.path.dirname(sys.executable)]
    for d in dirs:
        p = os.path.join(d, tool)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def view(job: dict) -> dict:
    """What a screen or an agent is given: never the tool's own words."""
    return {k: job.get(k) for k in _SHOWN}


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
    return view(job) if job else None


def _tell(job: dict) -> None:
    try:
        from . import changes

        changes.bump("logins", f"signin:{job['id']}")
    except Exception:  # noqa: BLE001
        pass


def _end(job: dict, state: str, text: str | None, result: dict | None = None) -> None:
    """The one place a job ends. The first ending stands: a stop is not overwritten by the tool's exit after it."""
    with _LOCK:
        if job["state"] not in ("starting", "waiting"):
            return
        job.update(state=state, text=text, result=result)
    t = job.pop("_timer", None)
    if t:
        t.cancel()
    job["_ended"].set()
    _tell(job)


def _kill(job: dict) -> None:
    p = job.get("_proc")
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            p.terminate()
        except OSError:
            pass


def _read(job: dict, spec: dict) -> None:
    p, lines = job["_proc"], []
    for raw in p.stdout:
        line = _ANSI.sub("", raw).rstrip()
        lines.append(line)
        del lines[:-200]
        if not job["link"]:
            m = _LINK.search(line)
            if m:
                job["link"] = m.group(0).rstrip(".,)")
        if spec.get("code") is not None and not job["code"]:
            m = spec["code"].search(line)
            if m:
                job["code"] = m.group(1)
        if job["link"] and (spec.get("code") is None or job["code"]):
            job["_linked"].set()
    code = p.wait()
    job["_linked"].set()
    label = spec["label"]
    if code == 0 and job["link"]:
        try:
            result = spec["done"](job, lines)
        except Exception:  # noqa: BLE001
            result = {}
        _end(job, "done", spec["said"](result), result)
    else:
        _end(job, "failed", f"{label} did not finish the sign-in. Try again.")


def start(provider: str, opts: dict | None = None, timeout_s: float | None = None, link_wait_s: float | None = None) -> dict:
    """Start this cloud's browser sign-in and return once its link is known: {id, state, link, code, text, ...}.
    `state` is waiting (the link is ready: open it), missing_tool (with `install`, the line that installs it), or
    failed. A sign-in already under way for the same cloud is stopped first."""
    spec = SPECS.get(provider)
    if spec is None:
        raise ValueError(f"Captain has no browser sign-in for {provider!r}: use {', '.join(SPECS)}")
    label = spec["label"]
    job = {"id": uuid.uuid4().hex[:12], "provider": provider, "label": label, "state": "starting", "link": None,
           "code": None, "text": None, "result": None, "install": None,
           "_ended": threading.Event(), "_linked": threading.Event()}
    with _LOCK:
        older = [j for j in _JOBS.values() if j["provider"] == provider and j["state"] in ("starting", "waiting")]
        _JOBS[job["id"]] = job
        for done in [k for k, j in _JOBS.items() if j["state"] not in ("starting", "waiting")][:-20]:
            del _JOBS[done]
    for j in older:
        stop(j["id"])
    tool = _find(spec["tool"])
    if tool is None:
        job["install"] = spec["install"]
        _end(job, "missing_tool", f"Captain needs the {label} tool to sign in.")
        return view(job)
    plan = spec["plan"](opts or {})
    job["keep"] = plan.get("keep", {})
    try:
        job["_proc"] = subprocess.Popen([tool, *plan["argv"]], env={**os.environ, **plan.get("env", {})},
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace", start_new_session=True)
    except OSError:
        _end(job, "failed", f"Captain could not start the {label} tool. Try again.")
        return view(job)
    limit = TIMEOUT_S if timeout_s is None else timeout_s
    timer = threading.Timer(limit, _expire, args=(job, limit))
    timer.daemon = True
    job["_timer"] = timer
    timer.start()
    threading.Thread(target=_read, args=(job, spec), daemon=True, name=f"signin-{provider}").start()
    job["_linked"].wait(LINK_WAIT_S if link_wait_s is None else link_wait_s)
    with _LOCK:
        if job["state"] == "starting" and job["link"]:
            job["state"] = "waiting"
    if job["state"] == "starting":   # no link in time: nothing for a person to open
        _kill(job)
        _end(job, "failed", f"{label} did not give a sign-in link. Try again.")
    return view(job)


def _expire(job: dict, limit: float) -> None:
    _kill(job)
    minutes = max(1, round(limit / 60))
    _end(job, "timed_out", f"The sign-in was not finished within {minutes} minute{'' if minutes == 1 else 's'}. Try again.")


def stop(job_id: str) -> dict | None:
    """Stop waiting: the tool is ended and nothing is kept."""
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return None
    _kill(job)
    _end(job, "stopped", None)
    return view(job)


def wait(job_id: str, timeout_s: float | None = None) -> dict | None:
    """For `cst cloud login`: block until the job ends, then what it ended as."""
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return None
    job["_ended"].wait(timeout_s)
    return view(job)
