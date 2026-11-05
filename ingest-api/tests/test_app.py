from contextlib import AsyncExitStack

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from ingest_api.app import create_app
from ingest_api.tokens import create_token, revoke_token

pytestmark = pytest.mark.integration

START = "2026-01-01T10:00:00+00:00"
END = "2026-01-01T10:05:00+00:00"


def ev(i=1, **over):
    base = {
        "device": "pc1",
        "ts_start": START,
        "ts_end": END,
        "kind": "window",
        "source_bucket": "b1",
        "source_event_id": i,
    }
    return {**base, **over}


def body(events, source="usage", device="pc1"):
    return {"source": source, "device": device, "events": events}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def count(dsn):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM usage.events").fetchone()[0]


def mint(dsn, sources=("usage",), device="pc1"):
    with psycopg.connect(dsn) as conn:
        return create_token(conn, device, list(sources), "test")


@pytest.fixture
async def make_client(app_settings):
    """Start the app (running its lifespan) and return an async client."""
    async with AsyncExitStack() as stack:

        async def build(**overrides):
            app = create_app(app_settings(**overrides))
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await stack.enter_async_context(client)
            return client

        yield build


async def test_ingest_requires_bearer(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    payload = body([ev()])
    assert (await client.post("/v1/ingest", json=payload)).status_code == 401
    unknown = await client.post("/v1/ingest", json=payload, headers=bearer("lds_nope"))
    assert unknown.status_code == 401
    assert unknown.json() == {"detail": "invalid token"}
    no_scheme = await client.post("/v1/ingest", json=payload, headers={"Authorization": token})
    assert no_scheme.status_code == 401


async def test_ingest_rejects_revoked_token(make_client, usage_db):
    client = await make_client()
    token, token_id = mint(usage_db)
    with psycopg.connect(usage_db) as conn:
        revoke_token(conn, token_id)
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 401


async def test_ingest_rejects_other_device(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    payload = body([ev(device="pc2")], device="pc2")
    resp = await client.post("/v1/ingest", json=payload, headers=bearer(token))
    assert resp.status_code == 403
    assert count(usage_db) == 0


async def test_ingest_rejects_event_for_other_device(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    events = [ev(1), ev(2, device="pc2")]
    resp = await client.post("/v1/ingest", json=body(events), headers=bearer(token))
    assert resp.status_code == 403
    assert count(usage_db) == 0


async def test_ingest_stamps_missing_event_device(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    event = ev(1)
    del event["device"]
    resp = await client.post("/v1/ingest", json=body([event]), headers=bearer(token))
    assert resp.status_code == 200
    with psycopg.connect(usage_db) as conn:
        assert conn.execute("SELECT device FROM usage.events").fetchall() == [("pc1",)]


async def test_ingest_rejects_unknown_source(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    resp = await client.post("/v1/ingest", json=body([ev()], source="nope"), headers=bearer(token))
    assert resp.status_code == 403
    assert count(usage_db) == 0


async def test_ingest_rejects_roles_only_source(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db, sources=("bank",))
    resp = await client.post("/v1/ingest", json=body([ev()], source="bank"), headers=bearer(token))
    assert resp.status_code == 403
    assert count(usage_db) == 0


async def test_ingest_rejects_source_not_granted_to_token(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db, sources=("email",))
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 403
    assert count(usage_db) == 0


async def test_ingest_rejects_bad_row_with_index(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    resp = await client.post(
        "/v1/ingest", json=body([ev(1), ev(2, kind="bogus")]), headers=bearer(token)
    )
    assert resp.status_code == 422
    detail = resp.json()
    assert (detail["index"], detail["field"]) == (1, "kind")
    assert detail["message"]
    assert count(usage_db) == 0


async def test_ingest_accepts_batch_sets_received_at(make_client, usage_db):
    client = await make_client()
    token, token_id = mint(usage_db)
    resp = await client.post("/v1/ingest", json=body([ev(1), ev(2)]), headers=bearer(token))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2, "inserted": 2, "updated": 0}
    with psycopg.connect(usage_db) as conn:
        total, stamped = conn.execute(
            "SELECT count(*), count(received_at) FROM usage.events"
        ).fetchone()
        assert (total, stamped) == (2, 2)
        seen = conn.execute(
            "SELECT last_seen_at FROM store.device_tokens WHERE id = %s", (token_id,)
        ).fetchone()
        assert seen[0] is not None
    again = await client.post("/v1/ingest", json=body([ev(1), ev(2)]), headers=bearer(token))
    assert again.json() == {"accepted": 2, "inserted": 0, "updated": 0}


async def test_ingest_rate_limited(make_client, usage_db):
    client = await make_client(RATE_LIMIT_PER_MINUTE=2)
    token, _ = mint(usage_db)
    codes = [
        (await client.post("/v1/ingest", json=body([ev(i)]), headers=bearer(token))).status_code
        for i in range(3)
    ]
    assert codes == [200, 200, 429]


async def test_ingest_batch_too_large(make_client, usage_db):
    client = await make_client(MAX_BATCH=2)
    token, _ = mint(usage_db)
    resp = await client.post(
        "/v1/ingest", json=body([ev(i) for i in range(3)]), headers=bearer(token)
    )
    assert resp.status_code == 413
    assert count(usage_db) == 0


async def test_healthz(make_client):
    client = await make_client()
    assert (await client.get("/healthz")).json() == {"ok": True}


async def test_readyz(make_client):
    client = await make_client()
    assert (await client.get("/readyz")).status_code == 200
    client._transport.app.state.pool.close()
    assert (await client.get("/readyz")).status_code == 503


async def test_ingest_database_rejection_is_422_without_values(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    resp = await client.post("/v1/ingest", json=body([ev(2**70)]), headers=bearer(token))
    assert resp.status_code == 422
    detail = resp.json()
    assert detail["index"] is None and detail["field"] is None
    assert "sqlstate" in detail["message"]
    assert str(2**70) not in detail["message"]
    assert count(usage_db) == 0


async def test_app_starts_without_admin_dsn(make_client, usage_db):
    client = await make_client(STORE_ADMIN_DSN=None)
    token, _ = mint(usage_db)
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 200
    assert count(usage_db) == 1


async def test_ingest_resend_with_longer_end_updates_one_row(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    await client.post("/v1/ingest", json=body([ev(1, title="a")]), headers=bearer(token))
    later = "2026-01-01T10:09:00+00:00"
    resp = await client.post(
        "/v1/ingest", json=body([ev(1, ts_end=later, title="b")]), headers=bearer(token)
    )
    assert resp.json() == {"accepted": 1, "inserted": 0, "updated": 1}
    assert count(usage_db) == 1
    with psycopg.connect(usage_db) as conn:
        title, end = conn.execute("SELECT title, ts_end FROM usage.events").fetchone()
    assert title == "b" and end.minute == 9


async def test_ingest_second_event_missing_ts_start_is_422_with_index(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    bad = ev(2)
    del bad["ts_start"]
    resp = await client.post("/v1/ingest", json=body([ev(1), bad]), headers=bearer(token))
    assert resp.status_code == 422
    detail = resp.json()
    assert (detail["index"], detail["field"]) == (1, "ts_start")
    assert count(usage_db) == 0


async def test_ingest_explicit_null_uses_column_default(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    resp = await client.post("/v1/ingest", json=body([ev(1, idle=None)]), headers=bearer(token))
    assert resp.status_code == 200
    with psycopg.connect(usage_db) as conn:
        assert conn.execute("SELECT idle FROM usage.events").fetchone() == (False,)


async def test_ingest_constraint_violation_is_422(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    with psycopg.connect(usage_db) as conn:
        conn.execute("ALTER TABLE usage.events DROP CONSTRAINT events_kind_check")
        conn.execute("ALTER TABLE usage.events ADD CONSTRAINT k CHECK (kind <> 'afk')")
        conn.commit()
    resp = await client.post("/v1/ingest", json=body([ev(1, kind="afk")]), headers=bearer(token))
    assert resp.status_code == 422
    assert "sqlstate 23514" in resp.json()["message"]


async def test_ingest_server_side_database_error_is_503(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    with psycopg.connect(usage_db) as conn:
        conn.execute("REVOKE INSERT ON usage.events FROM ingest")
        conn.commit()
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unavailable"}


async def test_ingest_missing_table_is_503(make_client, usage_db):
    client = await make_client()
    token, _ = mint(usage_db)
    with psycopg.connect(usage_db) as conn:
        conn.execute("DROP TABLE usage.events")
        conn.commit()
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 503


async def test_touch_failure_after_commit_does_not_fail_response(
    make_client, usage_db, monkeypatch
):
    client = await make_client()
    token, _ = mint(usage_db)

    def boom(app, token_id):
        raise psycopg.errors.InsufficientPrivilege("nope")

    monkeypatch.setattr("ingest_api.app._touch_throttled", boom)
    resp = await client.post("/v1/ingest", json=body([ev()]), headers=bearer(token))
    assert resp.status_code == 200
    assert count(usage_db) == 1
