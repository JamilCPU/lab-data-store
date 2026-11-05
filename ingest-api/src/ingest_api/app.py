"""FastAPI app: bearer-token auth, rate limiting, validation, upsert."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import psycopg
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel

from ingest_api.config import Settings
from ingest_api.ratelimit import RateLimiter
from ingest_api.sources import EventValidationError, load_sources, validate_events
from ingest_api.store_migrations import apply_store_migrations, ensure_roles
from ingest_api.tokens import TokenInfo, lookup_token, touch_token
from ingest_api.writer import write_events

TOUCH_INTERVAL_SECONDS = 60.0
logger = logging.getLogger(__name__)
_UNAVAILABLE = (psycopg.OperationalError, PoolTimeout)


def _stamp_device(events: list[dict[str, Any]], device: str) -> list[dict[str, Any]] | None:
    """Fill a missing per-event device from the token; None if any event names another."""
    stamped = []
    for event in events:
        if not isinstance(event, dict):
            stamped.append(event)  # rejected later by validation, with its index
            continue
        current = event.get("device")
        if current is not None and current != device:
            return None
        stamped.append({**event, "device": device})
    return stamped


class IngestRequest(BaseModel):
    source: str
    device: str
    events: list[dict[str, Any]]


def require_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> TokenInfo:
    scheme, _, token = (authorization or "").partition(" ")
    info = None
    if scheme.lower() == "bearer" and token.strip():
        try:
            with request.app.state.pool.connection() as conn:
                info = lookup_token(conn, token.strip())
        except _UNAVAILABLE as exc:
            logger.error("token lookup failed: %s", exc)
            raise HTTPException(status_code=503, detail="database unavailable") from exc
    if info is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return info


def _touch_throttled(app: FastAPI, token_id: int) -> None:
    last = app.state.last_touch.get(token_id)
    now = time.monotonic()
    if last is not None and now - last < TOUCH_INTERVAL_SECONDS:
        return
    with app.state.pool.connection() as conn:
        touch_token(conn, token_id)
    app.state.last_touch[token_id] = now


def _database_error_response(exc: psycopg.Error) -> JSONResponse:
    """Data errors (class 22/23) are the client's to fix (422); anything else is ours (503)."""
    sqlstate = exc.sqlstate or ""
    logger.error("database error (sqlstate %s): %s", sqlstate or "none", exc.diag.message_primary)
    if sqlstate[:2] in ("22", "23"):
        message = f"database rejected the batch (sqlstate {sqlstate})"
        return JSONResponse({"index": None, "field": None, "message": message}, status_code=422)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


def create_app(settings: Settings) -> FastAPI:
    sources = load_sources(Path(settings.sources_file))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.store_admin_dsn:  # dev/test convenience; deployments bootstrap separately
            apply_store_migrations(settings.store_admin_dsn)
            ensure_roles(settings.store_admin_dsn, sources, os.environ)
        pool = ConnectionPool(settings.store_ingest_dsn, min_size=1, max_size=10, open=True)
        app.state.pool = pool
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="ingest-api", lifespan=lifespan)
    app.state.sources = sources
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.last_touch = {}

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        try:
            with request.app.state.pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            logger.exception("readiness check failed")
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    @app.post("/v1/ingest", response_model=None)
    def ingest(
        body: IngestRequest, token: Annotated[TokenInfo, Depends(require_token)]
    ) -> JSONResponse | dict[str, int]:
        if not app.state.limiter.allow(str(token.id)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        if len(body.events) > settings.max_batch:
            raise HTTPException(status_code=413, detail=f"batch exceeds {settings.max_batch}")
        spec = sources.get(body.source)
        if (
            spec is None
            or not spec.ingestable
            or body.source not in token.sources
            or body.device != token.device
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        raw_events = body.events
        if "device" in spec.columns:
            stamped = _stamp_device(raw_events, token.device)
            if stamped is None:
                raise HTTPException(status_code=403, detail="forbidden")
            raw_events = stamped
        try:
            events = validate_events(spec, raw_events)
        except EventValidationError as exc:
            return JSONResponse(
                {"index": exc.index, "field": exc.field, "message": exc.message},
                status_code=422,
            )
        try:
            with app.state.pool.connection() as conn:
                result = write_events(conn, spec, events)
        except _UNAVAILABLE as exc:
            logger.error("database unavailable during ingest: %s", exc)
            return JSONResponse({"detail": "database unavailable"}, status_code=503)
        except psycopg.Error as exc:
            return _database_error_response(exc)
        try:
            _touch_throttled(app, token.id)
        except Exception as exc:  # the write is already committed; never fail the response
            logger.warning("last_seen touch failed after a committed write: %s", type(exc).__name__)
        return {
            "accepted": len(events),
            "inserted": result.inserted,
            "updated": result.updated,
        }

    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
