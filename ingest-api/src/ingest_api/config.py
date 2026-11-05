"""Process settings read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    store_admin_dsn: str | None = None
    store_ingest_dsn: str
    sources_file: str = "/etc/ingest-api/sources.yaml"
    rate_limit_per_minute: int = 120
    max_batch: int = 5000
    port: int = 8080


class AdminSettings(BaseSettings):
    """Settings for the store-admin CLI; the DSNs it needs depend on the command."""

    store_admin_dsn: str | None = None
    store_ingest_dsn: str | None = None
    sources_file: str = "/etc/ingest-api/sources.yaml"
