# Team collaboration

The packaged service synchronizes private/shared records, explicit local drafts, jobs
and bounded artifacts. Existing bucket workspaces and legacy `cst sync` are unchanged.
The first production auth mode is per-user, operator-issued opaque tokens. Company SSO
is optional. Synthetic fixture tokens are a separate mode and must never serve real data.

## Server and operator

Install `backend/core[team-server]`. Configure `CST_TEAM_DATABASE_URL`, private writable
`CST_TEAM_ARTIFACT_ROOT`, and `CST_TEAM_AUTH_MODE=tokens`. Run:

```sh
cst team-server migrate
cst team-server grant lewis team --role admin
cst team-server token issue lewis --output /private/onboarding/lewis.token
cst team-server serve
```

Use PostgreSQL for company service. Schema version 3 adds separate credential records;
legacy user hashes are preserved for compatibility but never accepted in production
token mode. Serving requires a current schema and performs no automatic DDL. Migrations
are forward-only, serialized and reject unknown newer versions.

Token bytes are generated cryptographically and only SHA256 hashes reach the database.
Each token has a safe ID, stable user principal, issue time, expiry and optional revocation
time. Default lifetime is 30 days (1 to 365 allowed). `token list [--user-id USER]` exposes
metadata only. `token revoke TOKEN_ID` is idempotent. `token rotate TOKEN_ID --output
NEW_FILE` issues a new credential for the same principal and revokes the old in one
transaction. File or commit failure rolls back rotation. Issue/rotate write only a new
0600 file, rejecting existing paths/symlinks. No bearer token is accepted as a command
argument or printed. Protect and privately deliver the onboarding file.

Direct database access is the operator boundary. API credentials cannot administer
credentials. Workspace membership remains separate: `team-server list WORKSPACE` and
`team-server revoke WORKSPACE USER` review/revoke membership. Regrant never revives a
revoked token. Every request checks token expiry/revocation and membership. In-flight
requests already authorized may finish; offline cached data cannot be recalled remotely.

Expose the service only through HTTPS. The Compose API binds to loopback; operator-managed
TLS is required for remote access. `deploy/team-sync/README.md` is the full installation,
onboarding, rotation, backup and verification runbook. It includes optional PostgreSQL
Compose and a private virtualenv installer.

## Desktop connection and local client

The **Team collaboration** native mod connects through authenticated local daemon routes.
Enter server origin, workspace and token. The input is masked and cleared after submission.
`POST /api/team/connect` verifies server identity and workspace permissions, then saves
the credential in the native OS keychain. `connection.json` contains only nonsecret
server/workspace/account-locator metadata. The keychain envelope binds the credential
to that server, workspace and verified user identity, preventing local config edits
from redirecting the token. Unsupported/unavailable keychains fail closed; no plaintext
fallback is used.

A restarted daemon reads the keychain and verifies identity and permissions before
showing cached records. An already-verified process can edit local drafts offline.
Caches use server-verified stable principals, so reconnecting with a rotated token
preserves authorized drafts. Different users/servers cannot share a cache. Expired or
revoked credentials produce 401 and hide records/drafts pending valid reconnection;
workspace revocation purges them. A write permission denial retains local proposals.
Disconnect removes the saved locator/keychain entry and hides the view. If OS deletion
fails, the response reports that cleanup remains necessary rather than claiming success.

All `/api/team` routes require the local daemon bearer token: connection, connect,
optional login start/poll, logout, records, pull, push, record drafts, draft resolution
and draft deletion. Remote credentials stay in the daemon/keychain, never responses.
Malformed token input and keychain/provider failures use sanitized errors.

The CLI shares this client and cache. Operator environment configuration remains
supported: `CST_SYNC_SERVER`, `CST_SYNC_WORKSPACE`, `CST_SYNC_TOKEN`, optional
`CST_SYNC_HOME`. Environment credentials retain legacy credential-scoped caches and
cannot be removed by UI logout. Normal desktop onboarding uses the saved keychain path.
No HTTP origin is accepted except localhost/loopback; credentials are not redirected.

```sh
cst --json team pull
cst --json team list
cst --json team edit investigation-id --kind investigation --body draft.json --visibility workspace
cst --json team push
cst --json team drafts
cst --json team resolve MUTATION_ID --body reviewed.json
```

New records are private. Edits retain kind/visibility. Save queues a draft; only Push
publishes. Conflicts retain the proposal and require explicit reviewed resolution.
Pull before reviewing a conflict. Discard removes a local proposal only, never undoes
a published change. Team workspace selection is independent of bucket workspace UI.

## Artifacts and optional SSO

Local artifact storage supports one server instance. An optional S3 adapter uses shared
staging, bounded checksum verification and conditional immutable object publication,
with server authorization on every read. Set `CST_TEAM_ARTIFACT_BUCKET` and optional
`CST_TEAM_ARTIFACT_PREFIX`; see the runbook for IAM and lifecycle requirements.

To enable SSO later, explicitly set `CST_TEAM_AUTH_MODE=oidc` and configure HTTPS issuer,
dedicated API audience and JWKS endpoint. Fixed RS256 verification checks issuer,
audience, signature, expiry, issued-at and subject. It never discovers trust from token
headers. Operator enrollment binds verified `(issuer, subject)` to a stable principal;
claims never grant membership. Optional device login uses configured HTTPS endpoints
and public client ID. No real company SSO has been configured or tested.

Synthetic-only startup requires `CST_TEAM_DEV_TOKENS=synthetic-only` and optional private
`CST_TEAM_DEV_FIXTURE`. It cannot be mixed with an explicitly selected production mode.
Direct factories require `allow_dev_tokens=True` for fixtures. Production token mode
requires `auth_mode='tokens'` and never accepts those legacy synthetic hashes.

Self-host setup and reproducible checks are documented in `deploy/team-sync/README.md`.
Native tests require isolated profiles and synthetic data. No public release or live
company SSO verification is implied by those tests.
