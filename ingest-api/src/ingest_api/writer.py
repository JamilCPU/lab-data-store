"""Batch upsert of validated events into a source's table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ingest_api.sources import SourceSpec

CHUNK_SIZE = 500


@dataclass(frozen=True)
class WriteResult:
    inserted: int
    updated: int


def _dedupe(spec: SourceSpec, events: list[dict]) -> list[dict]:
    """Keep the last event per conflict key; one statement cannot touch a row twice."""
    latest = {tuple(e.get(c) for c in spec.conflict_key): e for e in events}
    return list(latest.values())


def _values_row(columns: list[str], event: dict) -> sql.Composed:
    """One VALUES tuple; columns the event omits use the column DEFAULT (NULL if none)."""
    cells = [sql.Placeholder() if c in event else sql.SQL("DEFAULT") for c in columns]
    return sql.SQL("({})").format(sql.SQL(", ").join(cells))


def _build_statement(spec: SourceSpec, columns: list[str], chunk: list[dict]) -> sql.Composed:
    ident = sql.Identifier
    stmt = sql.SQL("INSERT INTO {table} ({cols}) VALUES {values} ON CONFLICT ({key}) ").format(
        table=sql.SQL(".").join([ident(spec.schema), ident(spec.table)]),
        cols=sql.SQL(", ").join(ident(c) for c in columns),
        values=sql.SQL(", ").join(_values_row(columns, e) for e in chunk),
        key=sql.SQL(", ").join(ident(c) for c in spec.conflict_key),
    )
    non_key = [c for c in columns if c not in spec.conflict_key]
    if spec.on_conflict == "update" and non_key:
        stmt += sql.SQL("DO UPDATE SET {sets} WHERE ({cur}) IS DISTINCT FROM ({new}) ").format(
            sets=sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=ident(c)) for c in non_key
            ),
            cur=sql.SQL(", ").join(
                sql.SQL("{t}.{c}").format(t=ident(spec.table), c=ident(c)) for c in non_key
            ),
            new=sql.SQL(", ").join(sql.SQL("EXCLUDED.{c}").format(c=ident(c)) for c in non_key),
        )
    else:
        stmt += sql.SQL("DO NOTHING ")
    return stmt + sql.SQL("RETURNING (xmax = 0) AS inserted")


def _param(value: Any, ctype: str) -> Any:
    return Jsonb(value) if ctype == "jsonb" and value is not None else value


def write_events(conn: psycopg.Connection, spec: SourceSpec, events: list[dict]) -> WriteResult:
    """Upsert events in one transaction; return counts of inserted vs updated rows."""
    if spec.table is None:
        raise ValueError(f"source '{spec.name}' has no ingest mapping")
    columns = list(spec.columns)
    rows = _dedupe(spec, events)
    inserted = updated = 0
    with conn.transaction():
        for start in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[start : start + CHUNK_SIZE]
            params = [_param(r[c], spec.columns[c]) for r in chunk for c in columns if c in r]
            result = conn.execute(_build_statement(spec, columns, chunk), params).fetchall()
            inserted += sum(1 for (is_new,) in result if is_new)
            updated += sum(1 for (is_new,) in result if not is_new)
    return WriteResult(inserted=inserted, updated=updated)
