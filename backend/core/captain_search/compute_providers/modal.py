"""Modal: spawn a function from runner/modal_app.py (deploy it once with `modal deploy runner/modal_app.py`).

Ran on a real Modal workspace on 2026-11-10 with client 1.6.3 (docs/compute-live-testing.md says what was run).
Modal does not say whether a call is queued or running, and gives no start time or exit code for a cancelled call.
"""
from __future__ import annotations

import datetime as dt

from . import ComputeProvider, Job, status


class ModalProvider(ComputeProvider):
    kind, provider, label = "modal", "modal", "Modal function"
    purposes = ["backtest"]
    # A team Modal workspace, in its own environment, CPU only: demo seed, an offline evaluation (with its live
    # stream), logs, cancel of a running retrain (nothing written), and a retrain with and without --wait.
    live_tested = "2026-11-10"
    fields = [
        {"name": "app", "label": "Modal app name", "required": True, "default": "captain-search-runner"},
        {"name": "function", "label": "Function", "required": True, "default": "run_cst"},
        {"name": "environment", "label": "Modal environment (blank for your login's default)", "required": False}]

    def _function(self, app: str, fn: str):
        import modal  # type: ignore

        return modal.Function.from_name(app, fn, environment_name=self.config.get("environment") or None)

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        fn = c.get("function") or "run_cst"
        if fn == "run_backtest":  # targets made before run_cst existed
            if job.backtest is None:
                raise ValueError("this Modal target calls run_backtest, which runs offline evaluations only. Set function=run_cst.")
            payload = {"workspace": job.env, "backtest": job.backtest}
        else:
            payload = {"workspace": job.env, "argv": job.argv}
        return [{"service": "modal", "call": "Function.from_name(app, function).spawn",
                 "args": {"app": c["app"], "function": fn,
                          **({"environment": c["environment"]} if c.get("environment") else {}), **payload}}]

    def submit(self, job: Job) -> dict:
        a = self.plan(job)[0]["args"]
        fn = self._function(a["app"], a["function"])
        call = fn.spawn(a["workspace"], a["backtest"] if "backtest" in a else a["argv"])
        return {"call_id": call.object_id, "app": a["app"]}

    def _call(self, h: dict):
        import modal  # type: ignore

        return modal.FunctionCall.from_id(h["call_id"])

    def status(self, h: dict) -> dict:
        try:
            code = self._call(h).get(timeout=0)
        except TimeoutError:
            return status("running", "the call has not returned yet (Modal does not say whether it is queued or running)")
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            # A cancelled call raises RemoteError("Function call was cancelled by user or a failure."), so the
            # class name alone does not say it (seen on a real account, 2026-11-10).
            if "Cancel" in name or "cancel" in str(e).lower():
                return status("stopped", "the call was cancelled. Modal gives a cancelled call no exit code.")
            if "Expired" in name:
                return status("succeeded", "the call finished more than a day ago, so Modal no longer keeps its result")
            return status("failed", f"{name}: {str(e)[:300]}")
        if isinstance(code, int) and code != 0:
            return status("failed", f"cst exited with code {code}", exit_code=code)
        return status("succeeded", "the call returned", exit_code=code if isinstance(code, int) else None)

    def logs(self, h: dict, since: str | None = None) -> dict:
        """The call's own lines (FunctionCall.logs). The cursor is the last line's time."""
        where = f"Modal call {h.get('call_id')} (also: modal app logs {h.get('app')})"
        reader = getattr(self._call(h), "logs", None)
        if reader is None:  # a Modal client from before per-call logs
            return {"lines": [], "next": since, "note": f"This Modal client cannot read one call's output. Upgrade it "
                    f"(pip install -U modal), or from a terminal: modal app logs {h.get('app')}"}
        after = dt.datetime.fromisoformat(since) if since else None
        lines, last = [], since
        for e in reader.fetch(since=after):
            ts = e.timestamp.astimezone(dt.timezone.utc) if e.timestamp.tzinfo else e.timestamp.replace(tzinfo=dt.timezone.utc)
            if after is not None and ts <= after:
                continue  # `since` includes the line the cursor points at
            last = ts.isoformat()
            lines.append({"ts": ts.isoformat(timespec="seconds"), "message": e.message.rstrip("\n")})
        return {"lines": lines, "next": last, "where": where}

    def cancel(self, h: dict) -> dict:
        self._call(h).cancel()
        # Report what Modal says, not what was asked for: a call that had already returned stays "succeeded", and
        # one that is still stopping stays "running" until Modal reports the cancel.
        s = self.status(h)
        if s["state"] == "running":
            s["reason"] = "cancel sent to Modal, which is stopping the call"
        elif s["state"] == "stopped":  # Modal gives no times, so the stop is stamped when it is first seen
            s["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return s

    def test(self) -> dict:
        try:
            import modal  # type: ignore
        except ImportError:
            return {"ok": False, "detail": "the modal package is not installed: pip install modal"}
        try:
            self._function(self.config["app"], self.config.get("function") or "run_cst").hydrate()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{str(e)[:300]}. Deploy it once: modal deploy runner/modal_app.py"}
        return {"ok": True, "detail": f"Modal function {self.config['app']}.{self.config.get('function') or 'run_cst'} is deployed"}
