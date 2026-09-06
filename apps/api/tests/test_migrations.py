"""Migrations must produce exactly the schema the models describe.

Invariant 11: migrations for every schema change, no silent drift. This test is
what makes that invariant enforceable rather than aspirational — if a model gains
a table or column without a migration, it fails.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from dsa_coach.db import Base

API_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(db_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env={
            **_clean_env(),
            "DATABASE_URL": db_url,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _clean_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    return env


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    path = tmp_path / "migrated.db"
    result = _run_alembic(f"sqlite+aiosqlite:///{path.as_posix()}")
    assert result.returncode == 0, result.stderr
    return path


def test_migration_creates_every_model_table(migrated_db: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_db.as_posix()}")
    try:
        actual = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert actual == set(Base.metadata.tables)


def test_migration_creates_every_model_column(migrated_db: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_db.as_posix()}")
    try:
        inspector = inspect(engine)
        drift: dict[str, set[str]] = {}
        for name, table in Base.metadata.tables.items():
            actual = {c["name"] for c in inspector.get_columns(name)}
            expected = set(table.columns.keys())
            if actual != expected:
                drift[name] = expected ^ actual
    finally:
        engine.dispose()

    assert drift == {}


def test_event_uuid_uniqueness_is_enforced_by_the_database(migrated_db: Path) -> None:
    """The idempotency guarantee must not rely on application code alone."""
    engine = create_engine(f"sqlite:///{migrated_db.as_posix()}")
    try:
        indexes = inspect(engine).get_indexes("attempt_events")
    finally:
        engine.dispose()

    unique_on_event_uuid = [
        i for i in indexes if i["unique"] and i["column_names"] == ["event_uuid"]
    ]
    assert unique_on_event_uuid, f"no unique index on event_uuid; found {indexes}"
