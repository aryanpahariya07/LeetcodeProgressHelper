"""FastAPI application.

Serves the API and, when it has been built, the web app itself — so day-to-day
use is one process on one port rather than two and a dev proxy.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dsa_coach import __version__
from dsa_coach.config import Settings, get_settings
from dsa_coach.logging_setup import configure_logging
from dsa_coach.routers import (
    attempts,
    coach,
    devices,
    extension,
    health,
    onboarding,
    plan,
    progress,
    teaching,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

#: apps/api/src/dsa_coach/main.py -> apps/web/dist
WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="DSA Coach API",
        version=__version__,
        description="Adaptive DSA interview preparation. See docs/spec.md.",
    )

    # Restricted origins (spec §8). No wildcard, credentials allowed for the
    # cookie-based dashboard session Phase 2 introduces.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.middleware("http")
    async def limit_request_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Reject oversized bodies before they are parsed (spec §8)."""
        length = request.headers.get("content-length")
        if length is not None and int(length) > settings.max_request_bytes:
            return Response(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content="Request body too large.",
            )
        return await call_next(request)

    if settings.log_request_bodies:
        _install_body_logging(app, settings)

    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(health.router)
    v1.include_router(onboarding.router)
    v1.include_router(plan.router)
    v1.include_router(progress.router)
    v1.include_router(attempts.router)
    v1.include_router(extension.router)
    v1.include_router(devices.router)
    v1.include_router(coach.router)
    v1.include_router(teaching.router)
    app.include_router(v1)
    _mount_web_app(app)

    return app


def _install_body_logging(app: FastAPI, settings: Settings) -> None:
    """Log the body of every mutating request (`log_request_bodies`).

    For working out what the extension actually sent, as opposed to what it was
    supposed to send. Uvicorn's access log gives the method, path and status;
    this fills in the part that decides whether an event was usable.

    Only mutating methods are logged — a GET body is almost always absent, and
    logging one per dashboard poll would drown the interesting lines.
    """
    body_logger = logging.getLogger("dsa_coach.request")
    limit = settings.log_body_max_chars

    @app.middleware("http")
    async def log_bodies(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method not in {"POST", "PATCH", "PUT", "DELETE"}:
            return await call_next(request)

        raw = await request.body()

        # Reading the stream consumes it, so the route handler downstream would
        # otherwise receive an empty body. Put it back.
        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": raw, "more_body": False}

        # Starlette has no public API for this; assigning `_receive` is how it is done.
        request._receive = receive

        if raw:
            text = raw.decode("utf-8", errors="replace")
            if len(text) > limit:
                text = f"{text[:limit]}... [{len(text) - limit} more chars]"
        else:
            text = "<empty>"

        body_logger.info("%s %s <- %s", request.method, request.url.path, text)

        response = await call_next(request)
        body_logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response


def _mount_web_app(app: FastAPI, dist: Path = WEB_DIST) -> None:
    """Serve the built web app from the API, if it has been built.

    Absent `dist`, the API runs exactly as before — so a fresh clone works
    without a frontend build, and `npm run dev` on :5173 stays available for
    frontend work (the Vite proxy still points here).
    """
    if not (dist / "index.html").is_file():
        logger.info("No web build at %s; serving the API only.", dist)
        return

    # Hashed filenames, so these are safe to cache hard.
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        """Serve the app shell for any non-API path.

        The router is client-side, so `/progress` is a real route to the browser
        and a non-existent file to the server. Everything that is not an API
        call or a real file therefore returns `index.html` and lets React route
        it.

        An unmatched `/api/...` must NOT fall through to the shell: returning
        HTML with a 200 to a fetch that expected JSON turns a plain 404 into a
        baffling parse error at the call site.
        """
        if path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown endpoint.")

        candidate = (dist / path).resolve()
        # `resolve()` plus this check keeps `../` out of the served tree.
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)

        return FileResponse(dist / "index.html")


app = create_app()
