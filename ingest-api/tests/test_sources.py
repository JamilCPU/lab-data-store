from datetime import UTC, datetime
from pathlib import Path

import pytest

from ingest_api.sources import (
    EventValidationError,
    SourceConfigError,
    load_sources,
    validate_events,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "sources.example.yaml"


@pytest.fixture
def usage():
    return load_sources(EXAMPLE)["usage"]


def make_event(**overrides):
    event = {
        "device": "pc1",
        "ts_start": "2026-01-01T10:00:00Z",
        "ts_end": "2026-01-01T10:05:00Z",
        "kind": "window",
        "source_bucket": "b1",
        "source_event_id": 1,
    }
    event.update(overrides)
    return event


def test_load_example_sources_has_usage(usage):
    assert usage.name == "usage"
    assert usage.conflict_key == ("device", "source_bucket", "source_event_id", "ts_start")
    assert usage.on_conflict == "update"
    assert usage.enum["kind"] == ("window", "web", "afk")


def test_validate_coerces_timestamps_to_aware_datetime(usage):
    [out] = validate_events(usage, [make_event()])
    assert out["ts_start"] == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert out["ts_start"].tzinfo is not None


def test_validate_rejects_missing_required(usage):
    events = [make_event(), make_event(), make_event()]
    del events[2]["ts_end"]
    with pytest.raises(EventValidationError) as exc:
        validate_events(usage, events)
    assert exc.value.index == 2
    assert exc.value.field == "ts_end"


def test_validate_rejects_end_before_start(usage):
    with pytest.raises(EventValidationError) as exc:
        validate_events(usage, [make_event(ts_end="2026-01-01T09:00:00Z")])
    assert exc.value.field == "ts_end"


def test_validate_rejects_unknown_kind(usage):
    with pytest.raises(EventValidationError) as exc:
        validate_events(usage, [make_event(kind="bogus")])
    assert exc.value.field == "kind"


def test_validate_rejects_unknown_column(usage):
    with pytest.raises(EventValidationError) as exc:
        validate_events(usage, [make_event(foo=1)])
    assert exc.value.field == "foo"


def test_validate_naive_timestamp_rejected(usage):
    with pytest.raises(EventValidationError) as exc:
        validate_events(usage, [make_event(ts_start="2026-01-01T10:00:00")])
    assert exc.value.field == "ts_start"


def test_validate_type_rules(usage):
    with pytest.raises(EventValidationError):
        validate_events(usage, [make_event(idle="true")])
    with pytest.raises(EventValidationError):
        validate_events(usage, [make_event(source_event_id=True)])
    with pytest.raises(EventValidationError):
        validate_events(usage, [make_event(source_event_id=1.5)])
    with pytest.raises(EventValidationError):
        validate_events(usage, [make_event(device=None)])
    [out] = validate_events(usage, [make_event(idle=False, app=None)])
    assert out["idle"] is False and out["app"] is None


def test_validate_does_not_mutate_input(usage):
    event = make_event()
    validate_events(usage, [event])
    assert event["ts_start"] == "2026-01-01T10:00:00Z"


def _write(tmp_path, body):
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_roles_only_source_loads_without_mapping(tmp_path):
    path = _write(tmp_path, "sources:\n  bank:\n    schema: bank\n")
    spec = load_sources(path)["bank"]
    assert spec.schema == "bank"
    assert spec.table is None
    assert spec.columns == {}
    assert spec.ingestable is False
    assert load_sources(EXAMPLE)["usage"].ingestable is True
    assert {"bank", "email"} <= set(load_sources(EXAMPLE))


def test_conflict_key_must_be_required(tmp_path):
    body = (
        "sources:\n  s:\n    schema: s\n    table: t\n"
        "    columns: {a: text, b: text}\n    required: [a]\n    conflict_key: [a, b]\n"
    )
    with pytest.raises(SourceConfigError, match="conflict_key"):
        load_sources(_write(tmp_path, body))


def test_enum_only_on_text_columns(tmp_path):
    body = (
        "sources:\n  s:\n    schema: s\n    table: t\n"
        "    columns: {a: text, n: int}\n    required: [a]\n    conflict_key: [a]\n"
        "    enum: {n: ['1']}\n"
    )
    with pytest.raises(SourceConfigError, match="enum"):
        load_sources(_write(tmp_path, body))
