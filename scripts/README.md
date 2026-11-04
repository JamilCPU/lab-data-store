# scripts

| Script | Where it runs | What it does |
| --- | --- | --- |
| `build-push.sh` | Primary node, from the repo root | Builds the `ingest-api` image for `linux/amd64` and `linux/arm64` with buildx and pushes it to the lab registry (`REGISTRY`, default `registry.lab:5000`, a lab-internal name). Refuses a dirty tree unless `ALLOW_DIRTY=1`; prints `TAG=<tag>`. |
| `make-secret.sh [namespace]` | Machine with `kubectl` access | Creates the Secret `lab-data-store` (default namespace `lab`) with a random password for Postgres, the `ingest` role and each source's owner and reader roles. Does nothing if the Secret exists. Never prints values. |
| `add-source-passwords.sh <source> [namespace]` | Machine with `kubectl` access | Patches `<SOURCE>_OWNER_PASSWORD` and `<SOURCE>_READER_PASSWORD` into the existing Secret when a tenant is added. Skips keys that exist. Never prints values. |
