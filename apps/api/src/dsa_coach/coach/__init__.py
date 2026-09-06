"""The judgment layer.

Selecting a runtime is the only decision made here, and it is the mechanism by
which invariant 4 holds: with no API key configured, the deterministic stub is
returned and everything downstream carries on unchanged. There is no "AI
disabled" branch anywhere else in the codebase, because there does not need to
be one.
"""

from __future__ import annotations

import logging

from dsa_coach.coach.runtime import (
    AttemptSummary,
    CoachContext,
    CoachFailure,
    CoachOutcome,
    CoachRuntime,
    PatternSummary,
)
from dsa_coach.coach.stub import StubCoachRuntime
from dsa_coach.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_runtime(settings: Settings | None = None) -> CoachRuntime:
    """The coach for this deployment.

    Falls back to the deterministic runtime — loudly in the logs, silently to the
    user — when the provider is not configured or its package is missing.
    """
    settings = settings or get_settings()

    if not settings.openai_api_key:
        logger.info("No OPENAI_API_KEY configured; using the deterministic coach.")
        return StubCoachRuntime()

    try:
        from dsa_coach.coach.openai_runtime import OpenAICoachRuntime
    except ImportError:
        logger.warning("openai-agents is not installed; using the deterministic coach instead.")
        return StubCoachRuntime()

    return OpenAICoachRuntime(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        tracing_enabled=settings.agent_tracing_enabled,
    )


__all__ = [
    "AttemptSummary",
    "CoachContext",
    "CoachFailure",
    "CoachOutcome",
    "CoachRuntime",
    "PatternSummary",
    "StubCoachRuntime",
    "build_runtime",
]
