import os
import secrets
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ingest_api.config import Settings
from ingest_api.sources import load_sources
from ingest_api.store_migrations import ensure_roles


def pytest_collection_modifyitems(config, items):
    if os.environ.get("TEST_ADMIN_DSN"):
        return
    skip = pytest.mark.skip(reason="TEST_ADMIN_DSN not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def test_dsn():
    """DSN of a fresh throwaway database per test, dropped afterwards."""
    admin = os.environ.get("TEST_ADMIN_DSN")
    if not admin:
        pytest.skip("TEST_ADMIN_DSN not set")
    name = f"lds_test_{secrets.token_hex(4)}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin, dbname=name)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
            conn.execute(drop.format(sql.Identifier(name)))


# Passwords for the throwaway roles; only ever used against per-test databases.
ROLE_PASSWORDS = {
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
EXAMPLE_SOURCES = Path(__file__).parent.parent / "config" / "sources.example.yaml"
USAGE_EVENTS_SQL = Path(__file__).parent / "fixtures" / "usage_events.sql"


@pytest.fixture
def usage_db(test_dsn):
    """Throwaway database with roles provisioned and the usage.events table created."""
    sources = load_sources(EXAMPLE_SOURCES)
    ensure_roles(test_dsn, sources, ROLE_PASSWORDS)
    owner_dsn = make_conninfo(test_dsn, user="usage_owner", password="pw-usage-owner")
    with psycopg.connect(owner_dsn) as conn:
        conn.execute(USAGE_EVENTS_SQL.read_text(encoding="utf-8"))
    ensure_roles(test_dsn, sources, ROLE_PASSWORDS)  # grants on the table that now exists
    return test_dsn


@pytest.fixture
def app_settings(usage_db, monkeypatch):
    """Factory for Settings pointing at the throwaway database."""
    for key, value in ROLE_PASSWORDS.items():
        monkeypatch.setenv(key, value)

    def build(**overrides):
        ingest_dsn = make_conninfo(usage_db, user="ingest", password="pw-ingest")
        return Settings(
            **{"STORE_ADMIN_DSN": usage_db, **overrides},
            STORE_INGEST_DSN=ingest_dsn,
            SOURCES_FILE=str(EXAMPLE_SOURCES),
        )

    return build
