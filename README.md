# lab-data-store

A centralized home for personal data in a home lab: one Postgres + TimescaleDB instance on a small Kubernetes cluster, fronted by a token-authenticated ingest API. Each data source (usage events, and later others) gets its own schema; apps and read-only MCP servers query it, and nothing personal lives in scattered per-app SQLite files.

## Architecture

```
 GPU PC ────────────┐                                  ┌─ usage web viewer (any node)
 Linux desktop ─────┤  usage client ──HTTPS──► ingest-api ─┤
 (primary node)     │  (buffer, ship)          (any node)  ├─ future: read-only MCP servers
 Android phone ─────┘                              │       │
                                                   ▼       │
                                    Postgres + TimescaleDB ◄┘
                             (pinned to the primary node, hostPath data)
                     schemas: usage | bank | sms | email | homestudio | ...
                     nightly pg_dump to a second disk; weekly off-node pull
```

- **Store**: Postgres with the TimescaleDB extension, run as a StatefulSet on the cluster's primary node (pinned by node label) with data on a hostPath volume. It is the only stateful component; everything else can float between nodes.
- **Ingest API**: the only entry point for writes. Clients send batches in a generic envelope (`source`, `device`, `events[]`) with a per-device bearer token. Sources are registered by configuration, so adding a tenant does not require code changes in this repo.
- **Backups**: a nightly `pg_dump` to a second disk on the primary node, plus a weekly pull to another machine so a copy always lives off-node. Restores are proven with `scripts/restore-check.sh`; see [docs/backup-restore.md](docs/backup-restore.md).
- **Tenant conventions**: one schema per source, with an `owner` role (runs migrations, owned by the tenant's repo) and a `reader` role (read-only, used by viewers and MCP servers). This repo owns the server, roles, backup and shared conventions; tenants own their schema and migrations.

## What exists today

- `usage`: ingest-mapped. Device clients post window/web/AFK events through `POST /v1/ingest`.
- `bank`, `email`, `sms`: roles-only tenants, owned by another project. Their schemas and roles exist; ingest is refused for them.

Deployment is GitOps: Flux applies the manifests in the `lab-gitops` repo (`apps/lab-data-store/`). This repo builds the image and holds the runbooks.

## Repo layout

| Path | Contents |
| --- | --- |
| `ingest-api/` | FastAPI ingest service (bearer-token auth, source registry, batch validation) and the `store-admin` CLI (`bootstrap`, `token ...`) |
| `ingest-api/config/` | Example source registry (`sources.example.yaml`) |
| `scripts/` | Operational helpers (image build, Secret creation, token minting, restore check, off-node backup pull); see `scripts/README.md` |
| `docs/deploy.md` | Runbook: node directories, Secret, Flux deploy, verification, tokens |
| `docs/tenants.md` | Contract for data sources: schemas, roles, registration, worked example |
| `docs/backup-restore.md` | Nightly backups, off-node pull, restore check, real restore, verification record |
| `docs/superpowers/specs/` | Design specs (canonical copy of the design lives here) |
| `docs/superpowers/plans/` | Implementation plans |
| `.github/workflows/` | CI (lint and tests for the Python package) |

## Quick start

Ingest API (`ingest-api/`):

```bash
(cd ingest-api && uv run pytest -q)   # integration tests need TEST_ADMIN_DSN (Postgres+TimescaleDB admin DSN); skipped without it
docker build ingest-api               # local image build
scripts/build-push.sh                 # lab build: multi-arch, pushes to the lab registry (run on the primary node)
```

Deploy: see [docs/deploy.md](docs/deploy.md) (node directories, Secret, Flux, verification). Adding a data source: [docs/tenants.md](docs/tenants.md).

### Configuration

Environment variables (see `.env.example`; real values are never committed). In the cluster they come from the Secret `lab-data-store` and the Deployment env.

| Variable | Used by | Default | Meaning |
| --- | --- | --- | --- |
| `STORE_INGEST_DSN` | API, `store-admin token` | required (API) | Connection string for the `ingest` role |
| `STORE_ADMIN_DSN` | `store-admin bootstrap`, optional elsewhere | unset | Superuser DSN. Set: the API bootstraps on start (dev/test) and token commands use it. In the cluster only the `bootstrap` initContainer has it |
| `SOURCES_FILE` | API, `store-admin` | `/etc/ingest-api/sources.yaml` | Source registry (YAML) |
| `RATE_LIMIT_PER_MINUTE` | API | `120` | Requests per token per minute |
| `MAX_BATCH` | API | `5000` | Maximum events per request |
| `PORT` | API | `8080` | Listen port |
| `INGEST_PASSWORD` | bootstrap | required | Password set on the `ingest` role |
| `<SOURCE>_OWNER_PASSWORD`, `<SOURCE>_READER_PASSWORD` | bootstrap | required per source | Passwords for each source's `owner` and `reader` roles (`USAGE`, `BANK`, `EMAIL`, `SMS`) |

Docs: [deploy](docs/deploy.md), [tenants](docs/tenants.md), [backup and restore](docs/backup-restore.md).

## License

MIT
