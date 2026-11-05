# Tenant conventions

A tenant is a data source that keeps its data in the lab data store: usage events, bank transactions, email, and so on. This repo owns the server, the roles, the ingest API and the backups. The tenant owns its schema and migrations. This page is the contract between the two.

## The model

- **One schema per source.** The schema name equals the source name (`usage`, `bank`, `email`, ...). Tenants never create objects outside their own schema.
- **Three kinds of role**:

  | Role | Used by | Can |
  | --- | --- | --- |
  | `<source>_owner` | the tenant's migrations | create and alter anything in `<source>`; owns the schema |
  | `<source>_reader` | viewers and read-only MCP servers | `SELECT` in `<source>`, nothing else |
  | `ingest` (shared) | the ingest API only | `SELECT, INSERT, UPDATE` on the one mapped table of each source that declares an ingest mapping (not on other tables in the schema, such as `schema_migrations`); read and manage device tokens |

- **Sources are registered by configuration.** `sources.yaml` (a ConfigMap in lab-gitops, example at `ingest-api/config/sources.example.yaml`) lists each source. An entry with only `schema` is roles-only: the schema and roles exist, but the ingest API refuses writes for it. An entry with `table`, `columns`, `required`, `conflict_key` and so on also enables `POST /v1/ingest` for that source.
- **Reads from outside the cluster** go through read-only MCP servers behind the gateway, never directly to Postgres. Inside the cluster, a service connects as `<source>_reader` (or `<source>_owner` for migrations).

## Rules for tenant schemas

1. **Migrations live in the tenant's repo**, in a `migrations/` directory, and run as `<source>_owner`.
2. **Track them in `<schema>.schema_migrations`** (`version text primary key`, `applied_at timestamptz not null default now()`). The tenant's migration runner creates it if missing.
3. **Every ingested table has `received_at timestamptz not null default now()`.** The ingest API does not set it; the database does.
4. **Ingested tables should be idempotent**: declare a `conflict_key` that matches a unique index so a client retrying a batch never duplicates rows.
5. **Files go under `files/<source>/` in the files area** (see below), never in the database as blobs.
6. **Backups are automatic.** The nightly job dumps the whole database, all roles and the whole files area. Nothing to configure per tenant.

## Worked example: adding a `netlog` tenant

### 1. Register the source

Add an entry to `sources.yaml`. A roles-only source needs just the schema (like the existing `bank`, `email` and `sms`); this one also accepts ingest:

```yaml
sources:
  netlog:
    schema: netlog
    table: flows
    columns:
      device: text
      seen_at: timestamptz
      bytes_total: bigint
      proto: text
      host: text
      flow_id: text
    required: [device, seen_at, bytes_total, proto, flow_id]
    conflict_key: [flow_id]
    on_conflict: update
```

Registering the source makes the `bootstrap` initContainer of the ingest-api pod create the `netlog` schema, the `netlog_owner` and `netlog_reader` roles and their grants the next time the pod starts.

### 2. Give the roles passwords

The bootstrap reads each role's password from `<SOURCE>_OWNER_PASSWORD` and `<SOURCE>_READER_PASSWORD` in its environment, and fails (keeping the pod in `Init`) if one is missing. Add both keys to the Secret without printing anything:

```bash
scripts/add-source-passwords.sh netlog
```

Then add the two matching `secretKeyRef` entries to the `bootstrap` initContainer of the `ingest-api` Deployment in lab-gitops (never to the HTTP container) and add the new entry to the `ingest-sources` ConfigMap there. Merge. The Deployment change rolls the pod; a ConfigMap-only change does not (the file is mounted with `subPath`), so run `kubectl -n lab rollout restart deploy/ingest-api` after Flux applies it.

### 3. Create the table as the owner

The tenant's repo carries `migrations/0001_flows.sql`:

```sql
CREATE TABLE netlog.flows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device text NOT NULL,
    seen_at timestamptz NOT NULL,
    bytes_total bigint NOT NULL,
    proto text NOT NULL,
    host text,
    flow_id text NOT NULL UNIQUE,
    received_at timestamptz NOT NULL DEFAULT now()
);
```

Its runner connects as `netlog_owner`, creates `netlog.schema_migrations` if it is missing, and applies each file once, recording the version. The table must exist before the first ingest, and the `ingest` grant on it is applied by the next bootstrap: after the tenant's first migration run, `kubectl -n lab rollout restart deploy/ingest-api`.

In-cluster connection strings (the password comes from the Secret, never from a file):

```
postgresql://netlog_owner@postgres.lab.svc.cluster.local:5432/lab
postgresql://netlog_reader@postgres.lab.svc.cluster.local:5432/lab
```

`netlog_reader` gets `SELECT` on tables the owner creates later through default privileges, so no extra `GRANT` is needed. `ingest` is deliberately narrower: it gets `SELECT, INSERT, UPDATE` only on the mapped table (`netlog.flows`), granted by the bootstrap once the table exists. If a table turns out unreadable, check that it was created by `netlog_owner`.

### 4. Files

Put files a tenant needs to keep (captures, exports) in the files area on the primary node, `/data/lab/lab-data-store/files/netlog/`. The ingest API does not use it. A tenant pod that needs it mounts the hostPath `/data/lab/lab-data-store/files/netlog` itself and pins to the node labelled `role: primary-worker`. The subdirectory is not auto-created: create it on the node first (`sudo mkdir -p` and `chown` to the pod's uid). Because the area is on the primary node, the nightly job archives it with the database.

### 5. Reading the data

Viewers and MCP servers connect as `netlog_reader`. For example, from a pod in the namespace:

```bash
psql "postgresql://netlog_reader@postgres.lab.svc.cluster.local:5432/lab" -c 'SELECT count(*) FROM netlog.flows'
```

### 6. Sending data from a device

Mint a token bound to the device and the source (see [deploy.md](deploy.md)), then post the generic envelope:

```json
{
  "source": "netlog",
  "device": "router-agent",
  "events": [
    {
      "seen_at": "2026-11-01T09:30:00Z",
      "bytes_total": 4096,
      "proto": "tcp",
      "host": "example.org",
      "flow_id": "flow-000123"
    }
  ]
}
```

```bash
curl -sS --cacert lab-ca.pem -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d @batch.json https://ingest.lab/v1/ingest
```

The `device` in the envelope must match the device the token was minted for, and `source` must be one of the token's sources. A column named `device` is filled in by the API from the token. Batches are limited to `MAX_BATCH` events (default 5000) and requests per token to `RATE_LIMIT_PER_MINUTE` (default 120).

### Upgrading a roles-only source to ingest

`bank`, `email` and `sms` are registered roles-only: their schemas and roles exist and ingest is refused. To let one accept data: (1) have the tenant create its table as `<source>_owner`; (2) add `table`, `columns`, `required` and `conflict_key` to the entry in the `ingest-sources` ConfigMap in lab-gitops and merge; (3) run `kubectl -n lab rollout restart deploy/ingest-api` so the pod re-reads the file and the bootstrap grants `ingest` on the table; (4) mint a device token for the source (see [deploy.md](deploy.md)). No new passwords are needed.

## Checklist for a new tenant

- [ ] Entry in `sources.yaml` (ConfigMap in lab-gitops)
- [ ] `<SOURCE>_OWNER_PASSWORD` / `<SOURCE>_READER_PASSWORD` in the Secret and in the `bootstrap` initContainer env
- [ ] `migrations/` in the tenant repo, applied as `<source>_owner`, tracked in `<schema>.schema_migrations`
- [ ] `received_at timestamptz NOT NULL DEFAULT now()` on every ingested table
- [ ] Files (if any) under `/data/lab/lab-data-store/files/<source>/`, created on the node and mounted by the tenant's own pod
- [ ] Readers use `<source>_reader`; external reads go through an MCP server behind the gateway
