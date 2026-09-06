"""Migrations must produce exactly the schema the models describe.

Invariant 11: migrations for every schema change, no silent drift. This test is
what makes that invariant enforceable rather than aspirational — if a model gains
a table or column without a migration, it fails.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

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


def _run_alembic_to(db_url: str, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=API_ROOT,
        env={**_clean_env(), "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_migrations_apply_to_a_database_that_already_has_rows(tmp_path: Path) -> None:
    """Fresh-database migrations prove almost nothing.

    A NOT NULL column added without a server_default succeeds on an empty table
    and fails on a populated one. Only a migration run against real rows catches
    it, which is exactly how this test was earned.
    """
    path = tmp_path / "populated.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"

    # Stop at the first revision, insert a row, then continue to head.
    first = _run_alembic_to(url, "855d289b60c2")
    assert first.returncode == 0, first.stderr

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, display_name, timezone, preferred_language,"
                    " created_at, updated_at) VALUES ('u1', 'T', 'UTC', 'python',"
                    " '2026-01-01', '2026-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO catalogue_sources (id, name, license, version,"
                    " transformation_notes, known_limitations, imported_at)"
                    " VALUES ('s1', 'src', 'x', '1', '', '', '2026-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO problems (id, provider, external_id, slug, title, url,"
                    " difficulty, rating, rating_rd, rating_source, is_active, created_at)"
                    " VALUES ('p1', 'leetcode', '1', 'two-sum', 'Two Sum', 'http://x',"
                    " 'easy', 1200, 300, 'manual', 1, '2026-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO attempts (id, user_id, problem_id, resolution,"
                    " submitted_at, excluded_seconds, run_count, submit_count,"
                    " submission_outcome, is_resolve, timed, source, capture_confidence,"
                    " created_at) VALUES ('a1', 'u1', 'p1', 'independent', '2026-01-02',"
                    " 0, 0, 1, 'accepted', 0, 0, 'manual', 'high', '2026-01-02')"
                )
            )
    finally:
        engine.dispose()

    rest = _run_alembic_to(url, "head")
    assert rest.returncode == 0, rest.stderr

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as conn:
            value = conn.execute(
                text("SELECT counts_toward_trigger FROM attempts WHERE id = 'a1'")
            ).scalar_one()
    finally:
        engine.dispose()

    # Backfilled as not counting: relevance cannot be reconstructed after the
    # fact, and inventing it would be fabricating evidence.
    assert value == 0


def test_no_schema_drift_between_models_and_migrations(tmp_path: Path) -> None:
    """Invariant 11, enforced properly.

    Comparing table and column *names* misses type changes, nullability and
    server defaults. `alembic check` diffs the full metadata, so a model edit
    without a matching migration fails here rather than in production.
    """
    path = tmp_path / "drift.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"

    upgrade = _run_alembic(url)
    assert upgrade.returncode == 0, upgrade.stderr

    check = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=API_ROOT,
        env={**_clean_env(), "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert check.returncode == 0, (
        "models and migrations have drifted:\n" + check.stdout + check.stderr
    )
