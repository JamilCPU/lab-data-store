import os
import secrets

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


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
