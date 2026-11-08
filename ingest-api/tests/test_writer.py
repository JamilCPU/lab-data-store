from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from conftest import EXAMPLE_SOURCES

from ingest_api.sources import load_sources
from ingest_api.writer import write_events

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 10, 9, tzinfo=UTC)


def event(**over):
    base = {
        "device": "pc1",
        "ts_start": T0,
        "ts_end": T1,
        "kind": "window",
        "app": "editor",
        "source_bucket": "b1",
        "source_event_id": 1,
    }
    return {**base, **over}


@pytest.fixture
def spec():
    return load_sources(EXAMPLE_SOURCES)["usage"]


def rows(conn):
    return conn.execute("SELECT ts_end, idle, title FROM usage.events").fetchall()


@pytest.mark.integration
def test_write_inserts_then_updates_on_conflict(usage_db, spec):
    with psycopg.connect(usage_db) as conn:
        first = write_events(conn, spec, [event()])
        assert (first.inserted, first.updated, first.replaced) == (1, 0, 0)
        second = write_events(conn, spec, [event(ts_end=T2)])
        assert (second.inserted, second.updated) == (0, 1)
        assert rows(conn) == [(T2, False, None)]  # omitted idle uses the column default


@pytest.mark.integration
def test_write_unchanged_row_counts_nothing(usage_db, spec):
    with psycopg.connect(usage_db) as conn:
        write_events(conn, spec, [event()])
        again = write_events(conn, spec, [event()])
        assert (again.inserted, again.updated) == (0, 0)
        assert len(rows(conn)) == 1


@pytest.mark.integration
def test_write_chunks_large_batches_and_collapses_duplicate_keys(usage_db, spec):
    batch = [event(source_event_id=i) for i in range(1200)]
    batch.append(event(source_event_id=0, ts_end=T2))  # duplicate key, last wins
    with psycopg.connect(usage_db) as conn:
        result = write_events(conn, spec, batch)
        assert (result.inserted, result.updated) == (1200, 0)
        count = conn.execute("SELECT count(*) FROM usage.events").fetchone()[0]
        assert count == 1200
        latest = conn.execute(
            "SELECT ts_end FROM usage.events WHERE source_event_id = 0"
        ).fetchone()[0]
        assert latest == T2


@pytest.mark.integration
def test_write_is_atomic_across_chunks(usage_db, spec):
    batch = [event(source_event_id=i) for i in range(600)]
    batch.append(event(source_event_id=999, kind="bogus"))  # violates the CHECK in chunk 2
    with psycopg.connect(usage_db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            write_events(conn, spec, batch)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM usage.events").fetchone()[0] == 0


def all_rows(conn):
    return conn.execute(
        "SELECT source_event_id, ts_start, ts_end FROM usage.events ORDER BY source_event_id"
    ).fetchall()


@pytest.mark.integration
def test_identity_key_replaces_same_event_with_different_ts_start(usage_db, spec):
    later = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)
    with psycopg.connect(usage_db) as conn:
        write_events(conn, spec, [event(source_event_id=1), event(source_event_id=2)])
        res = write_events(conn, spec, [event(source_event_id=1, ts_start=later, ts_end=T2)])
        assert (res.inserted, res.updated, res.replaced) == (1, 0, 1)
        assert all_rows(conn) == [(1, later, T2), (2, T0, T1)]  # id 2 untouched


@pytest.mark.integration
def test_identity_key_is_scoped_to_device_and_bucket(usage_db, spec):
    later = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)
    with psycopg.connect(usage_db) as conn:
        write_events(conn, spec, [event(), event(device="pc2"), event(source_bucket="b2")])
        res = write_events(conn, spec, [event(ts_start=later)])
        assert res.replaced == 1
        assert conn.execute("SELECT count(*) FROM usage.events").fetchone()[0] == 3


@pytest.mark.integration
def test_identity_key_collapses_one_batch_to_last_copy(usage_db, spec):
    later = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)
    with psycopg.connect(usage_db) as conn:
        res = write_events(conn, spec, [event(), event(ts_start=later, ts_end=T2)])
        assert (res.inserted, res.replaced) == (1, 0)
        assert all_rows(conn) == [(1, later, T2)]


@pytest.mark.integration
def test_identity_key_replacement_spans_chunks(usage_db, spec):
    later = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)
    with psycopg.connect(usage_db) as conn:
        write_events(conn, spec, [event(source_event_id=i) for i in range(700)])
        res = write_events(
            conn, spec, [event(source_event_id=i, ts_start=later) for i in range(700)]
        )
        assert (res.inserted, res.replaced) == (700, 700)
        assert conn.execute("SELECT count(*) FROM usage.events").fetchone()[0] == 700


@pytest.mark.integration
def test_without_identity_key_behaves_as_before(usage_db, spec):
    plain = replace(spec, identity_key=())
    later = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)
    with psycopg.connect(usage_db) as conn:
        write_events(conn, plain, [event()])
        res = write_events(conn, plain, [event(ts_start=later)])
        assert (res.inserted, res.replaced) == (1, 0)
        assert conn.execute("SELECT count(*) FROM usage.events").fetchone()[0] == 2
