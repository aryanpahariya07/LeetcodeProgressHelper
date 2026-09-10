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

    # --- runtime coach (spec §2).
    #
    # `codex` authenticates with a ChatGPT account rather than an API key, which
    # is why it is the default for a local single-user install. Deploying this
    # would require a separate auth, billing and terms review.
    #
    # Whatever is selected, an unavailable coach falls back to the deterministic
    # runtime and the product stays fully usable (invariant 4).
    coach_runtime: str = "codex"
    #: Optional model override. Codex picks a sensible default when unset.
    coach_model: str | None = None

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    agent_tracing_enabled: bool = False

    # --- logging
    log_level: str = "INFO"

    #: Optional file to mirror logs into. Console output continues either way.
    log_file: str | None = None

    #: Log the body of every mutating request.
    #:
    #: Off by default for a practical reason rather than a policy one: with code
    #: capture consented, a body carries a full source snapshot, and writing one
    #: per submission buries the lines worth reading. Turn it on while debugging
    #: the extension, when seeing exactly what arrived is the whole point.
    log_request_bodies: bool = False

    #: Truncation for a logged body, so one large snapshot cannot swamp the file.
    #: Raise it if a payload you need is being cut off.
    log_body_max_chars: int = 4000

    #: Log reads as well as writes.
    #:
    #: Off by default because the dashboard polls, and a wall of `GET /today`
    #: buries the one `POST /attempts` you are usually looking for. But when the
    #: question is "is the extension reaching the server *at all*", seeing every
    #: request is exactly what you want — so this is a setting rather than
    #: something to comment out in `main.py`, which is how it kept getting done.
    log_request_reads: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
