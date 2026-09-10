"""Logging configuration and request-body logging.

The load-bearing test here is `test_the_handler_still_receives_the_body`.
Reading a request body in middleware consumes the stream, so a naive
implementation logs the payload correctly and then hands the route an empty
one — turning a debugging aid into a bug that only appears when it is switched
on. That is the failure this file exists to catch.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from dsa_coach.config import Settings
from dsa_coach.logging_setup import configure_logging
from dsa_coach.main import _install_body_logging


def app_with_logging(**settings_kwargs: object) -> FastAPI:
    # `_env_file=None` so the developer's own .env cannot decide what these
    # tests are testing. This repo's .env sets both LOG_REQUEST_BODIES and
    # LOG_REQUEST_READS for live debugging, and each one in turn has silently
    # inverted a test here. A helper that builds settings for a test should
    # take them from the test, not from the machine it runs on.
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        log_request_bodies=True,
        **settings_kwargs,  # type: ignore[arg-type]
    )
    app = FastAPI()
    _install_body_logging(app, settings)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {"received": await request.json()}

    @app.get("/plain")
    async def plain() -> dict[str, str]:
        return {"ok": "yes"}

    return app


class TestBodyLogging:
    def test_the_handler_still_receives_the_body(self) -> None:
        # Middleware consumes the stream. If it is not put back, this returns
        # an empty body and every POST in the app breaks.
        client = TestClient(app_with_logging())

        response = client.post("/echo", json={"problem_slug": "two-sum"})

        assert response.status_code == 200
        assert response.json() == {"received": {"problem_slug": "two-sum"}}

    def test_the_payload_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        client = TestClient(app_with_logging())

        with caplog.at_level(logging.INFO, logger="dsa_coach.request"):
            client.post("/echo", json={"problem_slug": "two-sum"})

        assert "two-sum" in caplog.text
        assert "POST /echo" in caplog.text

    def test_the_status_is_logged_too(self, caplog: pytest.LogCaptureFixture) -> None:
        client = TestClient(app_with_logging())

        with caplog.at_level(logging.INFO, logger="dsa_coach.request"):
            client.post("/echo", json={"a": 1})

        assert "-> 200" in caplog.text

    def test_a_long_body_is_truncated(self, caplog: pytest.LogCaptureFixture) -> None:
        # A consented code snapshot can be large; one must not swamp the file.
        client = TestClient(app_with_logging(log_body_max_chars=100))

        with caplog.at_level(logging.INFO, logger="dsa_coach.request"):
            client.post("/echo", json={"code": "x" * 5000})

        assert "more chars]" in caplog.text
        assert "x" * 5000 not in caplog.text

    def test_reads_are_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        # The dashboard polls; logging every GET would bury the useful lines.
        client = TestClient(app_with_logging())

        with caplog.at_level(logging.INFO, logger="dsa_coach.request"):
            client.get("/plain")

        assert "/plain" not in caplog.text

    def test_a_non_utf8_body_is_logged_without_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Arbitrary bytes must not blow up inside the logger.

        Decoding is lenient (`errors="replace"`), so undecodable bytes become
        replacement characters and the request proceeds to fail — or succeed —
        on its own merits. What matters is that the failure never originates in
        the logging middleware.
        """
        client = TestClient(app_with_logging())

        # UnicodeDecodeError is raised by the route's own `request.json()`,
        # not by the logging middleware.
        with (
            caplog.at_level(logging.INFO, logger="dsa_coach.request"),
            pytest.raises(UnicodeDecodeError),
        ):
            client.post("/echo", content=b"\xff\xfe not json")

        assert "POST /echo" in caplog.text
        assert "not json" in caplog.text

    def test_it_is_off_unless_asked_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Checking the field's actual default, not whatever a developer's local
        # .env happens to say — Settings() reads .env the same way production
        # does, and this repo's own .env sets LOG_REQUEST_BODIES=true for
        # exactly the live-debugging workflow this file is testing around.
        # `_env_file=None` is pydantic-settings' documented way to skip it for
        # one instance without touching the class config.
        monkeypatch.delenv("LOG_REQUEST_BODIES", raising=False)

        assert Settings(_env_file=None).log_request_bodies is False  # type: ignore[call-arg]


class TestConfigureLogging:
    def test_it_honours_the_configured_level(self) -> None:
        configure_logging(Settings(log_level="WARNING"))

        assert logging.getLogger("dsa_coach").level == logging.WARNING

    def test_reload_does_not_duplicate_handlers(self) -> None:
        # `--reload` calls create_app repeatedly. Appending handlers each time
        # would print every line once per restart.
        configure_logging(Settings())
        first = len(logging.getLogger("dsa_coach").handlers)
        configure_logging(Settings())

        assert len(logging.getLogger("dsa_coach").handlers) == first

    def test_it_writes_to_the_log_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "logs" / "api.log"

        configure_logging(Settings(log_file=str(path)))
        logging.getLogger("dsa_coach.test").info("hello from the test")

        # The parent directory is created rather than requiring it to exist.
        assert path.is_file()
        assert "hello from the test" in path.read_text(encoding="utf-8")

    def test_no_log_file_means_console_only(self) -> None:
        configure_logging(Settings(log_file=None))
        handlers = logging.getLogger("dsa_coach").handlers

        assert len(handlers) == 1


class TestReadLogging:
    """`log_request_reads` — the escape hatch for "is anything arriving at all"."""

    def test_reads_are_logged_when_asked_for(self, caplog: pytest.LogCaptureFixture) -> None:
        client = TestClient(app_with_logging(log_request_reads=True))

        with caplog.at_level(logging.INFO, logger="dsa_coach.request"):
            client.get("/plain")

        assert "GET /plain" in caplog.text

    def test_it_is_off_by_default(self) -> None:
        # Existing as a setting is the point: it kept being done by commenting
        # out the filter in main.py, which broke the test above it every time.
        assert Settings(_env_file=None).log_request_reads is False  # type: ignore[call-arg]
