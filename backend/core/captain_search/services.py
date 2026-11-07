"""Every outside service a workspace uses, in one list, for Settings > Connected services.

Storage, database connections, compute targets (with which one runs backtests and serving by default), model
registries, MLflow and model-provider keys. Names, kinds and where each is used, never a secret. `check=True`
asks each one whether it answers right now; otherwise nothing is called.
"""
from __future__ import annotations


def listing(ctx, check: bool = False) -> dict:
    from . import compute as C
    from . import settings as ST

    out = {"storage": {"uri": ctx.workspace.storage_uri, "workspace": ctx.workspace.name}}
    conns = []
    for n in ctx.connections():
        try:
            c = ctx.load_connection(n)
            item = {"name": n, "kind": c.kind, "writable": bool(getattr(c, "writable", False)),
                    "network": getattr(getattr(c, "network", None), "mode", None)}
        except Exception as e:  # noqa: BLE001
            item = {"name": n, "kind": None, "error": str(e)[:200]}
        if check and "error" not in item:
            try:
                from .connectors.sql import run_sql

                r = run_sql(c, "select 1 as ok", limit=1)
                item["status"] = {"ok": True, "detail": f"answered in {r.elapsed_ms} ms"}
            except Exception as e:  # noqa: BLE001
                item["status"] = {"ok": False, "detail": str(e).splitlines()[0][:200] if str(e) else type(e).__name__}
        conns.append(item)
    out["connections"] = conns
    defaults = ST.get(ctx).get("compute") or {}
    kinds = C.kind_map()
    comp = []
    for t in C.listing(ctx):
        k = kinds.get(t["kind"], {})
        item = {"name": t["name"], "kind": t["kind"], "label": k.get("label", t["kind"]), "region": t.get("region"),
                "purposes": t.get("purposes") or k.get("purposes") or ["backtest"],
                "live_tested": k.get("live_tested"),
                "default_for": [p for p in ("backtest", "serve") if defaults.get(p) == t["name"]]}
        if check:
            try:
                item["status"] = C.test(ctx, t["name"])
            except Exception as e:  # noqa: BLE001
                item["status"] = {"ok": False, "detail": str(e)[:200]}
        comp.append(item)
    out["compute"] = {"targets": comp, "defaults": defaults,
                      "kinds": [{"kind": k["kind"], "label": k["label"], "purposes": k["purposes"],
                                 "live_tested": k["live_tested"]} for k in kinds.values()]}
    try:
        from . import attach

        out["registries"] = attach.registries(ctx)
    except Exception:  # noqa: BLE001
        out["registries"] = []
    try:
        from . import mlflow_bridge as MF

        out["mlflow"] = {k: v for k, v in MF.status(ctx).items() if k != "links"}
    except Exception:  # noqa: BLE001
        out["mlflow"] = {"configured": False}
    out["model_keys"] = _model_keys()
    return out


def _model_keys() -> list[dict]:
    """Which model-provider keys this computer has, by name only."""
    import os

    from .workspace import get_secret

    out = []
    for name, label in (("ANTHROPIC_API_KEY", "Anthropic"), ("OPENAI_API_KEY", "OpenAI")):
        have = bool(os.environ.get(name))
        if not have:
            try:
                have = bool(get_secret(name))
            except Exception:  # noqa: BLE001
                have = False
        out.append({"name": name, "label": label, "set": have})
    return out
