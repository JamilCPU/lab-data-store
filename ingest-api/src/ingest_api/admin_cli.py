"""store-admin: bootstrap and device-token management."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from ingest_api.config import AdminSettings
from ingest_api.sources import load_sources
from ingest_api.store_migrations import apply_store_migrations, ensure_roles
from ingest_api.tokens import create_token, revoke_token

app = typer.Typer(no_args_is_help=True, help="Administer the lab data store.")
token_app = typer.Typer(no_args_is_help=True, help="Manage device tokens.")
app.add_typer(token_app, name="token")


def _token_dsn() -> str:
    """Token management works with the admin DSN, or with the ingest role's own DSN."""
    settings = AdminSettings()
    dsn = settings.store_admin_dsn or settings.store_ingest_dsn
    if not dsn:
        typer.echo("set STORE_ADMIN_DSN or STORE_INGEST_DSN", err=True)
        raise typer.Exit(code=2)
    return dsn


@app.command()
def bootstrap() -> None:
    """Apply store migrations and create/refresh roles, schemas and grants."""
    settings = AdminSettings()
    if not settings.store_admin_dsn:
        typer.echo("bootstrap requires STORE_ADMIN_DSN", err=True)
        raise typer.Exit(code=2)
    try:
        sources = load_sources(Path(settings.sources_file))
        applied = apply_store_migrations(settings.store_admin_dsn)
        ensure_roles(settings.store_admin_dsn, sources, os.environ)
    except Exception as exc:
        typer.echo(f"bootstrap failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"migrations applied: {', '.join(applied) if applied else 'none pending'}", err=True)
    typer.echo(f"roles and grants ensured for sources: {', '.join(sources)}", err=True)


@token_app.command("create")
def token_create(
    device: Annotated[str, typer.Option(help="Device name the token is bound to.")],
    source: Annotated[list[str], typer.Option(help="Allowed source; repeatable.")],
    note: Annotated[str | None, typer.Option(help="Free-text note.")] = None,
) -> None:
    """Create a token. Only the plaintext token goes to stdout."""
    settings = AdminSettings()
    known = load_sources(Path(settings.sources_file))
    unknown = [name for name in source if name not in known]
    if unknown:
        typer.echo(f"unknown source(s): {', '.join(unknown)}; known: {', '.join(known)}", err=True)
        raise typer.Exit(code=2)
    with psycopg.connect(_token_dsn()) as conn:
        token, token_id = create_token(conn, device, source, note)
    typer.echo(f"created token id {token_id} for {device} ({', '.join(source)})", err=True)
    typer.echo("store it now: it cannot be shown again", err=True)
    typer.echo(token)


@token_app.command("list")
def token_list() -> None:
    """List tokens (never hashes)."""
    query = (
        "SELECT id, device, sources, note, created_at, last_seen_at, revoked_at "
        "FROM store.device_tokens ORDER BY id"
    )
    with psycopg.connect(_token_dsn()) as conn:
        rows = conn.execute(query).fetchall()
    typer.echo("id\tdevice\tsources\tnote\tcreated_at\tlast_seen_at\trevoked_at")
    for tid, device, sources, note, created, seen, revoked in rows:
        cells = [tid, device, ",".join(sources), note or "", created, seen or "", revoked or ""]
        typer.echo("\t".join(str(c) for c in cells))


@token_app.command("revoke")
def token_revoke(token_id: Annotated[int, typer.Argument(help="Token id.")]) -> None:
    """Revoke a token by id."""
    with psycopg.connect(_token_dsn()) as conn:
        revoked = revoke_token(conn, token_id)
    if not revoked:
        typer.echo(f"token {token_id} not found or already revoked", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"revoked token {token_id}", err=True)
