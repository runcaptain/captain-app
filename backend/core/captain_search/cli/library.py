"""cst data import / mkdir / move / folders, and cst registry attach / list / detach."""
from __future__ import annotations

from typing import List, Optional

import typer

from .. import attach as AT
from .. import library as LIB
from ..data import importer as IM
from .main import app, ctx, data_app, emit, fail

registry_app = typer.Typer(no_args_is_help=True, help="Model registries attached for lineage (read, never written)")
app.add_typer(registry_app, name="registry")


@data_app.command("import")
def data_import(paths: List[str] = typer.Argument(..., help="files or folders. Folders keep their structure"),
                folder: str = typer.Option("", "--folder", "-f", help="where in the workspace, like catalogs/2026")):
    """Bring local csv, tsv, parquet, json or jsonl files into the workspace. Same name again = a new version."""
    r = IM.import_paths(ctx(), paths, folder)
    lines = [f"added    {x['ref']:<40} {x['kind']:<10} {x['rows']:>10,} rows  in {x['folder'] or '/'}" for x in r["imported"]]
    lines += [f"updated  {x['ref']:<40} {x['kind']:<10} {x['rows']:>10,} rows" for x in r["updated"]]
    lines += [f"same     {x}" for x in r["unchanged"]]
    lines += [f"skipped  {x['path']}: {x['reason']}" for x in r["skipped"]]
    emit(r, "\n".join(lines) or "nothing to import")


@data_app.command("mkdir")
def data_mkdir(path: str):
    """Make a folder (and its parents) in the workspace."""
    emit({"folder": LIB.make_folder(ctx(), path)}, f"made {path}")


@data_app.command("move")
def data_move(names: List[str] = typer.Argument(...), to: str = typer.Option(..., "--to", help="folder, or '' for the top")):
    """Move datasets into a folder. Nothing is copied and versions don't change."""
    emit(LIB.move(ctx(), names, to), f"moved {len(names)} to {to or '/'}")


@data_app.command("rename-folder")
def data_rename_folder(old: str, new: str):
    emit(LIB.rename_folder(ctx(), old, new), f"{old} is now {new}")


@data_app.command("rmdir")
def data_rmdir(path: str):
    """Remove an empty folder. Datasets are never deleted."""
    try:
        emit(LIB.remove_folder(ctx(), path), f"removed {path}")
    except ValueError as e:
        fail(str(e))


@registry_app.command("attach")
def registry_attach(uri: str, profile: Optional[str] = typer.Option(None, "--profile", help="cloud login (cst cloud list)"),
                    label: Optional[str] = typer.Option(None, "--label")):
    """Attach a model registry (s3://bucket/prefix with <architecture>/<brand>/<version>/MANIFEST.yaml) and read it."""
    r = AT.attach(ctx(), uri, profile, label)
    emit(r, f"attached {r['uri']}: {r['models']} models ({r['snapshot']})")


@registry_app.command("list")
def registry_list():
    rs = AT.registries(ctx())
    emit(rs, "\n".join(f"{r['uri']}  {r.get('models') or 0} models, synced {r['synced_at'][:16]}" for r in rs) or "none attached")


@registry_app.command("detach")
def registry_detach(uri: str):
    emit(AT.detach(ctx(), uri), f"detached {uri}. Its snapshots stay.")


@registry_app.command("discover")
def registry_discover(profile: Optional[str] = typer.Option(None, "--profile", help="AWS login (cst cloud list)")):
    """List the S3 buckets this login can see, and mark the ones that hold a model registry. Reads only."""
    from .. import registry_setup as RS

    c = ctx()
    if profile is None:
        _, profile = AT.resolve(c, "s3://x", None)
    r = RS.discover(c, profile)
    lines = [f"{b['kind']:<10} {b['uri']:<60} {b['region'] or '':<12} {b['detail']}{'  (attached)' if b['attached'] else ''}"
             for b in r["buckets"]]
    emit(r, "\n".join(lines) + f"\n{r['registries']} registr{'y' if r['registries'] == 1 else 'ies'} in "
         f"{len(r['buckets'])} buckets" if lines else "no buckets this login can see")


@registry_app.command("create")
def registry_create(bucket: str, region: str = typer.Option("us-west-1", "--region"),
                    profile: Optional[str] = typer.Option(None, "--profile", help="AWS login (cst cloud list)"),
                    terraform: Optional[str] = typer.Option(None, "--terraform", help="write Terraform to this file instead of creating"),
                    cloudformation: Optional[str] = typer.Option(None, "--cloudformation", help="write the CloudFormation template to this file instead"),
                    dry_run: bool = typer.Option(False, "--dry-run")):
    """Make a model registry bucket (private, encrypted, versioned) through a CloudFormation stack, and attach it.
    With --terraform or --cloudformation, write the infrastructure code instead, for your own pipeline to apply."""
    from .. import registry_setup as RS

    for path, fmt in ((terraform, "terraform"), (cloudformation, "cloudformation")):
        if path:
            t = RS.template(bucket, region, fmt)
            with open(path, "w") as f:
                f.write(t["text"])
            emit({**t, "path": path, "text": None}, f"wrote {path}. Apply it with: {t['how']}")
            return
    if dry_run:
        emit({"bucket": RS.check_name(bucket), "region": region, "stack": RS.stack_name(bucket)},
             f"would create stack {RS.stack_name(bucket)} with bucket {bucket} in {region}, then attach it")
        return
    c = ctx()
    if profile is None:
        _, profile = AT.resolve(c, f"s3://{bucket}", None)
    r = RS.create(c, bucket, region, profile)
    emit(r, f"made {r['uri']} in {region} (CloudFormation stack {r['stack']}) and attached it")
