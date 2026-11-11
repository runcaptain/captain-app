"""cst job (what ran on compute targets) and cst compute setup (make the cloud side of a target)."""
from __future__ import annotations

import time
from typing import Optional

import typer

from .. import jobs as J
from .main import S, app, ctx, emit, fail
from .more import _do, compute_app

job_app = typer.Typer(no_args_is_help=True, help="Jobs submitted to compute targets: state, logs, cancel")
setup_app = typer.Typer(no_args_is_help=True, help="Make the cloud resources a compute target needs")
app.add_typer(job_app, name="job")
compute_app.add_typer(setup_app, name="setup")


def _row(r: dict) -> str:
    dur = f"{r['run_seconds']:.0f}s" if r.get("run_seconds") is not None else ""
    return f"{r['id']:31} {r['state']:9} {r['target']:14} {r['label'][:30]:30} {dur:>6}  {r.get('reason') or ''}"


@job_app.command("list")
def job_list(refresh: bool = typer.Option(False, "--refresh", help="ask each unfinished job's provider first"),
             limit: int = typer.Option(50, "--limit")):
    rows = _do(J.listing, ctx(), refresh_active=refresh, limit=limit)
    emit(rows, "\n".join(_row(r) for r in rows) or "no jobs yet")


@job_app.command("show")
def job_show(job_id: str, wait: bool = typer.Option(False, "--wait", help="poll until the job finishes"),
             every: float = typer.Option(10.0, "--every")):
    c = ctx()
    r = _do(J.show, c, job_id)
    while wait and r["state"] not in ("succeeded", "failed", "stopped"):
        if not S.json:
            typer.echo(f"{time.strftime('%H:%M:%S')} {r['state']}: {r.get('reason')}")
        time.sleep(every)
        r = _do(J.show, c, job_id)
    emit(r, "\n".join(f"{k:13} {v}" for k, v in r.items() if k != "handle") + f"\n{'handle':13} {r.get('handle')}")


@job_app.command("logs")
def job_logs(job_id: str, since: Optional[str] = typer.Option(None, "--since", help="cursor from a previous call's `next`"),
             follow: bool = typer.Option(False, "--follow", "-f")):
    c = ctx()
    while True:
        r = _do(J.logs, c, job_id, since)
        if S.json:
            emit(r)
        else:
            for ln in r["lines"]:
                typer.echo(f"{ln['ts'] or '':25} {ln['message']}" if ln.get("ts") else ln["message"])
            if r.get("note"):
                typer.secho(r["note"], fg="yellow", err=True)
        since = r.get("next")
        if not follow or J.load(c, job_id)["state"] in ("succeeded", "failed", "stopped") and not r["lines"]:
            break
        J.show(c, job_id)
        time.sleep(5)


@job_app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def job_run(typer_ctx: typer.Context, target: str = typer.Argument(..., help="compute target name"),
            label: Optional[str] = typer.Option(None, "--label")):
    """Run any cst command once on a compute target: cst job run ecs -- backtest run my-test"""
    from .. import compute as C

    args = list(typer_ctx.args)
    if not args:
        fail("give the cst arguments after --, e.g. cst job run ecs -- backtest run my-test")
    c = ctx()
    t = _do(C.get, c, target)
    if S.dry_run:
        return emit({"compute": target, "plan": _do(C.plan_job, c, t, args, label or args[0])})
    r = _do(C.submit_job, c, t, args, label or "-".join(args[:3]))
    emit(r, f"submitted {r['id']} to {target} ({r['target_kind']}). Follow it: cst job show {r['id']} --wait")


@job_app.command("cancel")
def job_cancel(job_id: str):
    if S.dry_run:
        return emit({"would_cancel": job_id})
    r = _do(J.cancel, ctx(), job_id)
    emit(r, r.get("note") or f"{job_id}: {r['state']}, {r.get('reason')}")


@setup_app.command("aws-ecs")
def setup_aws_ecs(profile: str = typer.Option(..., "--profile", help="cloud profile (see `cst cloud`)"),
                  region: str = typer.Option(..., "--region"),
                  name: str = typer.Option("ecs", "--name", help="compute target to add to this workspace"),
                  storage: Optional[str] = typer.Option(None, "--storage", help="workspace bucket; default: this workspace's"),
                  cluster: str = typer.Option("captain-search", "--cluster"),
                  image: Optional[str] = typer.Option(None, "--image", help="default: <account>.dkr.ecr.<region>.amazonaws.com/captain-search-runner:<version>"),
                  cpu: str = typer.Option("1024", "--cpu", help="CPU units: 1024 is one vCPU"),
                  memory: str = typer.Option("2048", "--memory", help="MB"),
                  subnets: Optional[str] = typer.Option(None, "--subnets", help="comma separated; default: the default VPC's"),
                  security_group: Optional[str] = typer.Option(None, "--security-group"),
                  dry_run: bool = typer.Option(False, "--dry-run", help="show what would be made, change nothing")):
    """Create (or reuse) the ECR repository, log group, roles, Fargate cluster and task definition, then add the target."""
    from .. import compute as C
    from .. import compute_setup as CS

    dry = dry_run or S.dry_run
    c = None
    if storage is None:
        c = ctx()
        storage = c.workspace.storage_uri
    r = _do(CS.setup_aws_ecs, profile, region, storage, cluster=cluster, image=image, cpu=cpu, memory=memory,
            subnets=[s for s in (subnets or "").split(",") if s] or None,
            security_groups=[security_group] if security_group else None, dry_run=dry)
    if not dry:
        c = c or ctx()
        if c.workspace.storage_uri == storage:
            t = _do(C.add, c, name, "aws-ecs", profile, region, r["target_config"], replace=True)
            r["target"] = t["name"]
        else:
            r["target"] = None
            r["notes"].append(f"not added as a target: the open workspace is {c.workspace.storage_uri}, not {storage}")
    lines = [f"{'(dry run) ' if dry else ''}AWS account {r['account']}, {region}"]
    lines += [f"  {s['action']:40} {s['resource']}: {s['name']}" + (f"  ({s['detail']})" if s['detail'] else "") for s in r["steps"]]
    lines += [f"note: {n}" for n in r["notes"]]
    if r.get("target"):
        lines.append(f"added compute target {r['target']}. Run a backtest on it: cst backtest run <name> --compute {r['target']}")
    elif not dry and r.get("target") is None:
        pass
    emit(r, "\n".join(lines))
    if any(s["action"] == "missing" for s in r["steps"]) and not dry:
        fail("the runner image is missing from ECR; push it before the first job", code=3)


@setup_app.command("aws-serving")
def setup_aws_serving(profile: str = typer.Option(..., "--profile", help="cloud profile (see `cst cloud`)"),
                      region: str = typer.Option(..., "--region"),
                      name_prefix: str = typer.Option("captain", "--name-prefix", help="every resource is named <prefix>-serving..."),
                      instance_type: str = typer.Option("ml.m5.large", "--instance-type"),
                      image: str = typer.Option("auto", "--image", help="auto, docker (this computer's Docker), codebuild (built in your account) or public (not available yet)"),
                      bucket: Optional[str] = typer.Option(None, "--bucket", help="for a workspace on this computer: the bucket for the serving bundle and request log"),
                      remove: bool = typer.Option(False, "--remove", help="remove the serving setup instead"),
                      policy: bool = typer.Option(False, "--policy", help="print the policy your AWS login needs, for an admin"),
                      yes: bool = typer.Option(False, "--yes", "-y")):
    """Set up serving in your AWS account: the endpoint's role, the image repository, the image and the compute target."""
    import json as _json

    from ..staging import serving_setup as SS

    c = ctx()
    try:
        if remove:
            r = SS.teardown(c, progress=None if S.json else lambda t: typer.echo(t, err=True))
            return emit(r, r["text"])
        p = SS.plan(c, profile, region, name_prefix=name_prefix, instance_type=instance_type, source=image, bucket=bucket)
        if policy:
            return emit(p["login_policy"], _json.dumps(p["login_policy"], indent=2))
        if S.dry_run or not yes:
            lines = ["Would create:" if S.dry_run else "This will create:"] + [f"  {r['resource']:24} {r['name']:28} {r['why']}" for r in p["creates"]] + [p["cost"]["text"]]
            if S.dry_run:
                return emit({**p, "dry_run": True}, "\n".join(lines))
            typer.echo("\n".join(lines))
            if not typer.confirm("Create these?"):
                raise typer.Exit(1)
        r = SS.setup(c, profile, region, name_prefix=name_prefix, instance_type=instance_type, source=image, bucket=bucket,
                     confirm=True, progress=None if S.json else lambda t: typer.echo(t, err=True))
    except SS.MissingPermission as e:
        emit({"error": str(e), "policy": e.policy}, f"error: {e}\n{_json.dumps(e.policy, indent=2)}")
        raise typer.Exit(1)
    except (ValueError, NotFound) as e:
        fail(str(e))
    emit(r, r["text"])
