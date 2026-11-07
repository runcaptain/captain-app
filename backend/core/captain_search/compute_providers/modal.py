"""Modal: spawn a function from runner/modal_app.py (deploy it once with `modal deploy runner/modal_app.py`).

Not tested on a real account (2026-11-06): Captain has no Modal token of its own for this. Modal keeps a call's
output in its dashboard and in `modal app logs`, and its SDK has no per-call log read, so `logs` says where to look.
"""
from __future__ import annotations

from . import ComputeProvider, Job, status


class ModalProvider(ComputeProvider):
    kind, provider, label = "modal", "modal", "Modal function"
    purposes = ["backtest"]
    live_tested = None
    can_logs = False
    fields = [
        {"name": "app", "label": "Modal app name", "required": True, "default": "captain-search-runner"},
        {"name": "function", "label": "Function", "required": True, "default": "run_cst"}]

    def plan(self, job: Job) -> list[dict]:
        c = self.config
        fn = c.get("function") or "run_cst"
        if fn == "run_backtest":  # targets made before run_cst existed
            if job.backtest is None:
                raise ValueError("this Modal target calls run_backtest, which runs backtests only. Set function=run_cst.")
            payload = {"workspace": job.env, "backtest": job.backtest}
        else:
            payload = {"workspace": job.env, "argv": job.argv}
        return [{"service": "modal", "call": "Function.from_name(app, function).spawn",
                 "args": {"app": c["app"], "function": fn, **payload}}]

    def submit(self, job: Job) -> dict:
        import modal  # type: ignore

        a = self.plan(job)[0]["args"]
        fn = modal.Function.from_name(a["app"], a["function"])
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
            if "Cancel" in name:
                return status("stopped", "the call was cancelled")
            if "Expired" in name:
                return status("succeeded", "the call finished more than a day ago, so Modal no longer keeps its result")
            return status("failed", f"{name}: {str(e)[:300]}")
        if isinstance(code, int) and code != 0:
            return status("failed", f"cst exited with code {code}", exit_code=code)
        return status("succeeded", "the call returned", exit_code=code if isinstance(code, int) else None)

    def logs(self, h: dict, since: str | None = None) -> dict:
        return {"lines": [], "next": since,
                "note": f"Modal keeps this call's output in its dashboard. From a terminal: modal app logs {h.get('app')}"}

    def cancel(self, h: dict) -> dict:
        self._call(h).cancel()
        return status("stopped", "cancel sent to Modal")

    def test(self) -> dict:
        try:
            import modal  # type: ignore
        except ImportError:
            return {"ok": False, "detail": "the modal package is not installed: pip install modal"}
        try:
            modal.Function.from_name(self.config["app"], self.config.get("function") or "run_cst").hydrate()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{str(e)[:300]}. Deploy it once: modal deploy runner/modal_app.py"}
        return {"ok": True, "detail": f"Modal function {self.config['app']}.{self.config.get('function') or 'run_cst'} is deployed"}
