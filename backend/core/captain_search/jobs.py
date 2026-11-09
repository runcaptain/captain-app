"""Job records: every job Captain submits to a compute target, so the app and teammates can list them.

A record lives in the workspace at `jobs/<id>.yaml`. It holds the target, the provider's handle, the last state
Captain saw and when. Reading a job that is not finished asks its provider for the current state and saves it.
"""
from __future__ import annotations

import datetime as dt

import yaml
from ulid import ULID

from . import compute_providers as P
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _key(job_id: str) -> str:
    return f"jobs/{job_id}.yaml"


def new_id() -> str:
    return "job-" + str(ULID()).lower()


def make_job(ctx: Context, args: list[str], label: str, backtest: str | None = None, job_id: str | None = None,
             target: str | None = None) -> P.Job:
    jid = job_id or new_id()
    env = {"CST_STORAGE": ctx.workspace.storage_uri, "CST_WORKSPACE": ctx.workspace.name, "CST_USER": whoami(),
           "CST_JOB_ID": jid, **({"CST_COMPUTE": target} if target else {})}
    return P.Job(id=jid, argv=["-w", ctx.workspace.name, *args], label=label, env=env, backtest=backtest)


def submit(ctx: Context, target: dict, job: P.Job, kind: str = "job") -> dict:
    """Start the job on the target and record it. Recorded before and after, so a crash mid-submit leaves a trace."""
    prov = P.get(target, ctx)
    rec = {"id": job.id, "kind": kind, "label": job.label, "argv": job.argv, "target": target["name"],
           "target_kind": target["kind"], "region": target.get("region"), "state": "queued", "reason": "submitting",
           "handle": None, "submitted_by": whoami(), "submitted_at": _now(), "updated_at": _now(),
           "started_at": None, "finished_at": None, "exit_code": None, "backtest": job.backtest}
    ctx.store.put_new(_key(job.id), dump_yaml(rec))
    try:
        handle = prov.submit(job)
    except Exception as e:
        _save(ctx, {**rec, "state": "failed", "reason": f"submit failed: {str(e)[:500]}", "finished_at": _now()})
        raise _wrap(e, target["kind"]) from e
    rec.update(handle=handle, reason=f"submitted to {target['kind']}", updated_at=_now())
    _save(ctx, rec)
    return rec


def _save(ctx: Context, rec: dict) -> dict:
    for _ in range(5):
        try:
            _, tag = ctx.store.get_with_tag(_key(rec["id"]))
        except NotFound:
            tag = None
        try:
            ctx.store.put_if(_key(rec["id"]), dump_yaml(rec), tag)
            return rec
        except Conflict:
            continue
    raise Conflict(f"job {rec['id']} kept changing while saving")


def load(ctx: Context, job_id: str) -> dict:
    try:
        return yaml.safe_load(ctx.store.get(_key(job_id)))
    except NotFound:
        raise NotFound(f"no job {job_id!r}. `cst job list` shows the recorded ones.") from None


def _target(ctx: Context, rec: dict) -> dict:
    from . import compute as C

    return C.get(ctx, rec["target"])


def refresh(ctx: Context, rec: dict) -> dict:
    """Ask the provider for the job's state if it is not finished, and save what changed."""
    if rec["state"] in P.TERMINAL or not rec.get("handle"):
        return rec
    try:
        s = P.get(_target(ctx, rec), ctx).status(rec["handle"])
    except Exception as e:  # noqa: BLE001
        return {**rec, "refresh_error": f"could not ask {rec['target_kind']} for the state: {str(e)[:300]}"}
    new = {**rec, **{k: s.get(k, rec.get(k)) for k in ("state", "reason", "started_at", "finished_at", "exit_code")}}
    if new["state"] in P.TERMINAL and not new.get("finished_at"):
        new["finished_at"] = _now()
    if any(new.get(k) != rec.get(k) for k in ("state", "reason", "started_at", "finished_at", "exit_code")):
        new["updated_at"] = _now()
        _save(ctx, new)
    return new


def _result_key(job_id: str) -> str:
    return f"jobs/{job_id}.result.yaml"


def save_result(ctx: Context, job_id: str, result: dict) -> None:
    """What a job produced, written once by the job itself as it finishes (a retrain's new version, say), so the
    caller that submitted it can read the outcome rather than guess it."""
    ctx.store.put_new(_result_key(job_id), dump_yaml(result))


def result(ctx: Context, job_id: str) -> dict | None:
    try:
        return yaml.safe_load(ctx.store.get(_result_key(job_id)))
    except NotFound:
        return None


def show(ctx: Context, job_id: str) -> dict:
    rec = _with_duration(refresh(ctx, load(ctx, job_id)))
    res = result(ctx, job_id)
    return {**rec, "result": res} if res is not None else rec


def listing(ctx: Context, refresh_active: bool = False, limit: int = 200) -> list[dict]:
    keys = sorted((k for k in ctx.store.list("jobs/") if k.endswith(".yaml") and not k.endswith(".result.yaml")),
                  reverse=True)[:limit]
    out = []
    for k in keys:
        rec = yaml.safe_load(ctx.store.get(k))
        out.append(_with_duration(refresh(ctx, rec) if refresh_active else rec))
    return out


def logs(ctx: Context, job_id: str, since: str | None = None) -> dict:
    rec = load(ctx, job_id)
    if not rec.get("handle"):
        return {"job": job_id, "lines": [], "next": since, "note": "the job never reached its provider"}
    try:
        r = P.get(_target(ctx, rec), ctx).logs(rec["handle"], since)
    except Exception as e:
        raise _wrap(e, rec["target_kind"]) from e
    return {"job": job_id, **r}


def cancel(ctx: Context, job_id: str) -> dict:
    rec = load(ctx, job_id)
    if rec["state"] in P.TERMINAL:
        return {**rec, "note": f"already {rec['state']}, nothing to cancel"}
    if not rec.get("handle"):
        raise ValueError("the job never reached its provider, so there is nothing to cancel")
    prov = P.get(_target(ctx, rec), ctx)
    if not prov.can_cancel:
        raise ValueError(f"{rec['target_kind']} jobs cannot be cancelled from Captain")
    try:
        s = prov.cancel(rec["handle"])
    except Exception as e:
        raise _wrap(e, rec["target_kind"]) from e
    new = {**rec, **{k: s.get(k, rec.get(k)) for k in ("state", "reason", "started_at", "finished_at", "exit_code")},
           "cancelled_by": whoami(), "cancelled_at": _now(), "updated_at": _now()}
    _save(ctx, new)
    return _with_duration(new)


def _wrap(e: Exception, kind: str) -> Exception:
    if isinstance(e, (ValueError, NotFound)):
        return e
    return P.ProviderError(f"{kind}: {type(e).__name__}: {str(e)[:500]}")


def _with_duration(rec: dict) -> dict:
    a, b = rec.get("started_at"), rec.get("finished_at")
    if a and b:
        try:
            rec = {**rec, "run_seconds": round((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds(), 1)}
        except ValueError:
            pass
    return rec
