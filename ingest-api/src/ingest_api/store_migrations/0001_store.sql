CREATE SCHEMA IF NOT EXISTS store;

CREATE TABLE IF NOT EXISTS store.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz DEFAULT now()
);

CREATE TABLE store.device_tokens (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device       text NOT NULL,
    sources      text[] NOT NULL,
    token_hash   text NOT NULL UNIQUE,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    revoked_at   timestamptz
);

CREATE TABLE store.sources (
    name          text PRIMARY KEY,
    schema_name   text NOT NULL,
    registered_at timestamptz DEFAULT now()
);
