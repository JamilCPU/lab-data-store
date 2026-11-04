"""Per-device bearer tokens. Only the SHA-256 hash is stored."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class TokenInfo:
    id: int
    device: str
    sources: tuple[str, ...]


def _commit(conn: psycopg.Connection) -> None:
    """Make a write durable regardless of the connection's autocommit mode."""
    if not conn.autocommit:
        conn.commit()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "lds_" + secrets.token_urlsafe(32)


def create_token(
    conn: psycopg.Connection, device: str, sources: list[str], note: str | None = None
) -> tuple[str, int]:
    """Create a token; the plaintext is returned once and never stored."""
    token = new_token()
    row = conn.execute(
        "INSERT INTO store.device_tokens (device, sources, token_hash, note) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (device, sources, hash_token(token), note),
    ).fetchone()
    _commit(conn)
    return token, int(row[0])


def lookup_token(conn: psycopg.Connection, token: str) -> TokenInfo | None:
    row = conn.execute(
        "SELECT id, device, sources FROM store.device_tokens "
        "WHERE token_hash = %s AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    return TokenInfo(id=int(row[0]), device=row[1], sources=tuple(row[2]))


def revoke_token(conn: psycopg.Connection, token_id: int) -> bool:
    cur = conn.execute(
        "UPDATE store.device_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (token_id,),
    )
    _commit(conn)
    return cur.rowcount == 1


def touch_token(conn: psycopg.Connection, token_id: int) -> None:
    conn.execute("UPDATE store.device_tokens SET last_seen_at = now() WHERE id = %s", (token_id,))
    _commit(conn)
