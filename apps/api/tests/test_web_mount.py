"""Serving the web app from the API (single-process operation).

The subtle requirement is the SPA fallback: `/progress` is a real route to the
browser and a non-existent file to the server, so it must return the app shell —
*without* swallowing unmatched API paths, because HTML with a 200 where JSON was
expected turns a plain 404 into a baffling parse error at the call site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dsa_coach.main import WEB_DIST, _mount_web_app, create_app

pytestmark = pytest.mark.skipif(
    not (WEB_DIST / "index.html").is_file(),
    reason="web app not built; run `npm run build` in apps/web",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_the_web_app_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()


@pytest.mark.parametrize("route", ["/progress", "/settings", "/log", "/onboarding"])
def test_client_routes_return_the_app_shell(client: TestClient, route: str) -> None:
    """React owns these paths; the server must not 404 them."""
    response = client.get(route)

    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()


def test_the_api_still_answers(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 200


def test_an_unknown_api_path_is_json_not_html(client: TestClient) -> None:
    """A fetch expecting JSON must get a real 404, never the app shell."""
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown endpoint."


def test_openapi_docs_are_untouched(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_a_traversal_attempt_does_not_escape_the_build(client: TestClient) -> None:
    """Anything outside dist falls back to the shell rather than leaking a file."""
    response = client.get("/../../../etc/passwd")

    assert response.status_code in {200, 404}
    assert "root:" not in response.text


def test_the_api_runs_without_a_web_build(tmp_path: Path) -> None:
    """A fresh clone has no dist. The API must not require one."""
    app = FastAPI()
    _mount_web_app(app, dist=tmp_path / "nothing")

    assert not any(getattr(r, "path", "") == "/{path:path}" for r in app.routes)
