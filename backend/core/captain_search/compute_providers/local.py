"""This machine: the job runs as a child process of whoever submitted it, with its output in a file."""
from __future__ import annotations

import datetime as dt
import os
import signal
import socket
import subprocess
import sys

from . import ComputeProvider, Job, status


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class LocalProvider(ComputeProvider):
    kind, provider, label = "local", "local", "This machine"
    purposes = ["backtest"]
    fields: list[dict] = []
    live_tested = "2026-11-06"

    def _dir(self) -> str:
        from ..workspace import home

        d = os.path.join(home(), "jobs")
        os.makedirs(d, exist_ok=True)
        return d

    def plan(self, job: Job) -> list[dict]:
        return [{"service": "local", "call": "cst", "args": {"argv": job.argv}}]

    def submit(self, job: Job) -> dict:
        base = os.path.join(self._dir(), job.id)
        log, exit_file = base + ".log", base + ".exit"
        py = [sys.executable, "-c", "import sys; from captain_search.cli.main import app; sys.argv[0] = 'cst'; app()"]
        # The shell records the exit code, so a later process (the app, another CLI call) can read it.
        script = '"$@"; echo $? > "$CST_EXIT_FILE"'
        with open(log, "ab") as fh:
            p = subprocess.Popen(["sh", "-c", script, "sh", *py, *job.argv], stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 env={**os.environ, **job.env, "CST_EXIT_FILE": exit_file, "PYTHONUNBUFFERED": "1"})
        return {"pid": p.pid, "host": socket.gethostname(), "log": log, "exit_file": exit_file, "started_at": _now()}

    def _here(self, h: dict) -> bool:
        return h.get("host") == socket.gethostname()

    def status(self, h: dict) -> dict:
        if not self._here(h):
            return status("running", f"this job runs on {h.get('host')}, so only that machine can see its state")
        if os.path.exists(h["exit_file"] + ".cancelled"):
            return status("stopped", "cancelled", finished_at=_mtime(h["exit_file"] + ".cancelled"))
        if os.path.exists(h["exit_file"]):
            code = int((open(h["exit_file"]).read().strip() or "1"))
            return status("succeeded" if code == 0 else "failed",
                          "exited cleanly" if code == 0 else f"cst exited with code {code}. The last log lines say why.",
                          exit_code=code, started_at=h.get("started_at"), finished_at=_mtime(h["exit_file"]))
        try:
            os.kill(h["pid"], 0)
            return status("running", "the process is running", started_at=h.get("started_at"))
        except ProcessLookupError:
            return status("failed", "the process ended without recording an exit code, so it was killed or crashed")
        except PermissionError:
            return status("running", "the process is running", started_at=h.get("started_at"))

    def logs(self, h: dict, since: str | None = None) -> dict:
        if not self._here(h):
            return {"lines": [], "next": since, "note": f"the log file is on {h.get('host')}"}
        try:
            with open(h["log"], "rb") as fh:
                fh.seek(int(since or 0))
                data = fh.read()
                end = fh.tell()
        except FileNotFoundError:
            return {"lines": [], "next": since}
        lines = [{"ts": None, "message": x} for x in data.decode(errors="replace").splitlines()]
        return {"lines": lines, "next": str(end)}

    def cancel(self, h: dict) -> dict:
        if not self._here(h):
            raise ValueError(f"this job runs on {h.get('host')}. Cancel it from that machine.")
        if os.path.exists(h["exit_file"]):
            return self.status(h)
        open(h["exit_file"] + ".cancelled", "w").close()
        try:
            os.killpg(h["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        return status("stopped", "cancelled")

    def test(self) -> dict:
        return {"ok": True, "detail": "runs on this machine"}


def _mtime(p: str) -> str:
    return dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc).isoformat(timespec="seconds")
