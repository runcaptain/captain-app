# Self-hosted team collaboration

The first team version uses operator-issued, per-user opaque tokens. Company SSO is
optional and deferred. Set `CST_TEAM_AUTH_MODE=tokens`; do not enable synthetic/dev
mode for real data. Server-side workspace roles and private/shared records apply to
every request. Tokens are cryptographically random, expire, can be revoked or rotated,
and are stored only as SHA256 hashes in PostgreSQL.

The supplied Compose API binds to loopback. The optional [Caddy HTTPS profile](HTTPS.md)
provides the reverse proxy; configure the operator-owned hostname before remote company use. Desktop connections reject HTTP except loopback
and never follow redirects with credentials. Use a dedicated PostgreSQL database,
persistent artifact storage, restricted service credentials and tested backups.
The local disk artifact adapter supports one service instance; use S3 for shared storage.
No public deployment is performed by these scripts.

## First operator bootstrap

A. Configure private `server.env` with PostgreSQL and `CST_TEAM_AUTH_MODE=tokens`.
B. Run migration, grant your stable user ID an admin role, and issue its token to a new
   private onboarding file using the commands below.
C. Configure the approved hostname and HTTPS exposure using [HTTPS.md](HTTPS.md).
   Keep the default loopback bindings until the operator has approved network exposure.
D. Verify HTTPS certificate validation and authenticated access, then connect Captain
   using the server origin, workspace and onboarding token. No SSO setup is needed.
E. Run and retain a successful [metadata/artifact restore drill](BACKUP.md) before
   company use. The helper supports local artifact volumes; S3 requires its own plan.

The complete isolated acceptance requires no actual hostname or cloud account:

```sh
python3 deploy/team-sync/scripts/check_https.py
python3 deploy/team-sync/scripts/check_backup_restore.py
```

Run these from the repository root after building `captain-team-sync:token-check`.
Both create synthetic private resources and remove them after verification.

## Install and initialize

Copy `server.env.example` to ignored `server.env`, populate it through your secret
manager and set mode 0600. Do not print resolved Compose configuration containing
credentials. From this directory:

```sh
docker compose build
docker compose run --rm migrate
docker compose run --rm migrate cst team-server grant lewis team --role admin
docker compose run --rm migrate cst team-server grant teammate team --role editor
docker compose up -d api
curl --fail http://127.0.0.1:8081/health
```

`grant` creates a stable principal and workspace membership without an identity provider.
Choose durable user IDs, not display names that will change. Roles are reader, editor
or admin. Granting membership does not issue a credential. `team-server list team`
shows membership; `team-server revoke team USER_ID` revokes workspace access. Regranting
membership never revives a revoked token.

For a new dedicated database, `compose.postgres.yaml` adds PostgreSQL 17 with a persistent
volume and no host port. Set `CST_TEAM_POSTGRES_PASSWORD` in `server.env`; set the URL to
`postgresql+psycopg://captain:URL_ENCODED_PASSWORD@postgres/captain_team`. Use the same
password, URL-encoding special characters only in the URL. For every command use:

```sh
docker compose --env-file server.env -f compose.yaml -f compose.postgres.yaml up -d
```

Use that same prefix for operator commands. Never run `down -v` on real data.

Without Docker, Python 3.11+ can install a private versioned virtual environment:

```sh
PYTHON=python3.12 ./scripts/install.sh /absolute/path/captain-team-v1
```

The installer refuses existing directories, installs the package and checks its CLI.
It does not start or modify OS services. Load environment privately; set
`CST_TEAM_ARTIFACT_ROOT` to a private persistent writable directory. Use the installed
`bin/cst team-server migrate`, `grant` and `serve` commands under a dedicated supervisor
user, behind HTTPS. TLS, DNS, firewall, backups and supervision remain operator work.

## Issue and deliver a user's token

All token commands require direct operator database access. Ordinary API tokens cannot
issue, list, rotate or revoke other credentials. Create a private onboarding directory
(mode 0700). With the private installation and operator environment loaded:

```sh
cst team-server token issue lewis --output /private/onboarding/lewis.token --expires-in-days 30
cst team-server token list --user-id lewis
cst team-server token rotate TOKEN_ID --output /private/onboarding/lewis-new.token --expires-in-days 30
cst team-server token revoke TOKEN_ID
```

Issue/rotate require a new output path and refuse existing files or symlinks. They write
only that file (mode 0600), never a token to stdout. Output/list metadata contains token
ID, principal ID, issued/expiry/revocation times, never the credential or its hash.
Default expiry is 30 days; permitted lifetimes are 1 to 365 days. Token IDs are safe
operator handles, not bearer credentials. Do not pass a bearer token in command arguments.

For Docker, bind-mount your private onboarding directory at `/output` into the operator
container, use your host UID/GID for output ownership, and pass `--output /output/lewis.token`.
For example, with the private directory already created:

```sh
docker compose run --rm --user "$(id -u):$(id -g)" \
  -v /private/onboarding:/output migrate \
  cst team-server token issue lewis --output /output/lewis.token
```

Deliver the file through an approved private channel to that user. Do not paste it into
logs, tickets or shared chat. After the user stores it in their OS keychain through the
app, remove the onboarding copy according to your credential-handling policy.

Rotation atomically creates a replacement for the same principal and revokes the old
token. File or database failure rolls back the rotation. There is no overlap period:
the user reconnects with the replacement token. Revocation is idempotent and expiry is
checked on every request. Unknown, expired and revoked tokens all return 401. Membership
revocation returns 403. A reader's denied write retains their local draft.

## Connect from Captain

Open **Team collaboration**, enter the HTTPS server origin, workspace and your token,
then choose **Connect**. The token field is masked and cleared after submission. The
local daemon verifies the principal and membership against the server before accepting
the connection or displaying cached data. It stores the token in the OS keychain,
not in config, SQLite or logs. A nonsecret connection locator lives under `CST_SYNC_HOME`.
There is no plaintext fallback when a secure keychain is unavailable or locked.

Subsequent daemon starts restore the keychain credential and revalidate server identity
and permissions before exposing the cache. Already-verified sessions can work with local
drafts offline. Reconnect through **Connection settings** to rotate the credential;
the server-verified principal cache retains authorized drafts. Wrong-user or other-server
connections cannot inherit another principal's cache. Only an explicit Push publishes.

Disconnect removes the saved connection and keychain credential and hides the view.
Expired/revoked credentials hide cached data until a valid connection is established;
confirmed workspace revocation purges synchronized records and drafts. Private drafts
are not shared by saving them. Discard removes only a local proposal and cannot undo an
already-published server change. Cached offline copies cannot be recalled before contact.
Operator environment-token mode remains available for development/automation but is not
the desktop onboarding path; an environment credential cannot be removed by UI logout.

## Storage, upgrades and operations

Schema migrations are forward-only. Version 3 adds the token table; version 4 adds
workspace history IDs. Paired restore rotates those IDs before the API resumes.
Clients must reload restored history and explicitly review stale drafts. Upgrade clients
before resuming writes after restore; legacy writers without history IDs are rejected.
Serving requires the current schema and performs no automatic DDL in production modes.
Run migrations with operator DDL credentials, then serve with credentials limited to
application reads/writes and schema-version reads. Back up PostgreSQL and artifact bytes
together before upgrading. Restore both for incompatible rollback; do not restore only
metadata after new artifact publication. Test restores before company use.

For S3, set `CST_TEAM_ARTIFACT_BUCKET` and optionally `CST_TEAM_ARTIFACT_PREFIX` (default
`team-sync`). Use a scoped AWS role with GetObject/PutObject on that prefix and
DeleteObject restricted to `<prefix>/staging/*` for rejected upload cleanup. No blob
delete, public ACL or list permission is required. Keep public access blocked.
The adapter requests AES256 encryption and conditional immutable writes. A local
`CST_TEAM_ARTIFACT_ROOT` remains required as a bounded upload spool. Expire only staging
objects with a lifecycle long enough for delayed finalization, never blobs automatically.
If storage or the database is unavailable during cleanup, the adapter logs a sanitized
warning and preserves possibly committed data; the staging lifecycle handles leftovers.
Coordinate S3 versioning/retention with DB backups. Live S3 verification remains separate.

The reverse proxy must permit the bounded 8 MiB upload contract and appropriate timeouts.
Health alone is not acceptance: verify two user identities, private/shared records,
conflicts, token expiry/rotation/revocation, logout and recovery. Distributed execution
workers are separate from this metadata and artifact synchronization service.

## Optional SSO

SSO is not needed for token setup. To enable it later, explicitly select
`CST_TEAM_AUTH_MODE=oidc` with verified HTTPS issuer/JWKS URL and dedicated API audience.
Enroll verified `(issuer, subject)` bindings with `team-server enroll`; never guess from
email. Configure desktop device/token HTTPS endpoints, public device-flow client ID,
audience and required scopes. The existing fixed-RS256 verifier stays fail-closed and
never discovers trust endpoints from token claims. No live company SSO has been tested.

## Isolated verification

From repository root:

```sh
docker build -f experiments/team-sync/Dockerfile -t captain-team-sync:token-check .
python3 deploy/team-sync/scripts/check_token_container.py
```

This uses production token authentication with synthetic records in a dedicated
PostgreSQL 17 container. It exercises operator commands, permissions, wrong/expired/
revoked credentials, rotation, restart and hash-only persistence, then removes its own
containers, network and volume. It never sets the synthetic auth bypass.

`scripts/check_postgres.py` provides a private Unix-socket PostgreSQL contract suite.
`scripts/check_keychain.py` creates one synthetic OS-keychain item, checks it from a
separate process, then deletes it. Native acceptance uses only an explicitly coordinated
isolated profile. Keep private acceptance evidence outside source control.
