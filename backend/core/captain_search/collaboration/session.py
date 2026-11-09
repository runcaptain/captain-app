"""Shared CLI and local desktop-daemon connection to the team service."""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .client import SyncClient
from .credentials import prepare_private_file
from . import login


class AuthenticationRequired(ValueError):
    """Configured connection needs a current credential, not a config change."""


@contextmanager
def connection(workspace=None):
    with login.credential() as token:
        with _connection(workspace, token, login.cache_identity()) as client:
            yield client


@contextmanager
def _connection(workspace, token, identity=None, server=None):
    server = server or login.origin()
    workspace = workspace or login._workspace()
    parsed = urlparse(server)
    if not workspace or not parsed.hostname:
        raise ValueError("Set CST_SYNC_SERVER and CST_SYNC_WORKSPACE (or --workspace)")
    if not token:
        raise AuthenticationRequired("Sign in again or configure CST_SYNC_TOKEN")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError("CST_SYNC_SERVER must be an origin without credentials, path or query")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Use HTTPS, or an HTTP localhost SSH tunnel")
    # Device login supplies a server-verified user identity. Environment tokens
    # retain credential-scoped caches; callers cannot select another user cache.
    identity = identity or hashlib.sha256(token.encode()).hexdigest()
    scope = hashlib.sha256(json.dumps([server, workspace, identity]).encode()).hexdigest()
    home = Path(os.environ.get("CST_SYNC_HOME", str(Path.home() / ".cache/captain/sync")))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / (scope + ".sqlite")
    lock_path = home / (scope + ".lock.sqlite")
    for private_path in (path, lock_path):
        prepare_private_file(private_path)
    # A separate SQLite transaction serializes the whole network operation across
    # desktop threads and CLI processes, not just individual cache commits. This
    # prevents a delayed authorized response from restoring data after revocation.
    with closing(sqlite3.connect(lock_path, timeout=35)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        with httpx.Client(base_url=server, headers={"Authorization": "Bearer " + token}, timeout=30, follow_redirects=False) as http:
            client = SyncClient(path, http, workspace, identity)
            try:
                yield client
            finally:
                client.close()
