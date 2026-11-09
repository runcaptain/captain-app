"""Opt-in local desktop API. Team credentials stay in the daemon environment."""
import json
import sqlite3
from contextlib import contextmanager
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, ValidationError

from .session import connection, AuthenticationRequired
from .client import write_permission_denied, StaleEditorError
from . import login

router = APIRouter(prefix="/api/team")


@contextmanager
def session():
    try:
        with connection() as client:
            yield client
    except login.credentials.KeychainUnavailable:
        raise HTTPException(503, "Native OS keychain unavailable") from None
    except AuthenticationRequired:
        raise HTTPException(401, "Sign in again; local drafts are preserved") from None
    except httpx.HTTPStatusError as exc:
        if write_permission_denied(exc.response):
            raise HTTPException(403, {"code": "write_denied", "message":
                "You cannot publish this change with your current role. Local drafts are retained."}) from None
        if exc.response.status_code in (401, 403):
            login.invalidate()
        raise HTTPException(exc.response.status_code, "Team server rejected the request") from None
    except httpx.RequestError:
        raise HTTPException(503, "Team server unavailable; drafts remain local") from None
    except sqlite3.Error:
        raise HTTPException(503, "Local team cache is busy or unavailable") from None
    except StaleEditorError:
        raise HTTPException(409, "Server history changed. Reload the record before editing or resolving.") from None
    except (ValueError, OSError):
        raise HTTPException(400, "Team configuration or draft is invalid") from None


class Edit(BaseModel):
    body: dict
    kind: str | None = Field(default=None, max_length=100)
    visibility: Literal["private", "workspace"] | None = None
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    expected_history: str | None = Field(default=None, min_length=1, max_length=100)


@router.get("/records")
def records():
    with session() as client:
        return {"records": [json.loads(r[0]) for r in client.db.execute("SELECT data FROM records ORDER BY id")],
                "drafts": client.drafts(), "cursor": client._get("cursor"), "cached": True,
                "history_id": client._get("history_id"),
                "workspace": client.workspace, "server": str(client.http.base_url)}


@router.post("/pull")
def pull():
    with session() as client:
        client.pull()
        return {"cursor": client._get("cursor"), "epoch": client._get("epoch")}


@router.post("/push")
def push():
    with session() as client:
        client.flush()
        return {"drafts": client.drafts(), "cursor": client._get("cursor")}


@router.put("/records/{record_id}/draft")
def edit(record_id: str, value: Edit):
    with session() as client:
        current = client.get(record_id)
        kind = value.kind or (current['kind'] if current else 'config')
        expected = {'expected_revision': value.expected_revision}
        if 'expected_history' in value.model_fields_set:
            expected['expected_history'] = value.expected_history
        mutation = client.queue_edit(record_id, kind, value.body, value.visibility, **expected)
        return {"mutation_id": mutation, "state": "pending"}


@router.post("/drafts/{mutation_id}/resolve")
def resolve(mutation_id: str, value: Edit):
    with session() as client:
        expected = {'expected_revision': value.expected_revision}
        if 'expected_history' in value.model_fields_set:
            expected['expected_history'] = value.expected_history
        replacement = client.resolve(mutation_id, value.body, **expected)
        return {"mutation_id": replacement, "state": "pending"}


def login_call(operation):
    try:
        return operation()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise HTTPException(code if code in (401, 403) else 502, "Team authentication failed; check credential and membership") from None
    except httpx.RequestError:
        raise HTTPException(503, "Company sign-in failed; check provider configuration and membership") from None
    except login.credentials.KeychainUnavailable:
        raise HTTPException(503, "Native OS keychain unavailable; no plaintext credential fallback") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local team cache is unavailable; sign in again when ready") from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "Company sign-in is unavailable or expired; check operator configuration and start again") from None


@router.get("/connection")
def connection_status():
    return login_call(login.status)


@router.post("/login/start")
def login_start():
    return login_call(login.start)


@router.post("/login/poll")
def login_poll():
    return login_call(login.poll)


@router.post("/logout")
def logout():
    return login_call(login.logout)


@router.delete("/drafts/{mutation_id}")
def discard(mutation_id: str):
    with session() as client:
        client.discard(mutation_id)
        return {"state": "discarded"}


class Connect(BaseModel):
    server: str = Field(min_length=1, max_length=2048)
    workspace: str = Field(min_length=1, max_length=200)
    token: SecretStr


@router.post("/connect")
async def connect(request: Request):
    # FastAPI validation details can echo an entire invalid body, including its
    # token. Parse locally and return only a constant error on every bad shape.
    try:
        raw = await request.body()
        if len(raw) > 32768:
            raise ValueError('oversized')
        value = Connect.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise HTTPException(400, "Invalid team connection fields") from None
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(login_call, lambda: login.connect(value.server, value.workspace, value.token.get_secret_value()))
