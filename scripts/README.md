# scripts

| Script | Where it runs | What it does |
| --- | --- | --- |
| `build-push.sh` | Primary node, from the repo root | Builds the `ingest-api` image for `linux/amd64` and `linux/arm64` with buildx and pushes it to the lab registry (`REGISTRY`, default `registry.lab:5000`, a lab-internal name). Refuses a dirty tree unless `ALLOW_DIRTY=1`; prints `TAG=<tag>`. |
| `make-secret.sh [namespace]` | Machine with `kubectl` access | Creates the Secret `lab-data-store` (default namespace `lab`) with a random password for Postgres, the `ingest` role and each source's owner and reader roles. Does nothing if the Secret exists. Never prints values. |
| `add-source-passwords.sh <source> [namespace]` | Machine with `kubectl` access | Patches `<SOURCE>_OWNER_PASSWORD` and `<SOURCE>_READER_PASSWORD` into the existing Secret when a tenant is added. Skips keys that exist. Never prints values. |
| `restore-check.sh <dump> [globals]` | Primary node (needs docker) | Restores a dump into a throwaway TimescaleDB container using the pre/post-restore sequence and compares `usage.events` and `store.device_tokens` counts with the live database. Prints `PASS`/`FAIL`, exits 0/1, removes the container. |
| `pull-backup.ps1` | GPU PC (PowerShell 5.1) | Copies the node's backups with `scp` to `F:\Backups\lab-data-store` (needs `LAB_DATA_STORE_SSH=user@host`), keeps the newest 14 per kind, writes `SHA256SUMS.txt`. |
| `register-pull-task.ps1` | GPU PC | Registers the scheduled task `LabDataStore-BackupPull` (Sundays 04:00, current user). Registers only; does not run it. |
| `mint-token.sh <device> <source...>` | Machine with `kubectl` access | Creates an ingest token for a device scoped to the given sources (`store-admin token create` in the `ingest-api` container; namespace `lab`, override with `NAMESPACE`). Prints only the token on stdout, so redirect it to a file under `umask 077`. |
