"""FastAPI application."""

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from dsa_coach import __version__
from dsa_coach.config import get_settings
from dsa_coach.routers import attempts, extension, health, onboarding, plan, progress

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()

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

    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(health.router)
    v1.include_router(onboarding.router)
    v1.include_router(plan.router)
    v1.include_router(progress.router)
    v1.include_router(attempts.router)
    v1.include_router(extension.router)
    app.include_router(v1)

    return app


app = create_app()
