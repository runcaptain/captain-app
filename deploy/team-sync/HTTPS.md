# Optional Caddy HTTPS edge

Use this after the token server, database and operator enrollment in [README.md](README.md)
are configured. This overlay never enables synthetic authentication and requires an
operator-owned hostname. It does not change DNS, firewalls or identity providers.

From `deploy/team-sync`, set `CST_TEAM_HOSTNAME` to the hostname only, without a scheme,
path or port. The default host bindings are `127.0.0.1:8080` and `127.0.0.1:8443`.
Including the file alone does not start Caddy; select the `https` profile explicitly.

```sh
export CST_TEAM_HOSTNAME=sync.your-company.example
docker compose -f compose.yaml -f compose.https.yaml --profile https run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
```

The production Caddyfile uses a public ACME issuer. For public certificates, the operator
must first point the hostname to this host and make TCP ports 80 and 443 reachable.
Only when that exposure is intended, set the public binding and standard ports:

```sh
export CST_TEAM_HTTPS_BIND=0.0.0.0
export CST_TEAM_HTTP_PORT=80
export CST_TEAM_HTTPS_PORT=443
docker compose -f compose.yaml -f compose.https.yaml --profile https up -d --wait
curl --fail "https://${CST_TEAM_HOSTNAME}/health"
```

Add `-f compose.postgres.yaml` before the HTTPS overlay when using the bundled private
PostgreSQL service. Existing database credentials remain in the protected `server.env`.
Do not use `docker compose config` on real secret configuration. The proxy receives only
its hostname, not the API's database or authentication configuration.

The API remains bound to host loopback. Caddy forwards to `api:8000` on the Compose
network. HTTP redirects to HTTPS. The proxy permits the API's 8 MiB request limit and
waits up to 120 seconds for response headers. It does not enable request access logs.
Do not enable debug/header logging for bearer-authenticated traffic.

`caddy_data` persists certificate private keys, ACME account state and renewals;
`caddy_config` persists Caddy configuration state. Protect these volumes and include
them in the operator's backup policy. Do not run `down -v` on a real installation.
Caddy's health probe checks its container-local proxy path to API `/health`. Successful
Compose health is not proof of certificate issuance: verify the public HTTPS request
without `--insecure`, then verify authenticated operations using the client.

## Isolated local CA verification

The local override is a separate, explicit test path. It uses `tls internal` for
`sync.test`, binds only loopback even if public binding variables are set, and never
installs its CA into macOS, browsers or another host trust store. Production does not
fall back to this mode. Use a fresh Compose project and synthetic database/data only.

From the repository root, run the self-contained acceptance harness:

```sh
CST_CHECK_IMAGE=captain-team-sync:token-check python3 deploy/team-sync/scripts/check_https.py
```

It copies the shipped Compose and Caddy files to a private temporary directory, writes
only synthetic database configuration, uses a unique project and ephemeral loopback port,
and disables the direct API host listener. It does not read your `server.env` or target
an existing database. It exports only the public test CA certificate to a temporary
client SSLContext; no host trust store, DNS, or hosts-file changes occur.

The check rejects default trust and wrong hostnames, then verifies token onboarding,
private/shared permissions, rotation/revocation and a full 8 MiB artifact through TLS.
It restarts Caddy and confirms certificate state persists; stopping/restarting its own
PostgreSQL instance verifies readiness failure and recovery. Cleanup removes only that
project's containers, volumes (including CA keys) and network. A local CA pass is not
evidence of live public DNS or ACME issuance.

Configuration references: [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls),
[reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
