# Lab Data Store and Usage Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Postgres+TimescaleDB store on the lab's primary node, fed only through a token-authenticated ingest API, with usage tracking (ActivityWatch sync shim per device) as its first tenant and a web viewer at `usage.lab`.

**Architecture:** `lab-data-store` owns the server (StatefulSet pinned by `role=primary-worker`), roles, nightly backups, the generic `ingest-api` (source registry by config, per-device bearer tokens, upsert writer) and the tenant conventions. `usage-tracker` owns the `usage` schema + migrations, the `usage-sync` shim (reads a local ActivityWatch server, ships settled events with original timestamps, idempotent upserts) and `usage-web` (FastAPI JSON API + Svelte frontend, read-only DB role, login through the MCP gateway's OAuth). Deploy manifests live in `lab-gitops` under `apps/lab-data-store` and `apps/usage-tracker`; images are built multi-arch on the primary node into the LAN registry (the repo's established path), with CI building (not pushing) on PRs.

**Tech Stack:** Python 3.12 + uv, FastAPI, psycopg 3 (pool), pydantic 2, PyYAML; `timescale/timescaledb:2.17.2-pg17`; Svelte 5 + Vite + TypeScript (SVG charts, no chart library); pytest, ruff; Kustomize/Flux; cert-manager ClusterIssuer `lab-ca` (already live).

**Spec:** `docs/superpowers/specs/2026-11-04-lab-data-store-and-usage-tracking-design.md` (canonical copy; the vault copy points here).

## Global Constraints

- Public, recruiter-safe repos: **no secrets, LAN IPs, hostnames, usernames or PII** in tree, commits, or history. Placeholders: `192.0.2.x`, `<primary-node>`, `user@host`. Real values only in `.env`/Secrets created out of band by scripts that never print them. `*.example` files for every out-of-band value.
- Never ask the user to paste tokens: helper scripts mint tokens and write them to `0600` files.
- Postgres pinned by label `role: primary-worker`, hostPath under `/data/lab/lab-data-store/`; backups on the other disk under `/srv/lab-data-store/backups`. Everything else floats on any node (multi-arch `linux/amd64,linux/arm64` images, pulled from `registry.lab/lab/<img>` so the air-gapped Pi can pull too).
- Devices never hold DB credentials; one revocable bearer token per device, bound to a device name and a source.
- Original timestamps always; `received_at` recorded separately. Ingest is idempotent (upsert on `(device, source_bucket, source_event_id, ts_start)`).
- `ingest.lab` / `usage.lab` get TLS from cert-manager (`cert-manager.io/cluster-issuer: lab-ca`), websecure entrypoint.
- Follow `lab-gitops` conventions: Kustomize folder per app in namespace `lab`, probes on every workload, `strategy: Recreate` for stateful, `enableServiceLinks: false` where env names could clash, pinned image tags = git short SHA.
- Nothing LAN-wide; nothing on the GPU PC (installs, Docker, scheduled tasks) until the hub says the user is not gaming; the LAN gateway Pi is never touched.
- Display timezone for the viewer: `America/Chicago` (env `USAGE_TZ`); stored timestamps are `timestamptz`.
- Event model (source `usage`): `device, ts_start, ts_end, kind (window|web|afk), app, title, url, domain, idle:boolean, source_bucket, source_event_id`.

## Review Focus

1. **Overlapping re-sends.** The shim re-ships the last hour each cycle because ActivityWatch extends the latest event; the server must update `ts_end`/`title` on conflict, never duplicate. Test in Task 4 (upsert) and Task 10 (overlap window).
2. **Token bound to a device.** A body whose `device` differs from the token's device, or whose `source` is not in the token's sources, must be rejected with 403 and nothing written. Test in Task 4.
3. **Malformed events inside a valid batch.** One bad row (missing `ts_start`, `ts_end < ts_start`, unknown `kind`) rejects the whole batch with 422 and the row index, so the shim does not advance its cursor past bad data. Test in Task 2/4.
4. **Offline for days.** The shim must survive the API being down for hours: no cursor advance without a 200, exponential backoff capped at 10 minutes, and no unbounded memory (events are re-read from ActivityWatch, not cached). Test in Task 11.
5. **Long events across hours.** A 3-hour game window must appear in each hour it spans, pro rata, not entirely in its start hour; "afk" events show as gaps, never as app usage. Test in Task 13.

---

## File structure

```
lab-data-store/
  README.md  LICENSE  .gitignore  .env.example
  docs/superpowers/{specs,plans}/…   docs/tenants.md   docs/backup-restore.md   docs/deploy.md
  ingest-api/
    pyproject.toml  uv.lock  Dockerfile  config/sources.example.yaml
    src/ingest_api/{__init__,config,sources,tokens,store_migrations,writer,ratelimit,app,admin_cli}.py
    src/ingest_api/store_migrations/0001_store.sql
    tests/{test_sources,test_tokens,test_writer,test_app,test_admin_cli}.py  tests/conftest.py
  scripts/{build-push.sh,make-secret.sh,mint-token.sh,restore-check.sh,pull-backup.ps1,register-pull-task.ps1}
  .github/workflows/ci.yml

usage-tracker/
  README.md  LICENSE  .gitignore
  migrations/0001_usage_events.sql  migrations/0002_usage_functions.sql
  sync/
    pyproject.toml  src/usage_sync/{__init__,config,aw,mapping,redact,state,shipper,cli,importer}.py
    tests/{test_mapping,test_redact,test_state,test_shipper,test_importer,test_cli}.py  tests/fixtures/*.json
    install/linux/{install-aw.sh,aw-server.service,aw-watcher-window.service,aw-watcher-afk.service,usage-sync.service,config.example.toml}
    install/windows/{install.ps1,config.example.toml}
    install/android/README.md
  web/
    pyproject.toml  Dockerfile  src/usage_web/{__init__,config,db,migrate,queries,api,auth,app,__main__}.py
    tests/{test_queries,test_api,test_auth,test_migrate}.py
    frontend/ (Vite + Svelte + TS)  src/{App.svelte,lib/{api.ts,filters.ts,format.ts},components/{Filters,HourlyChart,TopList,DailyTotals,DeviceCompare}.svelte}
  scripts/{build-push.sh,register-oauth-client.sh}
  .github/workflows/ci.yml

lab-gitops/apps/lab-data-store/{kustomization,postgres,backup,ingest-api}.yaml + sources.yaml
lab-gitops/apps/usage-tracker/{kustomization,usage-web}.yaml
```

---

## Phase 1 — Store (build order §9.1)

### Task 1: Repo scaffolds and spec hand-over

**Files:**
- Create: `lab-data-store/{README.md,LICENSE,.gitignore,.env.example}`, `lab-data-store/.github/workflows/ci.yml`
- Create: `usage-tracker/{README.md,LICENSE,.gitignore}`, `usage-tracker/.github/workflows/ci.yml`
- Modify: vault `Ideas/designs/2026-11-04-lab-data-store-and-usage-tracking-design.md` (add a line under the title: canonical copy lives in `JamilCPU/lab-data-store` `docs/superpowers/specs/`)

- [ ] **Step 1:** `gh repo create JamilCPU/lab-data-store --public --description "Centralized Postgres+TimescaleDB data store for a home lab: token-authenticated ingest API, per-source schemas, nightly backups" ` and the same for `usage-tracker` ("Cross-device app and website usage tracking: ActivityWatch sync client, Timescale schema and a filterable web viewer"). Init both locally on `main`, remote `origin`.
- [ ] **Step 2:** `.gitignore` in both: `.env`, `*.token`, `token`, `*.key`, `*.pem`, `.venv/`, `__pycache__/`, `node_modules/`, `dist/`, `*.db`, `state.json`, `.superpowers/`.
- [ ] **Step 3:** MIT LICENSE (copyright holder "Jamil Whitehead", year 2026) in both. README in both: one-paragraph purpose, architecture diagram from the spec §5 (ASCII), repo layout, "Quick start" placeholders that later tasks fill. No addresses or hostnames.
- [ ] **Step 4:** CI skeleton `ci.yml` (push main + pull_request): job per Python package (`uv sync --locked`, `uv run ruff check .`, `uv run pytest -q`), guarded with `if: hashFiles('<pkg>/pyproject.toml') != ''` so it passes while packages are empty.
- [ ] **Step 5:** Commit `chore: repo scaffold (README, LICENSE, CI)` in both; push; enable branch protection? No: self-merged PRs, keep it simple. Add the vault pointer line, commit nothing in the vault (not a git repo for us).

### Task 2: ingest-api — source registry and event validation (no DB)

**Files:**
- Create: `ingest-api/pyproject.toml` (name `ingest-api`, requires-python `>=3.12`, deps: `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]>=3.2`, `pydantic>=2`, `pyyaml`, `typer`; dev: `pytest`, `pytest-asyncio`, `httpx`, `ruff`), `src/ingest_api/sources.py`, `config/sources.example.yaml`
- Test: `ingest-api/tests/test_sources.py`

**Interfaces:**
- Produces: `load_sources(path: Path) -> dict[str, SourceSpec]`; `SourceSpec(name, schema, table, columns: dict[str, ColumnType], required: tuple[str,...], conflict_key: tuple[str,...], on_conflict: Literal["update","nothing"])`; `ColumnType = Literal["text","timestamptz","bool","bigint","int","double","jsonb"]`; `validate_events(spec: SourceSpec, events: list[dict]) -> list[dict]` raising `EventValidationError(index: int, field: str, message: str)`.

`config/sources.example.yaml` (this exact content is also the ConfigMap in Task 6):
```yaml
sources:
  usage:
    schema: usage
    table: events
    columns:
      device: text
      ts_start: timestamptz
      ts_end: timestamptz
      kind: text
      app: text
      title: text
      url: text
      domain: text
      idle: bool
      source_bucket: text
      source_event_id: bigint
    required: [device, ts_start, ts_end, kind, source_bucket, source_event_id]
    conflict_key: [device, source_bucket, source_event_id, ts_start]
    on_conflict: update
    enum:
      kind: [window, web, afk]
```

- [ ] **Step 1: Write failing tests** `test_load_example_sources_has_usage`, `test_validate_coerces_timestamps_to_aware_datetime` (ISO string with `Z` → `datetime` with tzinfo), `test_validate_rejects_missing_required` (missing `ts_end` at index 2 → `EventValidationError(index=2, field="ts_end")`), `test_validate_rejects_end_before_start`, `test_validate_rejects_unknown_kind` (enum), `test_validate_rejects_unknown_column` (extra key `foo` → error), `test_validate_naive_timestamp_rejected`.
- [ ] **Step 2:** Run `uv run pytest tests/test_sources.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `sources.py`: pydantic models for the YAML; `validate_events` converts each value by column type (timestamps must be timezone-aware), checks `required`, `enum`, and when both `ts_start` and `ts_end` are columns, `ts_end >= ts_start`.
- [ ] **Step 4:** Run tests → PASS. `uv run ruff check .` clean.
- [ ] **Step 5:** Commit `feat(ingest-api): source registry and event validation`.

### Task 3: ingest-api — store migrations, roles, device tokens

**Files:**
- Create: `src/ingest_api/store_migrations.py`, `src/ingest_api/store_migrations/0001_store.sql`, `src/ingest_api/tokens.py`, `src/ingest_api/config.py`, `tests/conftest.py`, `tests/test_tokens.py`, `tests/test_store_migrations.py`

**Interfaces:**
- Consumes: `SourceSpec` from Task 2.
- Produces: `Settings` (pydantic-settings style from env: `STORE_ADMIN_DSN`, `STORE_INGEST_DSN`, `SOURCES_FILE` default `/etc/ingest-api/sources.yaml`, `RATE_LIMIT_PER_MINUTE=120`, `MAX_BATCH=5000`, `PORT=8080`); `apply_store_migrations(admin_dsn: str) -> list[str]` (applied versions); `ensure_roles(admin_dsn: str, sources: dict[str, SourceSpec], passwords: Mapping[str,str]) -> None` (roles `ingest`, `<source>_owner`, `<source>_reader`; passwords from env `INGEST_PASSWORD`, `<SOURCE>_OWNER_PASSWORD`, `<SOURCE>_READER_PASSWORD`; creates `CREATE EXTENSION IF NOT EXISTS timescaledb`; `CREATE SCHEMA IF NOT EXISTS <schema> AUTHORIZATION <source>_owner`; `ALTER DEFAULT PRIVILEGES FOR ROLE <source>_owner IN SCHEMA <schema>` grant `SELECT, INSERT, UPDATE` to `ingest` and `SELECT` to `<source>_reader`; `GRANT USAGE ON SCHEMA` to both); `hash_token(token: str) -> str` (sha256 hex); `new_token() -> str` (`lds_` + 32 url-safe bytes); `create_token(conn, device: str, sources: list[str], note: str|None) -> tuple[str, int]` (plaintext once, id); `lookup_token(conn, token: str) -> TokenInfo | None` (`TokenInfo(id, device, sources: tuple[str,...])`, ignores revoked); `revoke_token(conn, id: int) -> bool`; `touch_token(conn, id: int) -> None` (sets `last_seen_at`, called at most once a minute).
- `0001_store.sql`: `CREATE SCHEMA IF NOT EXISTS store; CREATE TABLE store.schema_migrations(version text primary key, applied_at timestamptz default now()); CREATE TABLE store.device_tokens(id bigint generated always as identity primary key, device text not null, sources text[] not null, token_hash text not null unique, note text, created_at timestamptz not null default now(), last_seen_at timestamptz, revoked_at timestamptz);` plus `store.sources(name text primary key, schema_name text not null, registered_at timestamptz default now())` filled by `ensure_roles`.

Tests needing Postgres read `TEST_ADMIN_DSN`; `conftest.py` skips them when unset (`pytest.mark.integration`). CI (Task 5) provides `timescale/timescaledb:2.17.2-pg17` as a service. Locally on the primary node: `docker run -d --name lds-test -e POSTGRES_PASSWORD=test -p 54329:5432 timescale/timescaledb:2.17.2-pg17`.

- [ ] **Step 1: Write failing tests** `test_hash_token_is_sha256_hex`, `test_new_token_prefix_and_length` (starts `lds_`, ≥ 40 chars), integration: `test_apply_store_migrations_idempotent` (second call applies nothing), `test_ensure_roles_creates_schema_and_grants` (as `usage_reader` can `SELECT` from a table created by `usage_owner`; `ingest` can INSERT), `test_create_lookup_revoke_token` (lookup returns device/sources; after revoke returns None).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the four modules. Role passwords via `ALTER ROLE ... PASSWORD %s` after `CREATE ROLE IF NOT EXISTS` emulated with a `DO $$ ... $$` block. Use `psycopg.sql.Identifier` for every identifier.
- [ ] **Step 4:** Run with `TEST_ADMIN_DSN` → PASS.
- [ ] **Step 5:** Commit `feat(ingest-api): store schema, roles and device tokens`.

### Task 4: ingest-api — HTTP app, writer, rate limit, admin CLI

**Files:**
- Create: `src/ingest_api/writer.py`, `src/ingest_api/ratelimit.py`, `src/ingest_api/app.py`, `src/ingest_api/admin_cli.py`, `tests/test_writer.py`, `tests/test_app.py`, `tests/test_admin_cli.py`
- Modify: `pyproject.toml` (`[project.scripts] ingest-api = "ingest_api.app:main"`, `store-admin = "ingest_api.admin_cli:app"`)

**Interfaces:**
- Produces: `write_events(conn, spec: SourceSpec, events: list[dict]) -> WriteResult(inserted: int, updated: int)` using `INSERT … ON CONFLICT (<conflict_key>) DO UPDATE SET <non-key cols> = EXCLUDED.<col> WHERE (<row>) IS DISTINCT FROM (EXCLUDED…)` with `xmax = 0` to count inserts vs updates; `RateLimiter(per_minute: int).allow(key: str) -> bool` (token bucket, in-memory); FastAPI app factory `create_app(settings) -> FastAPI` with:
  - `POST /v1/ingest` body `{"source": str, "device": str, "events": [...]}` → `200 {"accepted": n, "inserted": i, "updated": u}`; `401` bad/absent bearer; `403` device/source mismatch; `413` > `MAX_BATCH`; `422` validation `{"index": i, "field": f, "message": m}`; `429` rate limited.
  - `GET /healthz` → `{"ok": true}`; `GET /readyz` → DB `SELECT 1` through the ingest pool, 503 otherwise.
  - Startup: `apply_store_migrations` + `ensure_roles` with the admin DSN, then open the ingest pool; `received_at` is a DB default on the tenant table, not sent by clients.
- `store-admin` (typer): `migrate`, `token create --device D --source S [--source S2] [--note N]` (prints only the plaintext token to stdout, everything else to stderr), `token list`, `token revoke ID`.

- [ ] **Step 1: Write failing tests** (integration, with a `usage.events` table created by the test from `usage-tracker/migrations/0001` text inlined as a fixture): `test_write_inserts_then_updates_on_conflict` (same key, longer `ts_end` → `updated == 1`, row count 1), `test_write_unchanged_row_counts_nothing`; app tests with `httpx.AsyncClient`: `test_ingest_requires_bearer` (401), `test_ingest_rejects_other_device` (403, table empty), `test_ingest_rejects_unknown_source` (403), `test_ingest_rejects_bad_row_with_index` (422 index 1), `test_ingest_accepts_batch_sets_received_at`, `test_ingest_rate_limited` (limit 2 → third call 429), `test_ingest_batch_too_large` (413), `test_readyz`; CLI: `test_token_create_prints_only_token` (stdout is one line starting `lds_`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Rate-limit key = token id. `touch_token` throttled per token id.
- [ ] **Step 4:** Run → PASS; ruff clean.
- [ ] **Step 5:** Commit `feat(ingest-api): ingest endpoint, upsert writer, rate limit, store-admin CLI`.

### Task 5: ingest-api — image, CI, build script

**Files:**
- Create: `ingest-api/Dockerfile` (multi-stage: `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` build → `python:3.12-slim` runtime, non-root uid 10001, `EXPOSE 8080`, `CMD ["ingest-api"]`), `scripts/build-push.sh`
- Modify: `.github/workflows/ci.yml` (postgres service `timescale/timescaledb:2.17.2-pg17` with `TEST_ADMIN_DSN`; `image-check` job `docker/build-push-action@v6 push: false` on PRs; on main push also push `ghcr.io/jamilcpu/ingest-api:{sha,latest}` with `packages: write`)

`scripts/build-push.sh`: run on the primary node; `TAG=$(git rev-parse --short HEAD)`; `docker buildx build --builder default --platform linux/amd64,linux/arm64 -t registry.lab:5000/lab/ingest-api:$TAG -t registry.lab:5000/lab/ingest-api:latest --push ingest-api`; prints the tag. Registry host from `REGISTRY` env (default `registry.lab:5000`) so no address is hard-coded beyond the lab name.

- [ ] **Step 1:** Write Dockerfile and script. Verify locally where Docker exists (primary node over SSH): `docker build ingest-api` succeeds and `docker run --rm <img> store-admin --help` prints usage.
- [ ] **Step 2:** Update CI; open PR `feat/ingest-api`, confirm green, merge (`gh pr merge --squash --delete-branch`).
- [ ] **Step 3:** On the primary node: clone the repo to `~/src/lab-data-store`, run `scripts/build-push.sh`; note the tag.

### Task 6: Deploy the store through lab-gitops

**Files (lab-gitops, branch `feat/lab-data-store` in a separate clone):**
- Create: `apps/lab-data-store/kustomization.yaml` (namespace `lab`, `configMapGenerator` `ingest-sources` from `sources.yaml`, `images:` newTag for `registry.lab/lab/ingest-api`), `apps/lab-data-store/sources.yaml` (= Task 2 example), `apps/lab-data-store/postgres.yaml`, `apps/lab-data-store/backup.yaml`, `apps/lab-data-store/ingest-api.yaml`
- Modify: `apps/kustomization.yaml` (add `lab-data-store`), `README.md` ("What runs here": Lab Data Store `ingest.lab`; Postgres internal)
- Create (lab-data-store): `scripts/make-secret.sh`, `docs/deploy.md`, `docs/tenants.md`

Manifests:
- `postgres.yaml`: StatefulSet `postgres` (1 replica, `nodeSelector: {role: primary-worker}`, image `timescale/timescaledb:2.17.2-pg17`, env `POSTGRES_DB=lab`, `POSTGRES_PASSWORD` from Secret `lab-data-store`, `PGDATA=/var/lib/postgresql/data/pgdata`, hostPath `/data/lab/lab-data-store/postgres` type `DirectoryOrCreate`, initContainer busybox `chown 70:70`, probes `pg_isready -U postgres`, requests `cpu 100m, memory 512Mi`, limits `memory 2Gi`, `args: ["-c","shared_buffers=512MB","-c","timescaledb.telemetry_level=off"]`); headless Service `postgres` :5432. Also a hostPath `/data/lab/lab-data-store/files` mounted at `/files` on ingest-api (the files area, reserved).
- `backup.yaml`: CronJob `postgres-backup` `0 3 * * *` (UTC), same image, `nodeSelector role: primary-worker`, hostPath `/srv/lab-data-store/backups` → `/backups`, hostPath files area read-only → `/files`, script: `pg_dump -Fc -h postgres -U postgres -d lab -f /backups/lab-$(date +%F-%H%M).dump && pg_dumpall -h postgres -U postgres --globals-only > /backups/globals-$(date +%F).sql && tar czf /backups/files-$(date +%F).tgz -C /files . ; find /backups -type f -mtime +14 -delete`, `PGPASSWORD` from the Secret, `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 3`.
- `ingest-api.yaml`: Deployment `ingest-api` (replicas 1, no nodeSelector, `enableServiceLinks: false`, runAsNonRoot 10001, env `STORE_ADMIN_DSN` = `postgresql://postgres:$(POSTGRES_PASSWORD)@postgres:5432/lab` built from Secret refs, `STORE_INGEST_DSN` likewise with `ingest`, `INGEST_PASSWORD`, `USAGE_OWNER_PASSWORD`, `USAGE_READER_PASSWORD` from Secret, `SOURCES_FILE=/etc/ingest-api/sources.yaml`; ConfigMap volume; probes `/healthz`, `/readyz`; resources `50m/128Mi`, limit `512Mi`); Service `ingest-api` :8080; Ingress `ingest.lab` with `cert-manager.io/cluster-issuer: lab-ca`, `traefik.ingress.kubernetes.io/router.entrypoints: websecure`, `tls: secretName: ingest-lab-tls`.
- `scripts/make-secret.sh`: `kubectl -n lab create secret generic lab-data-store --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 24) --from-literal=INGEST_PASSWORD=… --from-literal=USAGE_OWNER_PASSWORD=… --from-literal=USAGE_READER_PASSWORD=…` (idempotent: skip if exists; `--dry-run=client -o yaml | kubectl apply`); never echoes.
- `docs/tenants.md`: one schema per source, roles `<source>_owner` (migrations) / `<source>_reader` (viewers, MCP) / shared `ingest`; register in `sources.yaml`; add `<SOURCE>_OWNER_PASSWORD`/`<SOURCE>_READER_PASSWORD` to the Secret and rerun `make-secret.sh`; tenant repo owns `migrations/` applied as owner with a `<schema>.schema_migrations` table; `received_at timestamptz default now()` on every ingested table; files go under `/files/<source>/`; backups cover everything automatically; read access from outside the cluster only via MCP servers behind the gateway.

- [ ] **Step 1:** On the primary node: `sudo mkdir -p /data/lab/lab-data-store/{postgres,files} /srv/lab-data-store/backups`; run `make-secret.sh`.
- [ ] **Step 2:** Write manifests; `kubectl kustomize apps/lab-data-store` renders; `kubeconform` optional. PR to lab-gitops, merge, `flux reconcile kustomization apps --with-source` on the node.
- [ ] **Step 3:** Verify: `kubectl -n lab get pods` shows `postgres-0` and `ingest-api` Running; `kubectl -n lab get certificate` shows `ingest-lab-tls` Ready; from the node `curl --cacert ~/lab-tls/lab-ca.pem https://ingest.lab/healthz` → `{"ok":true}` and `/readyz` 200; `kubectl -n lab exec postgres-0 -- psql -U postgres -d lab -c '\dn'` lists `store`, `usage`.
- [ ] **Step 4:** Commit docs in lab-data-store `docs: deploy runbook and tenant conventions`.

### Task 7: usage schema migrations (first apply by runbook)

**Files (usage-tracker):**
- Create: `migrations/0001_usage_events.sql`, `migrations/0002_usage_functions.sql` (empty placeholder with header; filled in Task 13), `migrations/README.md`

`0001_usage_events.sql` (run as `usage_owner`, idempotent):
```sql
CREATE TABLE IF NOT EXISTS usage.schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS usage.events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  device text NOT NULL, ts_start timestamptz NOT NULL, ts_end timestamptz NOT NULL,
  kind text NOT NULL CHECK (kind IN ('window','web','afk')),
  app text, title text, url text, domain text, idle boolean NOT NULL DEFAULT false,
  source_bucket text NOT NULL, source_event_id bigint NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  CHECK (ts_end >= ts_start),
  PRIMARY KEY (ts_start, id),
  UNIQUE (device, source_bucket, source_event_id, ts_start)
);
SELECT create_hypertable('usage.events', 'ts_start', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS events_device_ts ON usage.events (device, ts_start DESC);
CREATE INDEX IF NOT EXISTS events_app ON usage.events (app, ts_start DESC) WHERE kind = 'window';
CREATE INDEX IF NOT EXISTS events_domain ON usage.events (domain, ts_start DESC) WHERE kind = 'web';
INSERT INTO usage.schema_migrations VALUES ('0001') ON CONFLICT DO NOTHING;
```
Retention: raw forever (spec §12 default); no compression yet (upserts land in recent chunks; revisit after a year).

- [ ] **Step 1:** Write the files. Apply on the cluster: `kubectl -n lab exec -i postgres-0 -- env PGPASSWORD="$(kubectl -n lab get secret lab-data-store -o jsonpath='{.data.USAGE_OWNER_PASSWORD}' | base64 -d)" psql -U usage_owner -d lab -v ON_ERROR_STOP=1 < migrations/0001_usage_events.sql` (a helper `scripts/apply-migrations.sh` wraps this; the Task 17 initContainer replaces it).
- [ ] **Step 2:** Verify `\d usage.events` shows the hypertable; commit `feat: usage schema (events hypertable)`.

### Task 8: Backup verified by a real restore

**Files (lab-data-store):** `scripts/restore-check.sh`, `docs/backup-restore.md`, `scripts/pull-backup.ps1`, `scripts/register-pull-task.ps1`

- [ ] **Step 1:** Mint a throwaway token and ingest one real-shaped batch (3 events) with `curl` so the dump is non-empty; `kubectl -n lab create job --from=cronjob/postgres-backup backup-manual-1`; wait; list `/srv/lab-data-store/backups`.
- [ ] **Step 2:** `restore-check.sh <dump>`: `docker run -d --name lds-restore -e POSTGRES_PASSWORD=x timescale/timescaledb:2.17.2-pg17`; `psql -f globals-*.sql`; `createdb lab`; `pg_restore -d lab <dump>`; `psql -c "select count(*) from usage.events"` equals the live count; prints PASS/FAIL; removes the container. Run it on the node → PASS.
- [ ] **Step 3:** `pull-backup.ps1`: `scp "$env:LAB_DATA_STORE_SSH:/srv/lab-data-store/backups/*" F:\Backups\lab-data-store\` keeping the newest 4 of each kind; `register-pull-task.ps1` registers a weekly Sunday 04:00 user task running it (**not run until the hub clears the GPU PC**). Run `pull-backup.ps1` once by hand now (a file copy, no install) and run `restore-check.sh` logic against the pulled copy on the node? No: the pulled copy is verified by size + `pg_restore --list` on the node over the same file; document both.
- [ ] **Step 4:** `docs/backup-restore.md`: schedule, where dumps live, restore steps (same script), weekly pull. Commit `feat: backup restore check and weekly pull scripts`. **Milestone 1 report to the hub.**

## Phase 2 — Client on the Linux desktop (§9.2)

### Task 9: usage-sync — ActivityWatch client, event mapping, redaction

**Files (usage-tracker/sync):** `pyproject.toml` (name `usage-sync`, requires-python `>=3.10`, **no runtime deps**; dev `pytest`, `ruff`), `src/usage_sync/{aw,mapping,redact}.py`, `tests/fixtures/{buckets.json,window_events.json,afk_events.json,web_events.json,android_events.json}`, `tests/{test_mapping,test_redact}.py`

**Interfaces:**
- `aw.AwClient(base_url: str, timeout: float = 10).buckets() -> dict[str, dict]`, `.events(bucket_id: str, start: datetime, end: datetime, limit: int = 5000) -> list[dict]` (stdlib `urllib`, `GET /api/0/buckets`, `GET /api/0/buckets/{id}/events?start=&end=&limit=`).
- `mapping.bucket_kind(bucket: dict) -> Literal["window","afk","web"] | None` (by `type`: `currentwindow`→window, `afkstatus`→afk, `web.tab.current`→web; android bucket type `currentwindow` too).
- `mapping.to_event(bucket_id: str, kind: str, aw_event: dict) -> dict | None` → `{ts_start, ts_end (= timestamp + duration), kind, app, title, url, domain, idle, source_bucket, source_event_id}`; afk: `idle = data.status == "afk"`, app `None`; web: `domain = urlsplit(url).hostname`, `app = data.get("app") or None`, skip `incognito` true; events with `duration == 0` are kept (ts_end = ts_start).
- `redact.Redactor(apps: list[str], domains: list[str]).apply(event: dict) -> dict` (new dict; case-insensitive substring on `app` or `domain` → `title=None, url=None`); `DEFAULT_APPS = ["1password","bitwarden","keepass","lastpass"]`, `DEFAULT_DOMAINS = ["bank","chase.com","wellsfargo.com","capitalone.com","paypal.com","venmo.com"]`.

- [ ] **Step 1: Failing tests** `test_window_event_maps_app_title_and_end`, `test_afk_event_sets_idle`, `test_web_event_domain_and_incognito_skipped`, `test_android_bucket_is_window_kind`, `test_unknown_bucket_type_is_none`, `test_redactor_blanks_title_for_matching_app`, `test_redactor_keeps_domain_blank_url`, `test_redactor_returns_new_dict`.
- [ ] **Step 2:** FAIL → implement → PASS → commit `feat(sync): ActivityWatch client, event mapping, redaction`.

### Task 10: usage-sync — cursor state and shipper with overlap

**Files:** `src/usage_sync/{state,shipper,config}.py`, `tests/{test_state,test_shipper}.py`

**Interfaces:**
- `config.Config` (from TOML via `tomllib`): `ingest_url: str`, `device: str`, `aw_url = "http://127.0.0.1:5600"`, `interval_seconds = 60`, `batch_size = 1000`, `settle_seconds = 120`, `overlap_seconds = 3600`, `redact_apps`, `redact_domains`, `token_file` (default beside config: `token`); `Config.load(path: Path) -> Config`; default config dir: `$XDG_CONFIG_HOME/usage-sync` or `~/.config/usage-sync` (Linux/Android), `%LOCALAPPDATA%\usage-sync` (Windows).
- `state.State(path).cursor(bucket_id) -> datetime | None`, `.advance(bucket_id, ts: datetime)` (write-through JSON, atomic rename).
- `shipper.Shipper(cfg, aw: AwClient, state: State, post: Callable[[str, bytes, dict], tuple[int, bytes]], now: Callable[[], datetime])` with `.sync_once() -> SyncReport(buckets: int, shipped: int, skipped_unsettled: int, errors: list[str])`: per bucket: `start = cursor - overlap or epoch`, `end = now`; map + redact; sort by `ts_start`; POST batches of `batch_size` to `{ingest_url}/v1/ingest` with `Authorization: Bearer <token>`; on 200 advance cursor to the max `ts_end` among events with `ts_end <= now - settle_seconds`; on non-200 stop this bucket, keep cursor, record error. `.run(stop: threading.Event)`: loop every `interval_seconds`; after consecutive failures sleep `min(interval * 2**n, 600)`.

- [ ] **Step 1: Failing tests** with a fake `post` and fake `AwClient`: `test_first_sync_ships_all_and_advances_to_settled_end`, `test_overlap_reships_recent_events` (cursor at T, events in `[T-overlap, T]` are re-posted), `test_failed_post_keeps_cursor_and_reports_error`, `test_events_sorted_by_start_in_batch`, `test_batches_respect_batch_size`, `test_backoff_caps_at_600_seconds` (pure function `backoff(n, base) -> int`), `test_state_round_trip_and_atomic_write`, `test_config_defaults_and_token_file_location`.
- [ ] **Step 2:** FAIL → implement → PASS → commit `feat(sync): cursor state and shipper with overlap and backoff`.

### Task 11: usage-sync — CLI and Linux install, end to end on the Linux desktop

**Files:** `src/usage_sync/cli.py` (`usage-sync run|once|status|init`), `install/linux/{install-aw.sh,aw-server.service,aw-watcher-window.service,aw-watcher-afk.service,usage-sync.service,config.example.toml}`, `tests/test_cli.py`; `lab-data-store/scripts/mint-token.sh`

- `usage-sync init --device D --ingest-url U` writes `config.toml` (no token); `status` prints cursors per bucket and the last sync result (no token); `once` runs `sync_once` and exits non-zero on errors; `run` loops. The HTTP `post` is `urllib`-based, verifying TLS against `ssl.create_default_context()` plus `cafile` from config `ca_file` (the lab CA exported by `lab-gitops/bootstrap/lab-tls/export-ca.sh`).
- `install-aw.sh`: downloads `activitywatch-linux-x86_64.AppImage` v0.14.1 from the GitHub release to `~/.local/opt/activitywatch`, `--appimage-extract`, installs user units (aw-server, aw-watcher-window with `Environment=DISPLAY=:1` detected from `loginctl`/`$DISPLAY`, aw-watcher-afk) and `usage-sync.service` (`ExecStart=%h/.local/bin/usage-sync run`, `Restart=always`), `systemctl --user enable --now`, `loginctl enable-linger`.
- `mint-token.sh <device> <source...>`: `kubectl -n lab exec deploy/ingest-api -- store-admin token create --device "$1" …` to stdout (caller redirects to the token file, `umask 077`).

- [ ] **Step 1:** CLI tests: `test_init_writes_config_without_token`, `test_status_lists_cursors`. FAIL → implement → PASS.
- [ ] **Step 2:** Install on the Linux desktop over SSH: `pip install --user` the sync package from the repo clone (`uv tool install` if available, else `python3 -m pip install --user .`), run `install-aw.sh`, `mint-token.sh <desktop> usage > ~/.config/usage-sync/token` (run on the node itself, never displayed), `usage-sync init --device <desktop> --ingest-url https://ingest.lab`, `ca_file` = the node's `~/lab-tls/lab-ca.pem` path. Confirm `usage-sync once` → shipped > 0; `psql -c "select kind,count(*),min(ts_start),max(ts_end) from usage.events where device='<desktop>' group by 1"`.
- [ ] **Step 3:** Forced offline test: `kubectl -n lab scale deploy/ingest-api --replicas=0`; wait 10 minutes (shim logs show backoff, cursor unchanged); scale back to 1; within 2 minutes `select min(ts_start), max(ts_end), min(received_at) from usage.events where device='<desktop>' and ts_start > <outage start>` shows events inside the outage window with `received_at` after it. Record the query output in the vault note and the hub report.
- [ ] **Step 4:** Commit `feat(sync): CLI and Linux install`; PR + merge. **Milestone 2 report.**

## Phase 3 — usage-web (§9.3)

### Task 12: usage-web — package, migrate runner, OAuth client

**Files (usage-tracker/web):** `pyproject.toml` (name `usage-web`; deps `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]`, `pydantic-settings`, `httpx`, `itsdangerous`, `jinja2`; scripts `usage-web = "usage_web.__main__:main"`), `src/usage_web/{config,db,migrate,auth,__main__}.py`, `tests/{test_migrate,test_auth}.py`

**Interfaces:**
- `Settings`: `USAGE_READER_DSN`, `USAGE_OWNER_DSN` (migrate only), `USAGE_TZ="America/Chicago"`, `SESSION_SECRET`, `PUBLIC_URL="https://usage.lab"`, `OAUTH_ISSUER="https://gateway.lab"`, `OAUTH_CLIENT_ID`, `OAUTH_SCOPE` (default: first of the issuer's `scopes_supported`), `OAUTH_CA_FILE` (lab CA), `AUTH_MODE="oauth"|"none"` (`none` only for tests), `PORT=8080`.
- `migrate.apply(owner_dsn: str, migrations_dir: Path) -> list[str]` (files `NNNN_*.sql` in order, skips versions present in `usage.schema_migrations`, each in a transaction).
- `auth`: PKCE authorization-code client: `GET /auth/login` → redirect to `{issuer}/authorize` with `code_challenge`, `state` in a signed cookie; `GET /auth/callback` → exchanges at the token endpoint (discovered from `/.well-known/oauth-authorization-server`), on success sets session cookie (`itsdangerous.TimestampSigner`, 30 days, `Secure; HttpOnly; SameSite=Lax`); `GET /auth/logout`; dependency `require_session` → 401 JSON for `/api/*`, redirect to `/auth/login` for pages. The client is public (gateway DCR issues `token_endpoint_auth_method=none`), registered once by `scripts/register-oauth-client.sh` (POST `{issuer}/register` with `client_name: usage-web`, `redirect_uris: ["https://usage.lab/auth/callback"]`, writes Secret `usage-web` key `OAUTH_CLIENT_ID`; never prints).

- [ ] **Step 1: Failing tests** `test_migrate_applies_in_order_and_skips_applied` (integration), `test_login_redirects_with_pkce_and_state`, `test_callback_rejects_bad_state`, `test_callback_sets_session_cookie` (fake issuer via `httpx.MockTransport`), `test_api_without_session_is_401`.
- [ ] **Step 2:** FAIL → implement → PASS → commit `feat(web): package, migrate runner, gateway OAuth login`.

### Task 13: usage-web — SQL functions and query layer

**Files:** `usage-tracker/migrations/0002_usage_functions.sql`, `src/usage_web/queries.py`, `tests/test_queries.py`

`0002_usage_functions.sql` (owner): `usage.hourly(devices text[], t_from timestamptz, t_to timestamptz, tz text)` returns `(bucket timestamptz, device text, app text, seconds double precision)`: for `kind='window' and not idle` events overlapping `[t_from, t_to)`, split by `generate_series` over `time_bucket('1 hour', ts_start, tz)` … `ts_end`, seconds = overlap of the event with each hour; idle time is subtracted by excluding intervals covered by `kind='afk' and idle` events of the same device (`NOT EXISTS` overlap test on the hour slice is acceptable for v1; exact subtraction is deferred). `usage.top_apps(devices, t_from, t_to, hour_from int, hour_to int, tz, lim int)` and `usage.top_domains(...)` (sum of overlap seconds, filtered by `extract(hour from ts_start at time zone tz) between hour_from and hour_to`). `usage.daily(devices, t_from, t_to, tz)` → `(day date, device, seconds)`.

**Interfaces:** `queries.Filters(devices: list[str], t_from: datetime, t_to: datetime, hour_from: int = 0, hour_to: int = 23, app: str|None, domain: str|None, tz: str)`; `devices(conn) -> list[DeviceRow(device, first_seen, last_seen, events)]`; `hourly(conn, f) -> list[HourlyRow]`; `top_apps(conn, f, limit=20)`; `top_domains(conn, f, limit=20)`; `daily(conn, f)`; `events(conn, f, cursor: str|None, limit=200) -> Page(rows, next_cursor)`.

- [ ] **Step 1: Failing integration tests** seeding `usage.events`: `test_hourly_splits_long_event_across_hours` (a 10:30–13:15 event → 1800 s in 10h, 3600 in 11h, 3600 in 12h, 900 in 13h, in `America/Chicago`), `test_hourly_excludes_afk_and_idle`, `test_top_apps_hour_filter`, `test_daily_totals_per_device`, `test_events_pagination_cursor`.
- [ ] **Step 2:** FAIL → write SQL + queries → PASS → commit `feat(web): hourly/top/daily SQL functions and query layer`.

### Task 14: usage-web — JSON API

**Files:** `src/usage_web/{api,app}.py`, `tests/test_api.py`

Routes (all under `require_session`): `GET /api/devices`; `GET /api/hourly?device=a&device=b&from=ISO&to=ISO&hour_from=0&hour_to=23&app=&domain=`; `GET /api/top/apps`, `GET /api/top/domains`; `GET /api/daily`; `GET /api/events?…&cursor=`; `GET /api/meta` → `{tz, devices}`; `GET /healthz`, `GET /readyz`. Defaults: `from`/`to` = today in `USAGE_TZ`. Invalid ranges → 422. Static frontend served at `/` (`StaticFiles` from `usage_web/static`, built in Task 16) with an index fallback.

- [ ] **Step 1: Failing tests** `test_hourly_defaults_to_today`, `test_hour_range_validated`, `test_devices_lists_seeded`, `test_healthz_public_readyz_checks_db`.
- [ ] **Step 2:** FAIL → implement → PASS → commit `feat(web): JSON API`.

### Task 15: usage-web — frontend

**Files:** `web/frontend/{package.json,vite.config.ts,tsconfig.json,index.html,src/main.ts,src/App.svelte,src/lib/{api,filters,format}.ts,src/components/{Filters,HourlyChart,TopList,DailyTotals,DeviceCompare}.svelte}`, `web/frontend/src/lib/{filters,format}.test.ts` (vitest)

- `filters.ts`: `Filters` type ⇄ URL query (`toQuery(f) -> string`, `fromQuery(q) -> Filters`), presets (today, yesterday, 7d, 30d), hour range; `format.ts`: `fmtDuration(seconds) -> "2h 15m"`, `hourLabel(n)`.
- UI: dark theme (CSS variables, `prefers-color-scheme`), single column ≤ 640px. Filters bar (device multi-select chips, date range + presets, hour range dual slider, app/domain text). HourlyChart: SVG stacked bars per hour, top 8 apps + "other", gaps where no usage; DailyTotals: bars per day per device; TopList: apps and domains with durations; DeviceCompare: per-device totals side by side for the range. Built output copied into `src/usage_web/static/` by the Dockerfile (not committed).

- [ ] **Step 1:** vitest for `toQuery/fromQuery` round trip, `fmtDuration` ("0m", "1h 05m"). FAIL → implement → PASS.
- [ ] **Step 2:** `npm run build` succeeds; run API with `AUTH_MODE=none` against the lab DB over an SSH tunnel and check the page in the browser. Commit `feat(web): Svelte frontend with device/date/hour filters`.

### Task 16: usage-web — image, CI, deploy, verify at usage.lab

**Files:** `web/Dockerfile` (stage 1 `node:22-alpine` builds frontend; stage 2 uv build; runtime `python:3.12-slim` uid 10001, `CMD ["usage-web","serve"]`), `usage-tracker/scripts/build-push.sh` (image `registry.lab/lab/usage-web`), CI jobs (`sync`, `web`, `frontend` vitest, image-check), lab-gitops `apps/usage-tracker/{kustomization.yaml,usage-web.yaml}`, `apps/kustomization.yaml`, README row.

`usage-web.yaml`: Deployment `usage-web` (replicas 1, Recreate, `enableServiceLinks: false`, initContainer `migrate` same image `usage-web migrate` with `USAGE_OWNER_DSN` built from Secret `lab-data-store` and `MIGRATIONS_DIR=/app/migrations` (migrations copied into the image), main container env `USAGE_READER_DSN`, `SESSION_SECRET` + `OAUTH_CLIENT_ID` from Secret `usage-web`, `OAUTH_CA_FILE=/etc/lab-ca/lab-ca.pem` from ConfigMap `lab-ca` (exists in `lab`), probes, resources); Service :8080; Ingress `usage.lab` TLS `lab-ca`, `usage-lab-tls`.

- [ ] **Step 1:** `register-oauth-client.sh` on the node (uses the lab CA); `kubectl -n lab create secret generic usage-web --from-literal=SESSION_SECRET=$(openssl rand -hex 32) --from-literal=OAUTH_CLIENT_ID=…` (script does both).
- [ ] **Step 2:** PRs: usage-tracker (`feat/usage-web`) green + merged; build-push on the node; lab-gitops PR merged; reconcile.
- [ ] **Step 3:** Verify in the browser (built-in browser, with `--resolve`-style host mapping unavailable: use the node's DNS or the DNS already pointing at lab-dns): `https://usage.lab` → gateway approval page → back to the viewer; filter by device `<desktop>`, today, hours 9–17; the hourly chart and top apps match `select app, sum(extract(epoch from ts_end-ts_start)) from usage.events where device='<desktop>' and kind='window' and ts_start >= date_trunc('day', now() at time zone 'America/Chicago') group by 1 order by 2 desc limit 5`. Works at 375 px wide. **Milestone 3 report.**

## Phase 4 — GPU PC client (§9.4) — only after the hub confirms no gaming

### Task 17: usage-sync — Windows install and activity-logger import/compare

**Files:** `sync/install/windows/{install.ps1,config.example.toml}`, `src/usage_sync/importer.py`, `tests/test_importer.py`, CLI subcommands `import-activity-logger <sqlite> --device <gpu-pc>` and `compare-activity-logger <sqlite> --date YYYY-MM-DD`

- `install.ps1` (no admin): downloads the ActivityWatch Windows zip (v0.13.2, last Windows build in the 0.13 line; verify the latest release asset at run time) into `%LOCALAPPDATA%\Programs\activitywatch`, registers a logon scheduled task `ActivityWatch` running `aw-qt.exe` (tray icon only), `pip install --user` the sync package, registers task `usage-sync` running `pythonw -m usage_sync run`, writes config; the token is minted over SSH: `ssh "$env:LAB_DATA_STORE_SSH" 'kubectl -n lab exec deploy/ingest-api -- store-admin token create --device <gpu-pc> --source usage' | Set-Content -NoNewline "$env:LOCALAPPDATA\usage-sync\token"` (never displayed). Browser extension: the user installs "ActivityWatch Web Watcher" in Brave themselves (one click; documented).
- `importer.rows_to_events(rows: Iterable[sqlite3.Row], device: str) -> Iterator[dict]`: `sessions` → `kind='window'` (`app = process_name or app_name`, `title = window_title`), `is_idle=1` → `kind='afk', idle=true, app=None`; `source_bucket='activity-logger'`, `source_event_id=id`; rows with `end_time NULL` use `start + duration` or are skipped; ships through the normal shipper in batches of 1000 (no DB access).
- `compare`: for one day, per-app seconds from the sqlite file vs from `GET /api/top/apps` is not available to the client (auth) → compare against `usage-sync`'s own view: sum durations of events it would ship for that day from the local aw-server vs sqlite; print a table and the ratio; pass if top-5 apps agree within 15 %.

- [ ] **Step 1:** Failing tests `test_rows_to_events_window_and_idle`, `test_rows_skip_open_sessions`, `test_import_batches_sorted`. FAIL → implement → PASS → commit.
- [ ] **Step 2:** Ask the hub; on clearance run `install.ps1`, confirm events arrive under `device='<gpu-pc>'`, run the importer (≈32k rows → `select count(*) from usage.events where source_bucket='activity-logger'`), register `register-pull-task.ps1` from Task 8.
- [ ] **Step 3:** After a day: `compare-activity-logger`, record the table; stop activity-logger (`activity-logger stop`, remove its autostart task), note in README that it is retired. **Milestone 4 report.**

## Phase 5 — Android phone (§9.5)

### Task 18: Android phone foreground-app tracking via pull

The old logger pulled the phone's ActivityWatch (aw-android) server over the network; keep that model: a second `usage-sync` instance on the Linux desktop with `aw_url = http://<phone>:5600`, `device = <phone>`, its own token, user unit `usage-sync@phone.service` (template unit with `%i` config dir). `install/android/README.md`: install aw-android (F-Droid/Play), enable "Server on all interfaces"/check the app's port, give the phone a DHCP reservation (the user does this in the router), no browser capture on Android (spec §12 default).

- [ ] **Step 1:** Ask the user (through the hub) whether aw-android is still installed and reachable; if yes, mint the phone's token on the node, configure, verify events under `device='<phone>'` and visible at `usage.lab`. If the phone is unreachable, document the steps and mark the task deferred. **Milestone 5 report.**

## Phase 6 — Hand-over (§9.6)

### Task 19: Conventions to the private-data-layer session, vault, final checks

- [ ] **Step 1:** Write `side-session-notes/lab-data-store-conventions.md` (shared notes folder): DSN shape (`postgresql://<role>@postgres.lab.svc:5432/lab`), Secret `lab-data-store` keys, how to add a source (`docs/tenants.md`), ingest envelope and token minting, the files area, backup coverage, read access via `<source>_reader`. Send the PDL session one message pointing at it.
- [ ] **Step 2:** Vault `Infrastructure/What Is Running.md`: rows for Postgres+Timescale (`postgres` internal, pinned to the primary node, data `/data/lab/lab-data-store`, backups `/srv/lab-data-store/backups` + weekly pull to `F:\Backups\lab-data-store`), `ingest.lab`, `usage.lab`, the native `usage-sync` + ActivityWatch user units on the Linux desktop, the GPU PC client, the phone; note the old the GPU PC `lab-db` Docker Postgres as retired (Docker Desktop is not running; repo archived in the audit). Update the idea notes `Cross-Device App Usage Tracking` with "shipped" + links.
- [ ] **Step 3:** Both repos: README complete (install per device, deploy, backup, tenant conventions), all PRs merged, CI green, `gh repo view` descriptions/topics set (`postgresql`, `timescaledb`, `kubernetes`, `flux`, `activitywatch`, `fastapi`, `svelte`). Final hub report.
