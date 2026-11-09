"""Explicit opt-in collaboration commands. Existing bucket workspaces are unchanged."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import httpx
import typer


app = typer.Typer(no_args_is_help=True, help="Experimental team server sync and offline drafts")


@contextmanager
def connection():
    from .main import S
    from ..collaboration.session import connection as open_connection
    with open_connection(S.workspace) as client:
        yield client


def run(action, *, writes=False):
    from .main import S, emit, fail
    if S.dry_run:
        return emit({"dry_run": True, "action": action})
    try:
        with connection() as client:
            if action == "pull":
                result = client.pull()
            elif action == "push":
                client.flush()
                result = {"drafts": client.drafts()}
            elif action == "drafts":
                result = client.drafts()
            else:
                result = [json.loads(row[0]) for row in client.db.execute("SELECT data FROM records ORDER BY id")]
            emit(result)
    except httpx.HTTPStatusError as exc:
        fail(f"Sync server returned HTTP {exc.response.status_code}")
    except httpx.RequestError:
        fail("Sync server unavailable. Pending drafts remain local.")
    except sqlite3.Error:
        fail("Local team cache is busy or unavailable. Retry the operation.")
    except (ValueError, OSError) as exc:
        fail(str(exc))


@app.command()
def pull():
    """Refresh authorized metadata and catch up from the saved cursor."""
    run("pull", writes=True)


@app.command()
def push():
    """Submit queued drafts; retain conflicts for explicit resolution."""
    run("push", writes=True)


@app.command("list")
def list_records():
    """Read cached records offline. Run pull to check current permissions."""
    run("list")


@app.command()
def drafts():
    """Show queued and conflicting local drafts."""
    run("drafts")


@app.command()
def edit(record_id: str, body: Path = typer.Option(..., exists=True, dir_okay=False),
         kind: str | None = None, visibility: str | None = None):
    """Queue a JSON object offline. New records are private unless specified."""
    from .main import S, emit, fail
    try:
        value = json.loads(body.read_text())
        if not isinstance(value, dict):
            raise ValueError("Body must be a JSON object")
        if visibility not in (None, "private", "workspace"):
            raise ValueError("Visibility must be private or workspace")
        if S.dry_run:
            return emit({"dry_run": True, "record_id": record_id, "body": value})
        with connection() as client:
            current = client.get(record_id)
            record_kind = kind or (current['kind'] if current else 'config')
            mutation = client.queue_edit(record_id, record_kind, value, visibility)
        emit({"mutation_id": mutation, "state": "pending"})
    except sqlite3.Error:
        fail("Local team cache is busy or unavailable. Retry the operation.")
    except (ValueError, OSError) as exc:
        fail(str(exc))


@app.command()
def resolve(mutation_id: str, body: Path = typer.Option(..., exists=True, dir_okay=False)):
    """Replace a conflicting draft with reviewed JSON against the cached revision."""
    from .main import S, emit, fail
    try:
        value = json.loads(body.read_text())
        if not isinstance(value, dict):
            raise ValueError("Body must be a JSON object")
        if S.dry_run:
            return emit({"dry_run": True, "resolves": mutation_id, "body": value})
        with connection() as client:
            replacement = client.resolve(mutation_id, value)
        emit({"mutation_id": replacement, "state": "pending", "resolves": mutation_id})
    except sqlite3.Error:
        fail("Local team cache is busy or unavailable. Retry the operation.")
    except (ValueError, OSError) as exc:
        fail(str(exc))
