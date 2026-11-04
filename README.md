# lab-data-store

A centralized home for personal data in a home lab: one Postgres + TimescaleDB instance on a small Kubernetes cluster, fronted by a token-authenticated ingest API. Each data source (usage events, and later others) gets its own schema; apps and read-only MCP servers query it, and nothing personal lives in scattered per-app SQLite files.

## Architecture

```
 GPU PC ───────────┐                                 ┌─ usage web viewer (any node)
 Linux desktop ────┤ usage client ──HTTPS──► ingest-api ─┤
 (primary node)    │ (buffer, ship)   (any node)         ├─ future: more read-only MCP servers
 Android phone ────┘                         │           │
                                             ▼           │
                                 Postgres + TimescaleDB ◄┘
                          (PINNED to the primary node, hostPath data)
                   schemas: usage | bank | sms | email | homestudio | ...
                   nightly pg_dump to a second disk; weekly off-node pull
```

- **Store**: Postgres with the TimescaleDB extension, run as a StatefulSet on the cluster's primary node (pinned by node label) with data on a hostPath volume. It is the only stateful component; everything else can float between nodes.
- **Ingest API**: the only entry point for writes. Clients send batches in a generic envelope (`source`, `device`, `events[]`) with a per-device bearer token. Sources are registered by configuration, so adding a tenant does not require code changes in this repo.
- **Backups**: a nightly `pg_dump` to a second disk on the primary node, plus a weekly pull to another machine so a copy always lives off-node.
- **Tenant conventions**: one schema per source, with an `owner` role (runs migrations, owned by the tenant's repo) and a `reader` role (read-only, used by viewers and MCP servers). This repo owns the server, roles, backup and shared conventions; tenants own their schema and migrations.

## Repo layout

| Path | Contents |
| --- | --- |
| `ingest-api/` | FastAPI ingest service (bearer-token auth, source registry, batch validation) |
| `scripts/` | Operational helpers (backup, restore verification, token management) |
| `docs/` | Documentation |
| `docs/superpowers/specs/` | Design specs (canonical copy of the design lives here) |
| `docs/superpowers/plans/` | Implementation plans |
| `.github/workflows/` | CI (lint and tests for each Python package once present) |

## Quick start

Ingest API (`ingest-api/`):

```bash
(cd ingest-api && uv run pytest -q)   # integration tests need TEST_ADMIN_DSN (Postgres+TimescaleDB admin DSN); skipped without it
docker build ingest-api               # local image build
scripts/build-push.sh                 # lab build: multi-arch, pushes to the lab registry (run on the primary node)
```

### Configuration

_Filled in by later tasks._ See `.env.example` for the variables involved; real values are never committed.

## License

MIT
