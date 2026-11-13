"""Local config, the current workspace, and secrets resolution.

Local config lives at $CST_HOME (default ~/.config/captain-search)/config.yaml. It holds only
workspace names and storage URIs. Shared state (connections, versions) lives in the bucket.
"""
from __future__ import annotations

import getpass
import os
import re
import subprocess
from dataclasses import dataclass

import yaml

from .model.entities import Connection, Workspace
from .storage.backend import AlreadyExists, NotFound, Store, open_store
from .storage.registry import Registry, check_name, dump_yaml

KEYRING_SERVICE = "captain-search"


def home() -> str:
    return os.path.expanduser(os.environ.get("CST_HOME", "~/.config/captain-search"))


def config_path() -> str:
    return os.path.join(home(), "config.yaml")


def load_config() -> dict:
    try:
        with open(config_path()) as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(home(), exist_ok=True)
    tmp = config_path() + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    os.replace(tmp, config_path())
    try:  # the cloud logins and this computer's settings live here, not in a store: the change feed is told by hand
        from . import changes

        changes.bump("logins", "settings")
    except Exception:  # noqa: BLE001
        pass


def whoami() -> str:
    if os.environ.get("CST_USER"):
        return os.environ["CST_USER"]
    cfg = load_config()
    if cfg.get("user"):
        return cfg["user"]
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=3).stdout.strip()
        if email:
            return email
    except Exception:
        pass
    return getpass.getuser()


@dataclass
class Context:
    workspace: Workspace
    store: Store
    registry: Registry

    # connections are shared, mutable config: one CAS-updated file per connection
    def conn_key(self, name: str) -> str:
        return f"connections/{name}.yaml"

    def save_connection(self, c: Connection, replace: bool = False) -> None:
        body = dump_yaml(c.model_dump(mode="json"))
        if not replace:
            self.store.put_new(self.conn_key(c.name), body)
            return
        try:
            _, tag = self.store.get_with_tag(self.conn_key(c.name))
        except NotFound:
            tag = None
        self.store.put_if(self.conn_key(c.name), body, tag)

    def load_connection(self, name: str) -> Connection:
        try:
            return Connection.model_validate(yaml.safe_load(self.store.get(self.conn_key(name))))
        except NotFound:
            raise NotFound(f"no connection named {name!r}. Add one with `cst conn add`.") from None

    def connections(self) -> list[str]:
        return [k[len("connections/") : -len(".yaml")] for k in self.store.list("connections/") if k.endswith(".yaml")]

    def delete_connection(self, name: str) -> None:
        # Stores expose no delete on purpose. A removed connection is a tombstone.
        c = self.load_connection(name)
        _, tag = self.store.get_with_tag(self.conn_key(name))
        self.store.put_if(self.conn_key(name), dump_yaml({**c.model_dump(mode="json"), "deleted": True}), tag)


def init_workspace(name: str, storage_uri: str, org: str = "captain", profile: str | None = None) -> Context:
    check_name(name)
    store = open_store(storage_uri, profile)
    ws = Workspace(org=org, workspace=name, name=name, storage_uri=storage_uri, created_by=whoami(), members=[whoami()])
    try:
        store.put_new("workspace.yaml", dump_yaml(ws.model_dump(mode="json")))
        from .lineage_index import mark_complete

        mark_complete(store, "new workspace")  # nothing published yet, so the edge index starts complete
    except AlreadyExists:
        ws = Workspace.model_validate(yaml.safe_load(store.get("workspace.yaml")))
        if ws.name != name:
            raise ValueError(f"{storage_uri} already holds workspace {ws.name!r}") from None
    cfg = load_config()
    cfg.setdefault("workspaces", {})[name] = {"storage": storage_uri, **({"profile": profile} if profile else {})}
    cfg.setdefault("current", name)
    save_config(cfg)
    return Context(ws, store, Registry(store))


def open_context(name: str | None = None) -> Context:
    cfg = load_config()
    name = name or os.environ.get("CST_WORKSPACE") or cfg.get("current")
    if not name:
        raise RuntimeError("no workspace selected. Run `cst init --storage <uri> <name>` first.")
    if name not in cfg.get("workspaces", {}) and os.environ.get("CST_STORAGE") and os.environ.get("CST_WORKSPACE") == name:
        # A compute job (runner image) names its workspace in the environment: attach to it, never create one.
        store = open_store(os.environ["CST_STORAGE"], os.environ.get("CST_STORAGE_PROFILE") or None)
        ws = Workspace.model_validate(yaml.safe_load(store.get("workspace.yaml")))
        cfg.setdefault("workspaces", {})[name] = {"storage": os.environ["CST_STORAGE"]}
        save_config(cfg)
        return Context(ws, store, Registry(store))
    try:
        uri = cfg["workspaces"][name]["storage"]
    except KeyError:
        raise RuntimeError(f"unknown workspace {name!r}. Known: {sorted(cfg.get('workspaces', {}))}") from None
    store = open_store(uri, cfg["workspaces"][name].get("profile"))
    ws = Workspace.model_validate(yaml.safe_load(store.get("workspace.yaml")))
    return Context(ws, store, Registry(store))


# ---- secrets: env, then keychain, then cloud secrets manager ----------------------------

SECRET_RE = re.compile(r"\{secret(_raw)?:([A-Za-z_][A-Za-z0-9_]*)\}")


def get_secret(name: str) -> str:
    if os.environ.get(name, "").strip():  # an empty variable counts as unset
        return os.environ[name]
    try:
        import keyring

        v = keyring.get_password(KEYRING_SERVICE, name)
        if v is not None:
            return v
    except Exception:
        pass  # no keychain (CI, headless Linux)
    sm = load_config().get("secrets_manager")  # e.g. {"type": "aws", "region": "us-west-1", "prefix": "cst/"}
    if sm and sm.get("type") == "aws":
        import boto3

        c = boto3.client("secretsmanager", region_name=sm.get("region"))
        return c.get_secret_value(SecretId=sm.get("prefix", "") + name)["SecretString"]
    raise KeyError(f"secret {name} not found in env, keychain or secrets manager. Set it with `cst secret set {name}`.")


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)
    cfg = load_config()
    names = set(cfg.get("secret_names", []))
    names.add(name)
    cfg["secret_names"] = sorted(names)
    save_config(cfg)


def delete_secret(name: str) -> None:
    import keyring

    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:
        pass
    cfg = load_config()
    cfg["secret_names"] = [n for n in cfg.get("secret_names", []) if n != name]
    save_config(cfg)


def render_secrets(template: str) -> str:
    """{secret:NAME} is URL-quoted, for passwords inside a URL. {secret_raw:NAME} is inserted as is."""
    from urllib.parse import quote_plus

    return SECRET_RE.sub(lambda m: get_secret(m.group(2)) if m.group(1) else quote_plus(get_secret(m.group(2))), template)
