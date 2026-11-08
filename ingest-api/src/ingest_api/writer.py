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
    replaced: int = 0


def _dedupe(spec: SourceSpec, events: list[dict]) -> list[dict]:
    """Keep the last event per key; one statement cannot touch a row twice.

    With an identity_key the batch is collapsed per identity, so at most one copy of a
    source event is written and the replace-delete below leaves exactly that copy.
    """
    key = spec.identity_key or spec.conflict_key
    latest = {tuple(e.get(c) for c in key): e for e in events}
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


_PG_TYPES = {
    "text": "text",
    "timestamptz": "timestamptz",
    "bool": "boolean",
    "bigint": "bigint",
    "int": "integer",
    "double": "double precision",
    "jsonb": "jsonb",
}


def _build_replace(spec: SourceSpec, chunk: list[dict]) -> tuple[sql.Composed, list[Any]]:
    """DELETE rows that share a batch row's identity but not its full conflict key."""
    ident = sql.Identifier
    cols = list(spec.conflict_key)
    row = sql.SQL("({})").format(
        sql.SQL(", ").join(
            sql.SQL("{}::{}").format(sql.Placeholder(), sql.SQL(_PG_TYPES[spec.columns[c]]))
            for c in cols
        )
    )
    batch = ident("batch")
    stmt = sql.SQL(
        "DELETE FROM {table} AS t USING (VALUES {values}) AS {batch} ({cols}) "
        "WHERE ({t_id}) = ({b_id}) AND ({t_key}) IS DISTINCT FROM ({b_key})"
    ).format(
        table=sql.SQL(".").join([ident(spec.schema), ident(spec.table)]),
        values=sql.SQL(", ").join(row for _ in chunk),
        batch=batch,
        cols=sql.SQL(", ").join(ident(c) for c in cols),
        t_id=sql.SQL(", ").join(sql.SQL("t.{}").format(ident(c)) for c in spec.identity_key),
        b_id=sql.SQL(", ").join(
            sql.SQL("{}.{}").format(batch, ident(c)) for c in spec.identity_key
        ),
        t_key=sql.SQL(", ").join(sql.SQL("t.{}").format(ident(c)) for c in cols),
        b_key=sql.SQL(", ").join(sql.SQL("{}.{}").format(batch, ident(c)) for c in cols),
    )
    return stmt, [r[c] for r in chunk for c in cols]


def _param(value: Any, ctype: str) -> Any:
    return Jsonb(value) if ctype == "jsonb" and value is not None else value


def write_events(conn: psycopg.Connection, spec: SourceSpec, events: list[dict]) -> WriteResult:
    """Upsert events in one transaction; return counts of inserted vs updated rows."""
    if spec.table is None:
        raise ValueError(f"source '{spec.name}' has no ingest mapping")
    columns = list(spec.columns)
    # An explicit null means "not provided": the column DEFAULT applies (NULL if none).
    events = [{k: v for k, v in e.items() if v is not None} for e in events]
    rows = _dedupe(spec, events)
    inserted = updated = replaced = 0
    with conn.transaction():
        for start in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[start : start + CHUNK_SIZE]
            if spec.identity_key:
                stmt, args = _build_replace(spec, chunk)
                replaced += conn.execute(stmt, args).rowcount
            params = [_param(r[c], spec.columns[c]) for r in chunk for c in columns if c in r]
            result = conn.execute(_build_statement(spec, columns, chunk), params).fetchall()
            inserted += sum(1 for (is_new,) in result if is_new)
            updated += sum(1 for (is_new,) in result if not is_new)
    return WriteResult(inserted=inserted, updated=updated, replaced=replaced)
