import hashlib

import psycopg
import pytest

from ingest_api.store_migrations import apply_store_migrations
from ingest_api.tokens import (
    create_token,
    hash_token,
    lookup_token,
    new_token,
    revoke_token,
    touch_token,
)


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(hash_token("abc")) == 64


def test_new_token_prefix_and_length():
    token = new_token()
    assert token.startswith("lds_")
    assert len(token) >= 40
    assert new_token() != token


@pytest.mark.integration
def test_create_lookup_revoke_token(test_dsn):
    apply_store_migrations(test_dsn)
    with psycopg.connect(test_dsn) as conn:
        token, token_id = create_token(conn, "pc1", ["usage"], "laptop")
        info = lookup_token(conn, token)
        assert info is not None
        assert (info.id, info.device, info.sources) == (token_id, "pc1", ("usage",))
        assert lookup_token(conn, "lds_unknown") is None
        stored = conn.execute("SELECT token_hash FROM store.device_tokens").fetchall()
        assert stored == [(hash_token(token),)]
        touch_token(conn, token_id)
        seen = conn.execute(
            "SELECT last_seen_at FROM store.device_tokens WHERE id = %s", (token_id,)
        ).fetchone()
        assert seen[0] is not None
        assert revoke_token(conn, token_id) is True
        assert revoke_token(conn, token_id) is False
        assert lookup_token(conn, token) is None


@pytest.mark.integration
def test_writes_are_visible_to_other_connections(test_dsn):
    apply_store_migrations(test_dsn)
    with psycopg.connect(test_dsn) as conn, psycopg.connect(test_dsn) as other:
        token, token_id = create_token(conn, "pc1", ["usage"])
        assert lookup_token(other, token) is not None  # create committed
        other.commit()
        # lookup leaves a transaction open on `conn`; touch/revoke must still commit
        assert lookup_token(conn, token) is not None
        touch_token(conn, token_id)
        seen = other.execute(
            "SELECT last_seen_at FROM store.device_tokens WHERE id = %s", (token_id,)
        ).fetchone()
        other.commit()
        assert seen[0] is not None
        assert lookup_token(conn, token) is not None
        assert revoke_token(conn, token_id) is True
        assert lookup_token(other, token) is None
