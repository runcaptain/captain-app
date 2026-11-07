# Back up and restore a local-artifact deployment

`backup_local.py` captures one PostgreSQL 17 database and its local artifact Docker
volume as a pair. It stops the API containers you name, waits for their processes to
exit, requires zero remaining database clients, and then takes a custom-format
`pg_dump` plus an artifact archive. The API containers that were running are restarted
in `finally`, including on backup failure. Already stopped containers remain stopped.

This is a maintenance-window backup, not a zero-downtime snapshot. Stop every writer,
including job workers, operator commands, direct database sessions, and any additional
API replica. Keep them stopped for the entire capture. The helper cannot prevent an
unlisted process from reconnecting during the backup. Restrict operator access and
pause automation during this window. Never use `docker pause`: in-flight writes need
to finish or abort before the dump and artifact copy.

## Capture

Discover and verify your actual container and volume names using `docker compose ps`
and `docker volume ls`. The following names are placeholders. Commands contain no
password or token. Database and user names must match `[A-Za-z_][A-Za-z0-9_]{0,62}`.
Connection strings and command flags are rejected before Docker is called. PostgreSQL authentication comes from the existing container's local
operator access; remote-database deployments need an equivalent controlled workflow.

```sh
python3 scripts/backup_local.py backup \
  --postgres captain-team-postgres-1 --user captain --database captain_team \
  --artifacts captain-team_artifacts \
  --writer captain-team-api-1 \
  --output /private/backups/captain-team-2026-11-07
```

Repeat `--writer` for every API writer container. The output directory must not exist;
its parent must exist. Each listed writer must mount the named volume at its configured
artifact root; a configured S3 bucket is rejected. The helper creates a mode 0700 directory with mode 0600 files:
`metadata.dump`, `artifacts.tar`, and a checksum manifest. No token, token hash, record,
or artifact contents are printed. Database backups contain token hashes, identities,
and private records, so treat the whole backup as confidential. Encrypt it before
external storage or transfer using your approved backup system. This helper does not
upload it anywhere or provide encryption, retention, or automatic schedules.

A directory without its final valid manifest is an incomplete backup. Retain the last
verified backup until a new backup has passed a restore drill. The manifest detects
accidental corruption; it is not a cryptographic signature against a malicious editor.
Protect it with the same access controls as the archive.

## Restore to a separate target first

Create a dedicated PostgreSQL 17 database and an empty artifact volume. Keep all target
API processes and workers stopped. Use the same application image/schema version as the
backup, then apply documented forward migrations only after verifying the restored data.
The backup excludes cluster roles, database passwords, runtime environment files, and
client OS keychain credentials. Provision the target database owner and runtime secrets
separately through your secret manager.

```sh
python3 scripts/backup_local.py restore \
  --postgres captain-restore-postgres-1 --user captain --database captain_team \
  --artifacts captain-restore_artifacts \
  --input /private/backups/captain-team-2026-11-07
```

The helper checks both file checksums and safe archive entries before changing the
target. It rejects a nonempty public schema or artifact volume by default, refuses
unexpected user schemas, and requires no active database clients. It never starts the
API. A failed restore must remain offline until repaired or discarded.

`--replace-existing` is an explicit destructive option for a dedicated restore target:
it drops and recreates the target's public schema and empties the artifact volume.
Prefer a fresh database and volume. Do not use this flag against a live deployment.
The helper does not ask interactively and does not automatically create a safety backup.

After restoring, start only the isolated target API and verify schema health, an existing
member's identity, private-record isolation, reader write denial, revoked-token denial,
and downloaded artifact SHA-256/length against metadata. Compare record revisions and
workspace cursor/epoch with the captured source. Only then plan any production cutover.
A health response alone is insufficient. Restore returns token and membership state
to the capture point. Reapply any revocations or rotations made after that point from
operator records before production access resumes; expiry still uses the current clock.

## Reproducible synthetic drill

```sh
CST_CHECK_IMAGE=captain-team-sync:token-check python3 scripts/check_backup_restore.py
```

The drill creates its own Docker network, two PostgreSQL 17 containers, two API
containers, and two artifact volumes. It uses production per-user token mode with
synthetic records and bytes. It verifies source data, captures the quiesced pair,
refuses a populated restore target without removing its sentinel table, restores into
the separate target, and checks identity, ACLs, token revocation, token hashes, and
artifact bytes. All owned resources are removed and inspected absent in `finally`.
The drill also checks corrupted archives, unsafe paths, unexpected user schemas,
writer restart after failure, and preservation of an already stopped writer.
No production data or existing containers are used. The temporary backup is deleted
when the drill exits.

## S3 is a separate recovery plan

This helper supports the local filesystem artifact adapter only. Do not point it at
an S3 deployment's local spool and treat that as an artifact backup. For S3, use a
separately designed and tested bucket-versioning/retention recovery plan tied to the
same quiesced database point. Include all referenced immutable blobs and staging
objects required by unfinished uploads. Database-only restore can leave metadata
pointing at unavailable objects. Cross-region copies, Object Lock, restore permissions,
and worker state recovery are not verified by this local-volume drill.
