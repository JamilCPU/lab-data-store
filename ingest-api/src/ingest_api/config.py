"""Process settings read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    store_admin_dsn: str
    store_ingest_dsn: str
    sources_file: str = "/etc/ingest-api/sources.yaml"
    rate_limit_per_minute: int = 120
    max_batch: int = 5000
    port: int = 8080
