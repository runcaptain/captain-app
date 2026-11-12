"""This computer as a host: nothing to set up, nothing to pay, and only this computer can reach it."""
from __future__ import annotations

from ...workspace import Context
from .base import Host


class LocalHost(Host):
    cloud, label, kind, target = "local", "This computer", "local", "local"
    storage = "The workspace itself."
    address = "http://127.0.0.1:<port>, with the endpoint key as a bearer token. Only this computer can reach it."

    def plan(self, ctx: Context, **_options) -> dict:
        return {"cloud": "local", "creates": [], "cost": {"setup_usd": 0.0, "text": "Nothing to set up, and nothing to pay."},
                "login_policy": None, "text": "This computer needs no setup."}

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **_options) -> dict:
        return {"cloud": "local", "status": "ready", "target": "local", "created": [], "text": "This computer needs no setup."}

    def status(self, ctx: Context) -> dict:
        return {"cloud": "local", "status": "ready", "target": "local", "created": [], "text": "This computer needs no setup."}

    def teardown(self, ctx: Context, progress=None, **_options) -> dict:
        return {"cloud": "local", "status": "ready", "created": [], "text": "There is nothing to remove on this computer."}

    def leftovers(self, ctx: Context) -> list[dict]:
        return []
