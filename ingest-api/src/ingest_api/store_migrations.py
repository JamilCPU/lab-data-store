"""Store-owned schema migrations and per-source role bootstrap."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import psycopg
from psycopg import sql

from ingest_api.sources import SourceSpec

MIGRATIONS_DIR = Path(__file__).parent / "store_migrations"

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS store;
CREATE TABLE IF NOT EXISTS store.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz DEFAULT now()
);
"""

_SOURCE_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {owner}",
    "GRANT USAGE ON SCHEMA {schema} TO {ingest}, {reader}",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
    "GRANT SELECT, INSERT, UPDATE ON TABLES TO {ingest}",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
    "GRANT SELECT ON TABLES TO {reader}",
    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} TO {ingest}",
    "GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader}",
)


class RoleConfigError(Exception):
    """Raised when a required role password is missing from the environment."""


def apply_store_migrations(admin_dsn: str) -> list[str]:
    """Apply pending SQL files in order, one transaction each; return applied versions."""
    applied: list[str] = []
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(_BOOTSTRAP)
        conn.commit()
        done = {r[0] for r in conn.execute("SELECT version FROM store.schema_migrations")}
        conn.commit()
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            if path.stem in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO store.schema_migrations (version) VALUES (%s)", (path.stem,)
                )
            applied.append(path.stem)
    return applied


def _password(passwords: Mapping[str, str], var: str) -> str:
    value = passwords.get(var)
    if not value:
        raise RoleConfigError(f"missing role password: set environment variable {var}")
    return value


def _ensure_role(conn: psycopg.Connection, role: str, password: str) -> None:
    ident = sql.Identifier(role)
    conn.execute(
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {name}) "
            "THEN CREATE ROLE {ident} LOGIN; END IF; END $$"
        ).format(name=sql.Literal(role), ident=ident)
    )
    conn.execute(
        sql.SQL("ALTER ROLE {ident} LOGIN PASSWORD {pw}").format(
            ident=ident, pw=sql.Literal(password)
        )
    )


def _ensure_source(conn: psycopg.Connection, spec: SourceSpec) -> None:
    names = {
        "schema": sql.Identifier(spec.schema),
        "owner": sql.Identifier(f"{spec.name}_owner"),
        "reader": sql.Identifier(f"{spec.name}_reader"),
        "ingest": sql.Identifier("ingest"),
    }
    for stmt in _SOURCE_STATEMENTS:
        conn.execute(sql.SQL(stmt).format(**names))
    conn.execute(
        "INSERT INTO store.sources (name, schema_name) VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE SET schema_name = EXCLUDED.schema_name",
        (spec.name, spec.schema),
    )


def ensure_roles(
    admin_dsn: str, sources: dict[str, SourceSpec], passwords: Mapping[str, str]
) -> None:
    """Create roles, schemas and grants for every source. Idempotent.

    Applies store migrations first so ``store.sources`` and the token table exist.
    """
    role_pws = {"ingest": _password(passwords, "INGEST_PASSWORD")}
    for spec in sources.values():
        for kind in ("owner", "reader"):
            var = f"{spec.name.upper()}_{kind.upper()}_PASSWORD"
            role_pws[f"{spec.name}_{kind}"] = _password(passwords, var)
    apply_store_migrations(admin_dsn)
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for role, pw in role_pws.items():
            _ensure_role(conn, role, pw)
        for spec in sources.values():
            _ensure_source(conn, spec)
        # The API runs as `ingest` and authenticates devices against the token table.
        conn.execute("GRANT USAGE ON SCHEMA store TO ingest")
        conn.execute("GRANT SELECT ON store.device_tokens TO ingest")
        conn.execute("GRANT UPDATE (last_seen_at) ON store.device_tokens TO ingest")
        conn.commit()
