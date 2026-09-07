"""The judgment layer.

Selecting a runtime is the only decision made here, and it is the mechanism by
which invariant 4 holds: when the configured provider is unusable, the
deterministic stub is returned and everything downstream carries on unchanged.
There is no "AI disabled" branch anywhere else in the codebase, because there
does not need to be one.
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

    Dispatches on `coach_runtime`, and falls back to the deterministic runtime —
    loudly in the logs, silently to the user — when the configured provider is
    unusable. That fallback is how invariant 4 holds: there is no "AI disabled"
    branch anywhere else, because the absence of a provider simply selects a
    different runtime.
    """
    settings = settings or get_settings()
    choice = settings.coach_runtime.strip().lower()

    if choice == "codex":
        try:
            from dsa_coach.coach.codex_runtime import CodexCoachRuntime
        except ImportError:
            logger.warning("openai-codex is not installed; using the deterministic coach instead.")
            return StubCoachRuntime()
        return CodexCoachRuntime(model=settings.coach_model)

    if choice == "openai":
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

    if choice != "stub":
        logger.warning("Unknown coach_runtime %r; using the deterministic coach.", choice)
    return StubCoachRuntime()


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
