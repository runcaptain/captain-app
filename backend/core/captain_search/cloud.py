"""Cloud profiles: one person's credentials for AWS, Google Cloud or Azure.

Profiles live in the local config (they belong to a person, not the workspace). Secret fields go to
the OS keychain (or env / secrets manager, see workspace.get_secret) under `cst-profile-<name>-<field>`.
Workspaces and compute targets name a profile; each teammate keeps their own profile of that name.
"""
from __future__ import annotations

import json
import re

from .storage.registry import check_name
from .workspace import KEYRING_SERVICE, get_secret, load_config, save_config

PROVIDERS = [
    {"provider": "aws", "label": "Amazon Web Services", "auth": [
        {"mode": "profile", "label": "AWS CLI profile", "fields": [
            {"name": "profile", "label": "Profile name", "secret": False, "required": True}]},
        {"mode": "keys", "label": "Access keys", "fields": [
            {"name": "access_key_id", "label": "Access key ID", "secret": False, "required": True},
            {"name": "secret_access_key", "label": "Secret access key", "secret": True, "required": True},
            {"name": "session_token", "label": "Session token (optional)", "secret": True, "required": False}]},
        {"mode": "role", "label": "Assume a role", "fields": [
            {"name": "role_arn", "label": "Role ARN", "secret": False, "required": True},
            {"name": "source_profile", "label": "Source profile (blank = default chain)", "secret": False, "required": False},
            {"name": "external_id", "label": "External ID (optional)", "secret": True, "required": False}]},
        {"mode": "default", "label": "Default chain (env, SSO, instance role)", "fields": []}]},
    {"provider": "gcp", "label": "Google Cloud", "auth": [
        {"mode": "service-account", "label": "Service account key (JSON)", "fields": [
            {"name": "project", "label": "Project ID", "secret": False, "required": True},
            {"name": "service_account_json", "label": "Key JSON", "secret": True, "required": True}]},
        {"mode": "adc", "label": "Application default credentials (gcloud auth)", "fields": [
            {"name": "project", "label": "Project ID", "secret": False, "required": True}]}]},
    {"provider": "azure", "label": "Microsoft Azure", "auth": [
        {"mode": "service-principal", "label": "Service principal", "fields": [
            {"name": "tenant_id", "label": "Tenant ID", "secret": False, "required": True},
            {"name": "client_id", "label": "Client ID", "secret": False, "required": True},
            {"name": "subscription_id", "label": "Subscription ID", "secret": False, "required": True},
            {"name": "client_secret", "label": "Client secret", "secret": True, "required": True}]},
        {"mode": "connection-string", "label": "Storage connection string", "fields": [
            {"name": "connection_string", "label": "Connection string", "secret": True, "required": True}]},
        {"mode": "default", "label": "Default credential (az login, managed identity)", "fields": [
            {"name": "subscription_id", "label": "Subscription ID", "secret": False, "required": False}]}]},
    # Modal signs in by itself on this computer (`modal token new`), so the login holds no secret: connecting it
    # here is how a person says "offer Modal in this Captain", and which Modal environment to use.
    {"provider": "modal", "label": "Modal", "auth": [
        {"mode": "local", "label": "This computer's Modal sign-in", "fields": [
            {"name": "environment", "label": "Modal environment (blank for your default)", "secret": False, "required": False}]}]},
]


def _spec(provider: str, mode: str) -> dict:
    p = next((p for p in PROVIDERS if p["provider"] == provider), None)
    if p is None:
        raise ValueError(f"unknown provider {provider!r}: use aws, gcp, azure or modal")
    a = next((a for a in p["auth"] if a["mode"] == mode), None)
    if a is None:
        raise ValueError(f"{provider} has no auth mode {mode!r}: use {', '.join(x['mode'] for x in p['auth'])}")
    return a


def secret_name(profile: str, field: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"CST_PROFILE_{profile}_{field}").upper()


def profiles() -> list[dict]:
    cfg = load_config()
    return [{"name": n, **p} for n, p in sorted((cfg.get("profiles") or {}).items())]


def get_profile(name: str) -> dict:
    p = (load_config().get("profiles") or {}).get(name)
    if p is None:
        raise ValueError(f"no cloud profile named {name!r}. Add one in Settings > Cloud or with `cst cloud add`.")
    return {"name": name, **p}


def add_profile(name: str, provider: str, auth: str, region: str | None, fields: dict, secrets: dict) -> dict:
    _forget_kept_clients()
    check_name(name)
    spec = _spec(provider, auth)
    allowed = {f["name"]: f for f in spec["fields"]}
    for k in list(fields) + list(secrets):
        if k not in allowed:
            raise ValueError(f"unknown field {k!r} for {provider} {auth}")
        if k in fields and allowed[k]["secret"]:
            raise ValueError(f"{k} is a secret field: send it in secrets, not fields")
    for f in spec["fields"]:
        src = secrets if f["secret"] else fields
        if f["required"] and not str(src.get(f["name"], "")).strip():
            raise ValueError(f"{f['label']} is required")
    if auth == "service-account" and secrets.get("service_account_json"):
        try:
            json.loads(secrets["service_account_json"])
        except json.JSONDecodeError:
            raise ValueError("the service account key is not valid JSON") from None
    import keyring

    for k, v in secrets.items():
        if str(v).strip():
            keyring.set_password(KEYRING_SERVICE, secret_name(name, k), str(v))
    cfg = load_config()
    cfg.setdefault("profiles", {})[name] = {"provider": provider, "auth": auth, "region": region,
                                            "fields": {k: v for k, v in fields.items() if str(v).strip()},
                                            "secret_fields": sorted(k for k, v in secrets.items() if str(v).strip())}
    save_config(cfg)
    return get_profile(name)


def set_label(name: str, label: str) -> dict:
    """The words a person named this login with ("GPU cloud"), kept beside the id Captain made from them."""
    cfg = load_config()
    if name not in (cfg.get("profiles") or {}):
        raise ValueError(f"no cloud account named {name}")
    cfg["profiles"][name]["label"] = " ".join(str(label or "").split())[:80] or None
    save_config(cfg)
    return get_profile(name)


def delete_profile(name: str) -> None:
    _forget_kept_clients()
    import keyring

    p = get_profile(name)
    for k in p.get("secret_fields", []):
        try:
            keyring.delete_password(KEYRING_SERVICE, secret_name(name, k))
        except Exception:
            pass
    cfg = load_config()
    cfg.get("profiles", {}).pop(name, None)
    save_config(cfg)


def _secret(p: dict, field: str) -> str | None:
    if field not in p.get("secret_fields", []):
        return None
    return get_secret(secret_name(p["name"], field))


# ---- clients ---------------------------------------------------------------------------


def aws_session(profile: str | dict | None, region: str | None = None):
    import boto3

    if profile is None:
        return boto3.Session(region_name=region)
    p = get_profile(profile) if isinstance(profile, str) else profile
    region = region or p.get("region")
    f = p.get("fields", {})
    if p["auth"] == "profile":
        return boto3.Session(profile_name=f["profile"], region_name=region)
    if p["auth"] == "keys":
        return boto3.Session(aws_access_key_id=f["access_key_id"], aws_secret_access_key=_secret(p, "secret_access_key"),
                             aws_session_token=_secret(p, "session_token"), region_name=region)
    if p["auth"] == "role":
        base = boto3.Session(profile_name=f.get("source_profile") or None, region_name=region)
        kw = {"RoleArn": f["role_arn"], "RoleSessionName": "captain-search-tools"}
        if _secret(p, "external_id"):
            kw["ExternalId"] = _secret(p, "external_id")
        c = base.client("sts").assume_role(**kw)["Credentials"]
        return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                             aws_session_token=c["SessionToken"], region_name=region)
    return boto3.Session(region_name=region)


def gcp_credentials(profile: str | dict):
    p = get_profile(profile) if isinstance(profile, str) else profile
    project = p.get("fields", {}).get("project")
    if p["auth"] == "service-account":
        from google.oauth2 import service_account  # type: ignore

        info = json.loads(_secret(p, "service_account_json"))
        return service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]), project
    import google.auth  # type: ignore

    creds, default_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return creds, project or default_project


def azure_credential(profile: str | dict):
    """Returns (credential or None, connection_string or None, subscription_id or None)."""
    p = get_profile(profile) if isinstance(profile, str) else profile
    f = p.get("fields", {})
    if p["auth"] == "connection-string":
        return None, _secret(p, "connection_string"), None
    if p["auth"] == "service-principal":
        from azure.identity import ClientSecretCredential  # type: ignore

        return ClientSecretCredential(f["tenant_id"], f["client_id"], _secret(p, "client_secret")), None, f.get("subscription_id")
    from azure.identity import DefaultAzureCredential  # type: ignore

    return DefaultAzureCredential(), None, f.get("subscription_id")


def test_profile(name: str) -> dict:
    """Check the login and remember the result (who it signed in as, when), so the app can show it without asking
    the cloud on every render."""
    import datetime as dt

    r = _test_profile(name)
    cfg = load_config()
    prof = (cfg.get("profiles") or {}).get(name)
    if prof is not None:
        prof["last_test"] = {**{k: r.get(k) for k in ("ok", "identity", "error")},
                             "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        save_config(cfg)
    return r


def _test_profile(name: str) -> dict:
    p = get_profile(name)
    try:
        if p["provider"] == "aws":
            ident = aws_session(p).client("sts").get_caller_identity()
            return {"ok": True, "identity": ident["Arn"]}
        if p["provider"] == "gcp":
            creds, project = gcp_credentials(p)
            from google.auth.transport.requests import Request  # type: ignore

            creds.refresh(Request())
            return {"ok": True, "identity": getattr(creds, "service_account_email", None) or f"project {project}"}
        if p["provider"] == "modal":
            return _test_modal()
        cred, conn, _ = azure_credential(p)
        if conn:
            from azure.storage.blob import BlobServiceClient  # type: ignore

            acct = BlobServiceClient.from_connection_string(conn).get_account_information()
            return {"ok": True, "identity": f"storage account ({acct.get('account_kind')})"}
        cred.get_token("https://management.azure.com/.default")
        return {"ok": True, "identity": "Azure token issued"}
    except ImportError as e:
        return {"ok": False, "error": f"the {p['provider']} SDK is not installed: pip install 'captain-search[{p['provider']}]' ({e.name})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


def _test_modal() -> dict:
    """Is this computer signed in to Modal? Read from Modal's own settings: nothing is sent anywhere."""
    from modal.config import config  # type: ignore

    if config.get("token_id") and config.get("token_secret"):
        return {"ok": True, "identity": "this computer's Modal sign-in"}
    return {"ok": False, "error": "This computer isn't signed in to Modal. Run `modal token new` in a terminal."}


def signin_found(provider: str, auth: str) -> dict:
    """For the Connect a cloud account form, which checks by itself: is the sign-in this choice relies on there?
    {found, text}. Only a sign-in kept on this computer can be looked for without a secret: Modal's. For anything
    else `found` is None, and nothing is said."""
    if provider == "modal" and auth == "local":
        try:
            ok = bool(_test_modal().get("ok"))
        except Exception:  # noqa: BLE001  the Modal package is not installed here
            ok = False
        return {"found": ok, "text": None if ok else
                "No Modal sign-in on this computer yet. Run `modal token new` in a terminal, then come back."}
    return {"found": None, "text": None}


def provider_of(uri: str) -> str:
    s = uri.split("://", 1)[0] if "://" in uri else "file"
    return {"s3": "aws", "gs": "gcp", "gcs": "gcp", "az": "azure", "azure": "azure"}.get(s, "local")


def summary(ctx) -> dict:
    """Which cloud this workspace is on: its storage, the login it uses, every login on this machine with its last
    check, and the compute targets that can run work. Read from config and the workspace only. Nothing calls the cloud."""
    from . import compute as C

    cfg = load_config()
    ws_cfg = (cfg.get("workspaces") or {}).get(ctx.workspace.name, {})
    storage = ctx.workspace.storage_uri
    provider = provider_of(storage)
    storage_profile = ws_cfg.get("profile")
    profs = []
    for p in profiles():
        profs.append({"name": p["name"], "provider": p["provider"], "auth": p["auth"], "region": p.get("region"),
                      "label": next((x["label"] for x in PROVIDERS if x["provider"] == p["provider"]), p["provider"]),
                      "account_hint": (p.get("fields") or {}).get("profile") or (p.get("fields") or {}).get("project")
                      or (p.get("fields") or {}).get("subscription_id"),
                      "last_test": p.get("last_test"), "used_by_storage": p["name"] == storage_profile})
    targets = []
    for t in C.listing(ctx):
        targets.append({"name": t["name"], "kind": t["kind"], "provider": t.get("provider"), "profile": t.get("profile"),
                        "region": t.get("region"), "purposes": t.get("purposes", [])})
    prov_label = {"aws": "Amazon Web Services", "gcp": "Google Cloud", "azure": "Microsoft Azure", "local": "This computer"}
    active = next((p for p in profs if p["used_by_storage"]), None) or (profs[0] if len(profs) == 1 else None)
    return {"workspace": ctx.workspace.name, "storage": storage, "storage_provider": provider,
            "storage_provider_label": prov_label.get(provider, provider), "storage_profile": storage_profile,
            "active": active, "profiles": profs, "compute": targets,
            "can_run_in_cloud": any(t["kind"] != "local" for t in targets),
            "notes": ([] if any(t["kind"] != "local" for t in targets) else
                      ["No cloud compute target yet: offline evaluations run on this computer. Add one with `cst compute add`."])}


def _forget_kept_clients() -> None:
    """A changed or removed login must not go on being used through a client kept from before."""
    try:
        from .compute_providers import aws

        aws.forget_clients()
    except Exception:  # noqa: BLE001
        pass
