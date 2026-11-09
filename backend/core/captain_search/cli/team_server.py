"""Dedicated company collaboration service, separate from the local daemon."""
import typer
from pathlib import Path

app = typer.Typer(help='Run the company collaboration server with explicit environment configuration.')
token_app = typer.Typer(help='Issue, list, revoke or rotate production bearer tokens through operator database access.')
app.add_typer(token_app, name='token')


@app.command()
def serve(host: str = '127.0.0.1', port: int = typer.Option(8081, min=1, max=65535)):
    import uvicorn
    from captain_search.collaboration.server import from_env
    try:
        service = from_env()
    except Exception:
        typer.echo("Server initialization failed. Check explicit configuration, database connectivity and schema version.", err=True)
        raise typer.Exit(1) from None
    uvicorn.run(service, host=host, port=port)


def operator_engine():
    import os
    from sqlalchemy import create_engine
    url = os.environ.get('CST_TEAM_DATABASE_URL')
    if not url:
        raise typer.BadParameter('CST_TEAM_DATABASE_URL is required')
    try:
        return create_engine(url, pool_pre_ping=True, hide_parameters=True)
    except Exception:
        raise typer.BadParameter("Invalid team database configuration") from None


def run_operator(operation):
    engine = operator_engine()
    try:
        return operation(engine)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    except Exception:
        # DB errors may contain connection credentials or identity parameters.
        typer.echo('Database operation failed. Check connectivity and schema with your operator.', err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()


@app.command()
def migrate():
    """Apply forward-only migrations. Requires direct operator database access."""
    from captain_search.collaboration.migrations import migrate as apply
    version = run_operator(apply)
    typer.echo(f'Schema version {version}')


@app.command()
def enroll(issuer: str, subject: str, user_id: str, workspace: str,
           role: str = typer.Option('reader')):
    """Bind a verified issuer/subject and grant membership. Never accepts tokens."""
    from captain_search.collaboration.operator import enroll as apply
    run_operator(lambda engine: apply(engine, issuer=issuer, subject=subject, user_id=user_id,
                                     workspace=workspace, role=role))
    typer.echo('Identity bound and membership granted')


@app.command('list')
def list_members(workspace: str):
    """List user IDs and roles only. Identity subjects and credentials are omitted."""
    import json
    from captain_search.collaboration.operator import list_members as read
    typer.echo(json.dumps(run_operator(lambda engine: read(engine, workspace))))


@app.command()
def revoke(workspace: str, user_id: str):
    """Revoke membership and invalidate sync epochs; preserve identity bindings."""
    from captain_search.collaboration.operator import revoke as apply
    run_operator(lambda engine: apply(engine, workspace=workspace, user_id=user_id))
    typer.echo('Membership revoked')


@app.command()
def grant(user_id: str, workspace: str, role: str = typer.Option('reader')):
    """Grant membership without an OIDC binding. Does not revive revoked tokens."""
    from captain_search.collaboration.operator import grant as apply
    run_operator(lambda engine: apply(engine, user_id=user_id, workspace=workspace, role=role))
    typer.echo('Membership granted')


@token_app.command('issue')
def issue_token(user_id: str, output: Path = typer.Option(...),
                expires_in_days: int = typer.Option(30, min=1, max=365)):
    """Write a new bearer token exclusively to a mode 0600 file. Never stdout."""
    import json
    from captain_search.collaboration.tokens import issue
    result = run_operator(lambda engine: issue(engine, user_id=user_id, output=output,
                                               expires_in_days=expires_in_days))
    typer.echo(json.dumps(result))


@token_app.command('rotate')
def rotate_token(token_id: str, output: Path = typer.Option(...),
                 expires_in_days: int = typer.Option(30, min=1, max=365)):
    """Atomically revoke a token and issue its replacement to the same user."""
    import json
    from captain_search.collaboration.tokens import issue
    result = run_operator(lambda engine: issue(engine, rotate_id=token_id, output=output,
                                               expires_in_days=expires_in_days))
    typer.echo(json.dumps(result))


@token_app.command('list')
def list_tokens(user_id: str | None = typer.Option(None)):
    """List token IDs, principal, expiry and revocation. Never hashes or tokens."""
    import json
    from captain_search.collaboration.tokens import list_tokens as read
    typer.echo(json.dumps(run_operator(lambda engine: read(engine, user_id=user_id))))


@token_app.command('revoke')
def revoke_token(token_id: str):
    from captain_search.collaboration.tokens import revoke_token as apply
    run_operator(lambda engine: apply(engine, token_id))
    typer.echo('Token revoked')
