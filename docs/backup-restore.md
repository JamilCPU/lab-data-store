# Backup and restore

## What is backed up, and when

The CronJob `postgres-backup` runs nightly at 03:00 UTC on the primary node and writes to `/srv/lab-data-store/backups` (a different disk from the data):

| File | Content |
| --- | --- |
| `lab-<date>-<hhmm>.dump` | `pg_dump -Fc` of the `lab` database (all schemas) |
| `globals-<date>.sql` | `pg_dumpall --globals-only`: roles and their attributes |
| `files-<date>.tgz` | Tarball of the files area |

The directory is owned by uid 70 with the group of the user that does the off-node pull, mode `2750` (setgid, so new files inherit the group); the job runs with `umask 027`, so files are `640`. One-time setup on the node: `sudo chgrp <pull-user-gid> /srv/lab-data-store/backups && sudo chmod 2750 /srv/lab-data-store/backups`, and for files that already exist `sudo chgrp <pull-user-gid> /srv/lab-data-store/backups/* && sudo chmod 640 /srv/lab-data-store/backups/*`.

The node keeps 14 days; older files are deleted by the job itself. Run one on demand with `kubectl -n lab create job --from=cronjob/postgres-backup backup-manual-1` (delete the job afterwards).

## Off-node copy

Once a week the GPU PC pulls the directory with `scripts/pull-backup.ps1` into `F:\Backups\lab-data-store` (override with `LAB_DATA_STORE_BACKUP_DIR`), keeps the 14 newest files of each kind (mirrors the node's 14-day retention; the job writes one set per night) and writes `SHA256SUMS.txt`. It reads `LAB_DATA_STORE_SSH` (`user@host` of the primary node) from the environment and needs key-based ssh. `scripts/register-pull-task.ps1` registers the scheduled task `LabDataStore-BackupPull` (Sundays 04:00, current user); it only registers, it does not run the task. The task runs only while that user is logged on; `StartWhenAvailable` makes a missed Sunday run catch up at the next logon. To check a pulled file, compare `Get-FileHash` with `sha256sum` on the node, and copy it to the node to run `pg_restore --list` against it.

## Verify a backup (restore check)

`scripts/restore-check.sh <dump> [globals]` runs on the primary node. It starts a throwaway `timescale/timescaledb:2.17.2-pg17` container, restores the dump into it and compares `usage.events` and `store.device_tokens` row counts with the live database (read from `postgres-0`, or pass `LIVE_EVENTS` / `LIVE_TOKENS`). It prints a table and `PASS` or `FAIL`, exits 0 or 1, and always removes the container. The live counts are taken at check time, so an ingest between the dump and the check makes the live count larger and gives a spurious `FAIL`; re-run the dump and the check, or pass `LIVE_*` values taken at dump time.

```bash
d=/srv/lab-data-store/backups
scripts/restore-check.sh $d/lab-<date>-<hhmm>.dump $d/globals-<date>.sql
```

The steps it performs, in order (the real restore differs only in step 5):

1. Wait for the server to accept connections (the image restarts once during init).
2. `psql -f globals-<date>.sql`. Errors for roles that already exist or superuser attributes are expected on a fresh server and ignored; this is the only step where failure is tolerated.
3. `createdb lab`.
4. `CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();`
5. `pg_restore -d lab --no-owner --no-privileges <dump>`. The check only counts rows, so ownership and grants do not matter here; the real restore below keeps them.
6. `SELECT timescaledb_post_restore();`
7. Compare counts.

### TimescaleDB requirement

`usage.events` is a hypertable. A restore must be wrapped in `timescaledb_pre_restore()` / `timescaledb_post_restore()`, with the extension created first; without them the internal catalog and background workers can be left inconsistent. The `timescale/timescaledb` image already preloads the extension in `template1`, so step 4 prints `extension "timescaledb" already exists, skipping`; that is harmless. `pg_restore` exited 0 in the verification below, but exit 1 with "errors ignored on restore" is accepted by the script as long as the counts match; any higher code fails.

## Restore for real into the cluster

This replaces the live data. Do it only after a restore check of the same dump passed. Status: the dump/restore sequence is rehearsed via `restore-check.sh` (which differs in step 5 only); the full ownership-preserving restore has not been executed against the cluster.

```bash
kubectl -n lab scale deploy/ingest-api --replicas=0     # Flux reverts this at its next reconcile; suspend the apps Kustomization first (flux suspend kustomization apps) or be quick
kubectl -n lab cp /srv/lab-data-store/backups/lab-<date>-<hhmm>.dump postgres-0:/tmp/lab.dump
kubectl -n lab cp /srv/lab-data-store/backups/globals-<date>.sql postgres-0:/tmp/globals.sql
kubectl -n lab exec postgres-0 -- psql -U postgres -f /tmp/globals.sql            # existing-role errors are fine
kubectl -n lab exec postgres-0 -- psql -U postgres -c 'DROP DATABASE IF EXISTS lab WITH (FORCE)' -c 'CREATE DATABASE lab'
kubectl -n lab exec postgres-0 -- psql -U postgres -d lab -c 'CREATE EXTENSION IF NOT EXISTS timescaledb' -c 'SELECT timescaledb_pre_restore()'
kubectl -n lab exec postgres-0 -- pg_restore -U postgres -d lab /tmp/lab.dump
kubectl -n lab exec postgres-0 -- psql -U postgres -d lab -c 'SELECT timescaledb_post_restore()'
kubectl -n lab exec postgres-0 -- rm /tmp/lab.dump /tmp/globals.sql
kubectl -n lab scale deploy/ingest-api --replicas=1
kubectl -n lab rollout restart deploy/ingest-api        # re-runs the bootstrap initContainer; then: flux resume kustomization apps
```

The globals file is applied first so the roles exist; the restore then runs **without** `--no-owner --no-privileges`, so every object keeps its owner (`<source>_owner`, which owns its schema and tables) and every grant (`<source>_reader`, `ingest`) is restored as dumped. The restore is wrapped in `timescaledb_pre_restore()` / `timescaledb_post_restore()` as above. The final `rollout restart` re-runs the `bootstrap` initContainer, which re-applies migrations, role passwords and grants for every registered source (idempotent), and starts the API against the restored database; do not skip it. Restore the files area with `tar xzf files-<date>.tgz -C /data/lab/lab-data-store/files` on the node. Afterwards check `/readyz` and the row counts. `restore-check.sh` keeps `--no-owner --no-privileges` on purpose: it restores into a throwaway server and only counts rows, so ownership is irrelevant and the flag avoids depending on the roles existing.

## Verification record

| | |
| --- | --- |
| Date | 2026-11-05 |
| Dump | `lab-2026-11-05-0300.dump` (nightly job) with the newest `globals-<date>.sql` |
| Where | Throwaway container on `<primary-node>`, image `timescale/timescaledb:2.17.2-pg17` |
| `usage.events` | live 18, restored 18 |
| `store.device_tokens` | live 3, restored 3 |
| Result | PASS, `pg_restore` exit code 0 |
| Earlier run | 2026-11-04, `lab-2026-11-04-1913.dump`: events 4/4, tokens 3/3, PASS. A pulled copy had the same SHA-256 as on the node and `pg_restore --list` showed 26 table-data entries |

The seed rows (device `restore-check`, source bucket `restore-check`) were ingested with a throwaway token, revoked straight after. A `FAIL` was also provoked on purpose (`LIVE_EVENTS=99`) to confirm the script reports it.
