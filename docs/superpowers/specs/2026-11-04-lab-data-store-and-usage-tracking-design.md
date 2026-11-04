---
type: design
name: Lab Data Store and Usage Tracking — Design
status: approved
created: "2026-11-04"
updated: "2026-11-04"
tags: [design, datastore, postgres, usage-tracking, k3s]
---

# Lab Data Store and Usage Tracking — Design

Brainstormed with Jamil 2026-11-04 in the idea hub. **Approved by Jamil 2026-11-04** ("Go ahead start building"). Build session: `lab-data-store`. Two pieces, built together because the second is the first real tenant of the first.

## 1. Intent

1. **Lab Data Store**: one centralized home on the primary node for *all* of Jamil's personal data — not one app's database but the database. Usage events, the network log (later), bank statements, SMS, email cache, Home Studio history, whatever comes next. Each source gets its own area; apps and the assistant's MCP servers read from it; nothing personal lives in scattered per-app SQLite files anymore.
2. **Usage Tracking**: know what Jamil is doing on his devices — which apps, which websites, when — across the GPU PC, the primary node and the phone, with a web app to explore it by device, by hour, by day.

Jamil's words: "I want the the primary node datastore to be like a centralized home for all of my data, not just for a single app." "I want to know what apps I'm using, what websites I'm going to etc. Generalized information about what I'm doing on my devices for the time being." "Code should be like a usage tracking client that periodically sends data back to the datastore." "There should be a usage tracking web app where I can see all my data with filters, by device, by hours, etc."

## 2. Goals, non-goals, anti-goals

**Goals**
- One database server on the primary node that every lab app can write to and read from over the network.
- Usage data from every device Jamil actually uses, with the *original* timestamps even when it arrives late.
- A web app that answers "what was I doing on X between these hours" in a couple of clicks.
- Everything in public, recruiter-safe GitHub repos.

**Non-goals (for now)**
- Keystrokes, screenshots, file contents, or anything deeper than app + window title + URL. Jamil: "a little in-depth, but we can leave it simple for now."
- Network request logging (separate idea, [[Home Network Request Log]]; it becomes a later tenant of this store).
- Migrating existing apps' data (AAF, Home Studio) on day one. They move when touched.

**Anti-goals (failure even if it works)**
- Devices talking straight to Postgres with a shared password. Every writer goes through an ingest API with its own token.
- Data that only lives on the primary node with no backup.
- Any secret, IP, hostname or personal data in the repos.
- Anything disruptive on the GPU PC while Jamil is gaming (install the client when he says so; it must never steal focus or fight fullscreen games).

## 3. Vocabulary

- **Store**: the Postgres server on the primary node plus its file area.
- **Source**: a kind of data (usage, bank, sms…). One Postgres schema per source.
- **Tenant**: an app that owns a source (usage-tracker owns `usage`).
- **Ingest API**: the only way data gets in from outside the cluster.
- **Client**: a small background program on a device (not an AI agent) that records and ships events.

## 4. Why Postgres (decided with Jamil)

SQLite is one file, one writer, no network — that's why data is scattered today. Postgres is the general-purpose database server: many writers over the network, every language speaks it, extensions for what we need (TimescaleDB for events-over-time, pgvector later for the assistant's semantic search). ClickHouse is better for billions of request-log rows and may be added as a second engine for the network log only, later. MinIO/plain folders hold *files* (statement PDFs, exports) beside it.

## 5. Architecture

```
 GPU PC ─────┐                                     ┌─ usage-web (k3s, any node)  usage.lab
 desktop ────┤ usage client ──HTTPS──► ingest-api ─┤
 phone ──────┘ (buffer, ship)   (k3s, any node)    ├─ future: netlog-mcp, bank-mcp, …
                                      │            │
                                      ▼            │
                               Postgres + Timescale ◄┘  (k3s, PINNED to the primary node, local PV on the primary node's disk)
                               schemas: usage | bank | sms | email | homestudio | …
                               + files/ area (the primary node disk)  + nightly pg_dump → second disk / the GPU PC F:\Backups
```

- **Postgres** runs in k3s as a StatefulSet with `nodeSelector: role=primary-worker` and a local PersistentVolume on the primary node's big disk. It is the one stateful thing; everything else floats.
- **ingest-api** (stateless, any node): accepts batches from clients, validates against the source's schema, writes to Postgres. Per-client bearer token (one per device), rate-limited, `/healthz`. Reachable as `ingest.lab` inside the LAN only.
- **usage-web** (stateless, any node): the viewer. Reads Postgres directly (read-only DB role). `usage.lab`.
- Future tenants add a schema + migrations in their own repo and register with ingest-api by config; the store repo owns only the server, roles, backup and shared conventions.
- Access from outside the cluster: through the existing MCP gateway (read-only MCP servers per source) — not by exposing Postgres.
- **TLS (hub's call):** `ingest.lab` and `usage.lab` use cert-manager with the lab-internal CA that the cluster infra provides (the migration session is landing cert-manager + CA + a CA-install helper for the GPU PC/the phone in `lab-gitops`; the private-data-layer design assumed the same). This project does not install its own; if it isn't there when step 1 lands, the ingest API runs plain HTTP on the LAN for the few days until it is — tokens are per device and revocable, so the window is tolerable.
- **Backup (hub's call):** nightly `pg_dump` + files to the primary node's other disk (`/`, not the k3s NVMe), and the GPU PC *pulls* a weekly copy over SSH into `F:\Backups\lab-data-store\` via a 4 AM scheduled task — no inbound service on the GPU PC, nothing while gaming. That satisfies "not only on the primary node". Restore is verified once from the pulled copy.
- **Existing Lab DB on the GPU PC** (`F:\Dev\lab-db`, Postgres in Docker, research_agent for AI News): builder checks whether it still runs. Dead → note it retired. Alive → it becomes a tenant schema here in a later step, with the the air-gapped Pi research agent and AI News repointed; not part of steps 1–6.
- **SMS ingest:** ingest-api ships with a generic envelope (`source, device, events[]`) and per-source registration from day one. `sms-store` keeps its own `sms.lab` ingress as the private-data-layer design says; reconciling it onto ingest-api is a step-6 conversation, not a blocker.

## 6. The usage client

**Adopt, don't rewrite** (hub's call, builder verifies): ActivityWatch's watchers already capture exactly "app + window title" (aw-watcher-window), "away/idle" (aw-watcher-afk), "website" (aw-watcher-web browser extension for Chrome/Brave/Firefox) and the phone (aw-android, foreground app). They are open source, buffer locally in a per-device aw-server, and keep original timestamps. Our client is then a **sync shim**: a small service that every N minutes reads new events from the local aw-server API and POSTs them to ingest-api as a timestamp-sorted batch, remembering the last synced range per bucket. If a watcher proves unreliable on Windows 11 or Android, the builder may replace that one watcher with our own; the sync contract stays.

- Devices day one: the GPU PC (Windows 11), the primary node (Ubuntu with GNOME, mostly used over SSH), the Android phone (aw-android + its browser extension where possible). Pis: not tracked (no desktop use).
- **the primary node (hub's call):** only the primary node's graphical session is tracked, the same way as any desktop; SSH/terminal sessions are not usage events. Step 2 proves the pipeline on the primary node's GNOME session (log in on an X11 session if the Wayland window watcher is a fight); the primary node simply shows little usage, which is true.
- **activity-logger history (hub's call):** the ~7 months in `%USERPROFILE%\.activity-loggerctivity.db` (5-second app/idle samples since 2026-04-01) are imported once, converted to intervals under device `the GPU PC`, so the viewer isn't empty for 2026. No URLs exist in that data; that's fine.
- Event model (per source `usage`): `device, ts_start, ts_end, kind (window|web|afk), app, title, url, domain, idle:boolean`. Original timestamps always; `received_at` recorded separately.
- Local buffer: stays on the device until the ingest API acknowledges; retry with backoff; offline for a week is fine.
- Privacy: incognito/private windows are not captured by the browser extension; the builder adds an allow-list of apps/domains to redact titles for (password managers, banking) with sensible defaults.
- the GPU PC install happens only when Jamil says he's not gaming; the client runs as a user-level startup task, no admin service, no overlays.
- Existing `activity-logger` on the GPU PC: retired once the new client has run for a day side by side and numbers agree (builder compares).

## 7. The usage web app

- Timeline view (day, hour granularity), top apps / top sites, filters: device, date range, hours of day, app, domain; "away" time shown as gaps, not usage.
- Compare devices side by side; totals per day/week.
- Login: through the MCP gateway's OAuth (same browser approval Jamil already uses for Claude Code) — hub's call; a local password is the fallback if that proves awkward.
- Stack: builder's choice; must be a single container, dark mode, usable on the phone.

## 8. Repos (public, recruiter-safe)

- `JamilCPU/lab-data-store`: Postgres + Timescale manifests (StatefulSet, local PV, roles), ingest-api, backup CronJob, tenant conventions doc, `docs/superpowers/specs/` (this doc).
- `JamilCPU/usage-tracker`: sync client (Windows/Linux; Android via aw-android config), `usage` schema + migrations, usage-web.
- Deploy manifests land in `JamilCPU/lab-gitops` via Flux like everything else. **Images (hub's call):** follow whatever image path `lab-gitops` already uses for CI-built apps; if none is established, publish to GHCR from GitHub Actions (repos are public, so public images are fine) and keep the air-gapped Pi (no internet) off these workloads with a nodeSelector. The LAN registry is for images built on the primary node.
- No IPs, hostnames, tokens or sample personal data anywhere; `*.example` files only.

## 9. Build order

1. Store: Postgres on the primary node via GitOps, backup job verified by restoring the dump somewhere else, ingest-api with one device token and the `usage` schema.
2. Client on the primary node (Linux) end to end: events visible in the database with original timestamps after a forced offline period.
3. usage-web with device/date/hour filters, read-only role.
4. the GPU PC client (when Jamil isn't gaming), side-by-side with activity-logger for a day, then retire activity-logger.
5. the phone: aw-android pointed at the sync shim (or shim running on the phone), browser capture where possible.
6. Hand the store conventions to the private-data-layer session so step 3 (bank + email) writes to it.

## 10. Constraints

- Postgres is pinned to the primary node; everything else must be able to run on any node.
- Devices never get database credentials; tokens are per device and revocable.
- Nothing LAN-wide (DNS/DHCP/router) is touched by this project; `*.lab` names come from the migration's lab-dns.
- Public repos; no secrets, IPs, hostnames, PII, questionable content.
- the GPU PC: no install, reboot or GPU use while Jamil is gaming.

## 11. Left to the builder

- Exact ActivityWatch components vs. hand-written watchers (verify on Windows 11 and Android first).
- Sync interval, batch size, retry policy.
- Web stack, chart library, auth fallback.
- Timescale hypertable/retention settings; what "raw forever vs. hourly rollups after N months" looks like.
- Backup target (second disk on the primary node vs. the GPU PC `F:\Backups`) and schedule.

## 12. Open for Jamil (defaults ship if unanswered)

- Retention: keep raw events forever (default) or roll up after a year?
- Should the primary node (headless) be tracked at all? Default: yes, as a device, even if it's mostly terminal sessions.
- the phone browser capture: Chrome on Android has no extension support; default is foreground-app only on the phone unless he uses a browser that supports extensions.
