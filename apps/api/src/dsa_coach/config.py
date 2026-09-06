"""Application settings.

Tuning thresholds that are *product hypotheses* live in `tuning.py`, not here.
This module holds deployment configuration only.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage. Postgres-compatible schema; SQLite locally (spec §13).
    database_url: str = "sqlite+aiosqlite:///./dsa_coach.db"

    # Bootstrap secret for the pairing flow (spec §5). Server-only: never shipped
    # to the web app or the extension. Phase 2 builds the real pairing exchange.
    app_secret: str = "dev-only-change-me"

    # CORS. Restricted origins (spec §8).
    web_origin: str = "http://localhost:5173"

    # Extension batch ingestion limits (spec §14).
    max_batch_size: int = 100
    max_request_bytes: int = 1_048_576

    log_level: str = "INFO"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
