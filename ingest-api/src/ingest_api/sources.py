"""Source registry (YAML) and event validation. No database access."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

ColumnType = Literal["text", "timestamptz", "bool", "bigint", "int", "double", "jsonb"]


class SourceConfigError(Exception):
    """Raised when a sources YAML file is malformed or inconsistent."""


class EventValidationError(Exception):
    def __init__(self, index: int, field: str, message: str) -> None:
        super().__init__(f"event[{index}].{field}: {message}")
        self.index = index
        self.field = field
        self.message = message


@dataclass(frozen=True)
class SourceSpec:
    name: str
    schema: str
    table: str | None = None
    columns: dict[str, ColumnType] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    conflict_key: tuple[str, ...] = ()
    identity_key: tuple[str, ...] = ()
    on_conflict: Literal["update", "nothing"] = "update"
    enum: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ingestable(self) -> bool:
        return self.table is not None


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: str
    table: str | None = None
    columns: dict[str, ColumnType] = {}
    required: list[str] = []
    conflict_key: list[str] = []
    identity_key: list[str] | None = None
    on_conflict: Literal["update", "nothing"] = "update"
    enum: dict[str, list[str]] = {}

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> _SourceModel:
        if not isinstance(raw, dict):
            raise TypeError("source definition must be a mapping")
        data = dict(raw)
        if "schema" in data:
            data["schema_"] = data.pop("schema")
        return cls.model_validate(data)


class _RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: dict[str, dict[str, Any]]


def _build_spec(name: str, raw: dict[str, Any]) -> SourceSpec:
    try:
        model = _SourceModel.parse(raw)
    except (ValidationError, TypeError) as exc:
        raise SourceConfigError(f"source '{name}': {exc}") from exc
    if model.table is None:
        if model.columns or model.required or model.conflict_key or model.enum:
            raise SourceConfigError(f"source '{name}': ingest mapping requires 'table'")
        if model.identity_key is not None:
            raise SourceConfigError(f"source '{name}': ingest mapping requires 'table'")
        return SourceSpec(name=name, schema=model.schema_)
    for label, names in (
        ("required", model.required),
        ("conflict_key", model.conflict_key),
        ("enum", list(model.enum)),
    ):
        unknown = [n for n in names if n not in model.columns]
        if unknown:
            raise SourceConfigError(f"source '{name}': {label} names unknown column(s) {unknown}")
    if not model.conflict_key:
        raise SourceConfigError(f"source '{name}': conflict_key must not be empty")
    outside = [c for c in model.conflict_key if c not in model.required]
    if outside:
        raise SourceConfigError(
            f"source '{name}': conflict_key column(s) {outside} must also be required"
        )
    identity = model.identity_key
    if identity is not None:
        bad = [c for c in identity if c not in model.conflict_key]
        if not identity or bad or len(set(identity)) != len(identity):
            raise SourceConfigError(
                f"source '{name}': identity_key must be a non-empty list of distinct "
                f"conflict_key columns (offending: {bad})"
            )
    non_text = [c for c in model.enum if model.columns[c] != "text"]
    if non_text:
        raise SourceConfigError(f"source '{name}': enum only allowed on text columns {non_text}")
    return SourceSpec(
        name=name,
        schema=model.schema_,
        table=model.table,
        columns=dict(model.columns),
        required=tuple(model.required),
        conflict_key=tuple(model.conflict_key),
        identity_key=tuple(identity or ()),
        on_conflict=model.on_conflict,
        enum={k: tuple(v) for k, v in model.enum.items()},
    )


def load_sources(path: Path) -> dict[str, SourceSpec]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourceConfigError(f"cannot read {path}: {exc}") from exc
    try:
        registry = _RegistryModel.model_validate(raw)
    except ValidationError as exc:
        raise SourceConfigError(f"invalid sources file {path}: {exc}") from exc
    return {name: _build_spec(name, body) for name, body in registry.sources.items()}


def _convert(value: Any, ctype: str) -> Any:
    """Convert one non-None value; raise ValueError with a message on mismatch."""
    if ctype == "text":
        if isinstance(value, str):
            return value
        raise ValueError("expected a string")
    if ctype == "timestamptz":
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("expected an ISO-8601 timestamp") from exc
        if not isinstance(value, datetime):
            raise ValueError("expected an ISO-8601 timestamp")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value
    if ctype == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected a boolean")
    if ctype in ("bigint", "int"):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("expected an integer")
    if ctype == "double":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ValueError("expected a number")
    if ctype == "jsonb":
        if isinstance(value, (dict, list)):
            return value
        raise ValueError("expected an object or array")
    raise ValueError(f"unsupported column type {ctype}")


def _validate_one(spec: SourceSpec, index: int, event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise EventValidationError(index, "", "event must be an object")
    for key in event:
        if key not in spec.columns:
            raise EventValidationError(index, str(key), "unknown column")
    out: dict[str, Any] = {}
    for col in spec.required:
        if event.get(col) is None:
            raise EventValidationError(index, col, "required field is missing or null")
    for col, value in event.items():
        if value is None:
            out[col] = None
            continue
        try:
            out[col] = _convert(value, spec.columns[col])
        except ValueError as exc:
            raise EventValidationError(index, col, str(exc)) from exc
        allowed = spec.enum.get(col)
        if allowed is not None and out[col] not in allowed:
            raise EventValidationError(index, col, f"must be one of {list(allowed)}")
    start, end = out.get("ts_start"), out.get("ts_end")
    if (
        "ts_start" in spec.columns
        and "ts_end" in spec.columns
        and start is not None
        and end is not None
        and end < start
    ):
        raise EventValidationError(index, "ts_end", "ts_end must not be before ts_start")
    return out


def validate_events(spec: SourceSpec, events: list[dict]) -> list[dict]:
    return [_validate_one(spec, i, event) for i, event in enumerate(events)]


__all__ = [
    "ColumnType",
    "EventValidationError",
    "SourceConfigError",
    "SourceSpec",
    "load_sources",
    "validate_events",
]
