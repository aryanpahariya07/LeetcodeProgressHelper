"""Test fixtures.

Each test gets a fresh SQLite file, the full schema, and the seeded catalogue.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import models  # noqa: F401  (registers tables on Base)
from dsa_coach.catalogue import import_catalogue
from dsa_coach.config import get_settings
from dsa_coach.db import Base, get_engine, get_session_factory, reset_engine_state
from dsa_coach.main import create_app


@pytest.fixture
async def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Path, None]:
    path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    # The suite must never reach a real provider: it would be slow,
    # non-deterministic, and would spend the developer's ChatGPT quota on every
    # run. This used to hold only because no API key was configured in CI —
    # which stopped being true the moment the default runtime became one that
    # authenticates without a key. Pin it explicitly instead.
    #
    # Tests that need coach behaviour inject a `ScriptedRuntime`; the one that
    # needs the real thing is a manual check, not part of this suite.
    monkeypatch.setenv("COACH_RUNTIME", "stub")
    get_settings.cache_clear()
    reset_engine_state()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield path

    await engine.dispose()
    reset_engine_state()
    get_settings.cache_clear()


@pytest.fixture
async def session(db_path: Path) -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as s:
        yield s


@pytest.fixture
async def seeded(session: AsyncSession) -> AsyncSession:
    await import_catalogue(session)
    await session.commit()
    return session


@pytest.fixture
async def client(seeded: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c


@pytest.fixture
def onboarding_payload() -> dict[str, object]:
    return {
        "display_name": "Test User",
        "timezone": "Asia/Kolkata",
        "preferred_language": "python",
        "target_companies": ["Example Corp"],
        "days_per_week": 5,
        "minutes_per_day": 60,
        "self_assessed_level": "beginner",
        "approx_problems_solved": 12,
    }


@pytest.fixture
async def onboarded(client: AsyncClient, onboarding_payload: dict[str, object]) -> AsyncClient:
    response = await client.post("/onboarding", json=onboarding_payload)
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
async def extension_token(onboarded: AsyncClient) -> str:
    """Pair an extension device and return its token.

    The batch endpoint requires the `extension:ingest` scope, so anything
    exercising it must hold a real device credential.
    """
    code = (await onboarded.post("/devices/pairing-code")).json()["code"]
    response = await onboarded.post(
        "/extension/pair", json={"code": code, "device_name": "Test browser"}
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["token"]
    return token


@pytest.fixture
def extension_auth(extension_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {extension_token}"}
