from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from ingest_api.sources import load_sources
from ingest_api.store_migrations import RoleConfigError, apply_store_migrations, ensure_roles

EXAMPLE = Path(__file__).parent.parent / "config" / "sources.example.yaml"
PASSWORDS = {
    "INGEST_PASSWORD": "pw-ingest",
    "USAGE_OWNER_PASSWORD": "pw-usage-owner",
    "USAGE_READER_PASSWORD": "pw-usage-reader",
    "BANK_OWNER_PASSWORD": "pw-bank-owner",
    "BANK_READER_PASSWORD": "pw-bank-reader",
    "EMAIL_OWNER_PASSWORD": "pw-email-owner",
    "EMAIL_READER_PASSWORD": "pw-email-reader",
    "SMS_OWNER_PASSWORD": "pw-" + "sms-owner",
    "SMS_READER_PASSWORD": "pw-" + "sms-reader",
}


def as_role(dsn, role, password):
    return psycopg.connect(make_conninfo(dsn, user=role, password=password))


def test_missing_password_names_env_var():
    passwords = {k: v for k, v in PASSWORDS.items() if k != "BANK_READER_PASSWORD"}
    with pytest.raises(RoleConfigError, match="BANK_READER_PASSWORD"):
        ensure_roles("postgresql://unused", load_sources(EXAMPLE), passwords)


@pytest.mark.integration
def test_apply_store_migrations_idempotent(test_dsn):
    assert apply_store_migrations(test_dsn) == ["0001_store"]
    assert apply_store_migrations(test_dsn) == []


@pytest.mark.integration
def test_ensure_roles_creates_schema_and_grants(test_dsn):
    sources = load_sources(EXAMPLE)
    ensure_roles(test_dsn, sources, PASSWORDS)
    ensure_roles(test_dsn, sources, PASSWORDS)  # idempotent

    with as_role(test_dsn, "usage_owner", PASSWORDS["USAGE_OWNER_PASSWORD"]) as conn:
        conn.execute("CREATE TABLE usage.events (id int primary key, v text)")
        conn.commit()
    ensure_roles(test_dsn, sources, PASSWORDS)  # the mapped table now exists
    with as_role(test_dsn, "ingest", PASSWORDS["INGEST_PASSWORD"]) as conn:
        conn.execute("INSERT INTO usage.events VALUES (1, 'x')")
        conn.execute("UPDATE usage.events SET v = 'y' WHERE id = 1")
        conn.commit()
    with as_role(test_dsn, "usage_reader", PASSWORDS["USAGE_READER_PASSWORD"]) as conn:
        assert conn.execute("SELECT v FROM usage.events").fetchall() == [("y",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO usage.events VALUES (2, 'z')")

    with psycopg.connect(test_dsn) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM store.sources")}
        assert {"usage", "bank", "email", "sms"} <= names
        assert conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'bank'").fetchone()


@pytest.mark.integration
def test_rerun_grants_on_existing_tables(test_dsn):
    sources = load_sources(EXAMPLE)
    ensure_roles(test_dsn, sources, PASSWORDS)
    with psycopg.connect(test_dsn) as conn:  # created by admin: default privileges don't apply
        conn.execute("CREATE TABLE usage.late (id int)")
        conn.commit()
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "usage_reader", PASSWORDS["USAGE_READER_PASSWORD"]) as conn:
        assert conn.execute("SELECT count(*) FROM usage.late").fetchone() == (0,)


@pytest.mark.integration
def test_roles_only_source_gets_no_ingest_access(test_dsn):
    sources = load_sources(EXAMPLE)
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "bank_owner", PASSWORDS["BANK_OWNER_PASSWORD"]) as conn:
        conn.execute("CREATE TABLE bank.t (id int)")
        conn.commit()
    ensure_roles(test_dsn, sources, PASSWORDS)  # also covers grants on existing tables
    with as_role(test_dsn, "ingest", PASSWORDS["INGEST_PASSWORD"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO bank.t VALUES (1)")
    with as_role(test_dsn, "bank_reader", PASSWORDS["BANK_READER_PASSWORD"]) as conn:
        assert conn.execute("SELECT count(*) FROM bank.t").fetchone() == (0,)


@pytest.mark.integration
def test_concurrent_bootstrap_is_serialised(test_dsn):
    sources = load_sources(EXAMPLE)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(ensure_roles, test_dsn, sources, PASSWORDS) for _ in range(4)]
        for future in futures:
            future.result()
    with psycopg.connect(test_dsn) as conn:
        rows = conn.execute("SELECT version FROM store.schema_migrations").fetchall()
        assert rows == [("0001_store",)]


@pytest.mark.integration
def test_ingest_cannot_write_schema_migrations(test_dsn):
    sources = load_sources(EXAMPLE)
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "usage_owner", PASSWORDS["USAGE_OWNER_PASSWORD"]) as conn:
        conn.execute("CREATE TABLE usage.events (id int primary key)")
        conn.execute("CREATE TABLE usage.schema_migrations (version text primary key)")
        conn.commit()
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "ingest", PASSWORDS["INGEST_PASSWORD"]) as conn:
        conn.execute("INSERT INTO usage.events VALUES (1)")
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO usage.schema_migrations VALUES ('x')")


@pytest.mark.integration
def test_ensure_roles_revokes_legacy_broad_ingest_grants(test_dsn):
    sources = load_sources(EXAMPLE)
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "usage_owner", PASSWORDS["USAGE_OWNER_PASSWORD"]) as conn:
        conn.execute("CREATE TABLE usage.events (id int primary key)")
        conn.execute("CREATE TABLE usage.schema_migrations (version text primary key)")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA usage TO ingest")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA usage "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO ingest"
        )
        conn.commit()
    ensure_roles(test_dsn, sources, PASSWORDS)
    with as_role(test_dsn, "usage_owner", PASSWORDS["USAGE_OWNER_PASSWORD"]) as conn:
        conn.execute("CREATE TABLE usage.later (id int)")
        conn.commit()
    with as_role(test_dsn, "ingest", PASSWORDS["INGEST_PASSWORD"]) as conn:
        conn.execute("INSERT INTO usage.events VALUES (1)")
        conn.commit()
        for table in ("schema_migrations", "later"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT 1 FROM usage.{table}")
            conn.rollback()


@pytest.mark.integration
def test_ingest_can_manage_device_tokens(test_dsn):
    ensure_roles(test_dsn, load_sources(EXAMPLE), PASSWORDS)
    with as_role(test_dsn, "ingest", PASSWORDS["INGEST_PASSWORD"]) as conn:
        conn.execute(
            "INSERT INTO store.device_tokens (device, sources, token_hash) "
            "VALUES ('d', ARRAY['usage'], 'h')"
        )
        conn.commit()
        assert conn.execute("SELECT count(*) FROM store.device_tokens").fetchone() == (1,)
