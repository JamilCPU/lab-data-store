# Tenant conventions

A tenant is a data source that keeps its data in the lab data store: usage events, bank transactions, email, and so on. This repo owns the server, the roles, the ingest API and the backups. The tenant owns its schema and migrations. This page is the contract between the two.

## The model

- **One schema per source.** The schema name equals the source name (`usage`, `bank`, `email`, ...). Tenants never create objects outside their own schema.
- **Three kinds of role**:

  | Role | Used by | Can |
  | --- | --- | --- |
  | `<source>_owner` | the tenant's migrations | create and alter anything in `<source>`; owns the schema |
  | `<source>_reader` | viewers and read-only MCP servers | `SELECT` in `<source>`, nothing else |
  | `ingest` (shared) | the ingest API only | write to the tables of sources that declare an ingest mapping; read device tokens |

- **Sources are registered by configuration.** `sources.yaml` (a ConfigMap in lab-gitops, example at `ingest-api/config/sources.example.yaml`) lists each source. An entry with only `schema` is roles-only: the schema and roles exist, but the ingest API refuses writes for it. An entry with `table`, `columns`, `required`, `conflict_key` and so on also enables `POST /v1/ingest` for that source.
- **Reads from outside the cluster** go through read-only MCP servers behind the gateway, never directly to Postgres. Inside the cluster, a service connects as `<source>_reader` (or `<source>_owner` for migrations).

## Rules for tenant schemas

1. **Migrations live in the tenant's repo**, in a `migrations/` directory, and run as `<source>_owner`.
2. **Track them in `<schema>.schema_migrations`** (`version text primary key`, `applied_at timestamptz not null default now()`). The tenant's migration runner creates it if missing.
3. **Every ingested table has `received_at timestamptz not null default now()`.** The ingest API does not set it; the database does.
4. **Ingested tables should be idempotent**: declare a `conflict_key` that matches a unique index so a client retrying a batch never duplicates rows.
5. **Files go under `/files/<source>/`** (see below), never in the database as blobs.
6. **Backups are automatic.** The nightly job dumps the whole database, all roles and the whole files area. Nothing to configure per tenant.

## Worked example: adding a `bank` tenant

### 1. Register the source

Add an entry to `sources.yaml`. A roles-only source needs just the schema; this one also accepts ingest:

```yaml
sources:
  bank:
    schema: bank
    table: transactions
    columns:
      device: text
      booked_at: timestamptz
      amount_cents: bigint
      currency: text
      description: text
      external_id: text
    required: [device, booked_at, amount_cents, currency, external_id]
    conflict_key: [external_id]
    on_conflict: update
```

Registering the source makes the ingest API create the `bank` schema, the `bank_owner` and `bank_reader` roles and their grants the next time it starts.

### 2. Give the roles passwords

The API reads each role's password from `<SOURCE>_OWNER_PASSWORD` and `<SOURCE>_READER_PASSWORD` in its environment, and refuses to start if one is missing. Add both keys to the Secret without printing anything:

```bash
scripts/add-source-passwords.sh bank
```

Then add the two matching `secretKeyRef` entries to the `ingest-api` Deployment in lab-gitops and add the new entry to the `ingest-sources` ConfigMap there. Merge; Flux rolls the pod.

### 3. Create the table as the owner

The tenant's repo carries `migrations/0001_transactions.sql`:

```sql
CREATE TABLE bank.transactions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device text NOT NULL,
    booked_at timestamptz NOT NULL,
    amount_cents bigint NOT NULL,
    currency text NOT NULL,
    description text,
    external_id text NOT NULL UNIQUE,
    received_at timestamptz NOT NULL DEFAULT now()
);
```

Its runner connects as `bank_owner`, creates `bank.schema_migrations` if it is missing, and applies each file once, recording the version. The table must exist before the first ingest.

In-cluster connection strings (the password comes from the Secret, never from a file):

```
postgresql://bank_owner@postgres.lab.svc.cluster.local:5432/lab
postgresql://bank_reader@postgres.lab.svc.cluster.local:5432/lab
```

Grants for `bank_reader` and `ingest` are meant to cover tables the owner creates later through default privileges, so no extra `GRANT` should be needed. If a table turns out unreadable, check that it was created by `bank_owner`.

### 4. Files

Put files a tenant needs to keep (statements, attachments) under `/files/bank/`. The ingest API pod mounts the files area read-write at `/files`; other pods that need it mount the same hostPath. Because the area is on the primary node, the nightly job archives it with the database.

### 5. Reading the data

Viewers and MCP servers connect as `bank_reader`. For example, from a pod in the namespace:

```bash
psql "postgresql://bank_reader@postgres.lab.svc.cluster.local:5432/lab" -c 'SELECT count(*) FROM bank.transactions'
```

### 6. Sending data from a device

Mint a token bound to the device and the source (see [deploy.md](deploy.md)), then post the generic envelope:

```json
{
  "source": "bank",
  "device": "statement-importer",
  "events": [
    {
      "booked_at": "2026-11-01T09:30:00Z",
      "amount_cents": -1250,
      "currency": "EUR",
      "description": "Coffee",
      "external_id": "tx-000123"
    }
  ]
}
```

```bash
curl -sS --cacert lab-ca.pem -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d @batch.json https://ingest.lab/v1/ingest
```

The `device` in the envelope must match the device the token was minted for, and `source` must be one of the token's sources. A column named `device` is filled in by the API from the token. Batches are limited to `MAX_BATCH` events (default 5000) and requests per token to `RATE_LIMIT_PER_MINUTE` (default 120).

## Checklist for a new tenant

- [ ] Entry in `sources.yaml` (ConfigMap in lab-gitops)
- [ ] `<SOURCE>_OWNER_PASSWORD` / `<SOURCE>_READER_PASSWORD` in the Secret and in the Deployment env
- [ ] `migrations/` in the tenant repo, applied as `<source>_owner`, tracked in `<schema>.schema_migrations`
- [ ] `received_at timestamptz NOT NULL DEFAULT now()` on every ingested table
- [ ] Files under `/files/<source>/`
- [ ] Readers use `<source>_reader`; external reads go through an MCP server behind the gateway
