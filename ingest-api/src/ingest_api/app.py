"""FastAPI app: bearer-token auth, rate limiting, validation, upsert."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from ingest_api.config import Settings
from ingest_api.ratelimit import RateLimiter
from ingest_api.sources import EventValidationError, load_sources, validate_events
from ingest_api.store_migrations import apply_store_migrations, ensure_roles
from ingest_api.tokens import TokenInfo, lookup_token, touch_token
from ingest_api.writer import write_events

TOUCH_INTERVAL_SECONDS = 60.0


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
        with request.app.state.pool.connection() as conn:
            info = lookup_token(conn, token.strip())
    if info is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return info


def _touch_throttled(app: FastAPI, token_id: int) -> None:
    last = app.state.last_touch.get(token_id)
    now = time.monotonic()
    if last is not None and now - last < TOUCH_INTERVAL_SECONDS:
        return
    app.state.last_touch[token_id] = now
    with app.state.pool.connection() as conn:
        touch_token(conn, token_id)


def create_app(settings: Settings) -> FastAPI:
    sources = load_sources(Path(settings.sources_file))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        try:
            events = validate_events(spec, body.events)
        except EventValidationError as exc:
            return JSONResponse(
                {"index": exc.index, "field": exc.field, "message": exc.message},
                status_code=422,
            )
        with app.state.pool.connection() as conn:
            result = write_events(conn, spec, events)
        _touch_throttled(app, token.id)
        return {
            "accepted": len(events),
            "inserted": result.inserted,
            "updated": result.updated,
        }

    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
