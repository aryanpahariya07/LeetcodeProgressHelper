"""Database engine, session factory and declarative base."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, TypeDecorator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from dsa_coach.config import get_settings


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware UTC on the way in *and* on the way out.

    SQLite has no native timestamp type and hands back naive datetimes, where
    Postgres returns aware ones. Left alone, that difference leaks into any code
    doing arithmetic on stored timestamps — and it surfaces as a crash only on
    SQLite, which is exactly where the tests run.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, Any] = {"echo": False, "future": True}
        if settings.is_sqlite:
            # SQLite needs foreign key enforcement turned on per connection;
            # see the event hook below.
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(settings.database_url, **kwargs)
        _install_sqlite_pragmas(_engine)
    return _engine


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """SQLite does not enforce foreign keys unless asked, per connection."""
    if not get_settings().is_sqlite:
        return

    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session with commit/rollback handling."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def reset_engine_state() -> None:
    """Test hook: drop cached engine/session factory so settings can change."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
