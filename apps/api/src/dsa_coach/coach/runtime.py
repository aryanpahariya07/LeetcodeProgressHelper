"""The coach runtime boundary.

Everything the rest of the system knows about the AI provider is in this file.
`CoachRuntime` is deliberately narrow: given the evidence, return a block shape
or say why you could not. It cannot write to the database, cannot name a problem,
and cannot be asked for anything else.

Two implementations exist. `OpenAICoachRuntime` is the real one. `StubCoachRuntime`
is a deterministic double used in tests — and, notably, it is also what runs when
no API key is configured, which is how invariant 4 stays true without a special
case anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from dsa_coach.mechanism.prescription import Prescription


class CoachFailure(StrEnum):
    """Why a run produced nothing. Determines whether a retry is worth it."""

    #: No API key, or the provider package is missing. Not an error.
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    #: The model returned something that is not a valid prescription at all.
    INVALID_OUTPUT = "invalid_output"

    @property
    def retryable(self) -> bool:
        return self in {
            CoachFailure.TIMEOUT,
            CoachFailure.RATE_LIMITED,
            CoachFailure.PROVIDER_ERROR,
        }


@dataclass(frozen=True)
class PatternSummary:
    """One pattern, as the coach is allowed to see it."""

    pattern_id: UUID
    slug: str
    name: str
    band: str
    calibrated: bool
    evidence_count: int
    unlocked: bool
    provisional: bool


@dataclass(frozen=True)
class AttemptSummary:
    attempt_id: UUID
    problem_slug: str
    problem_rating: int
    patterns: tuple[str, ...]
    resolution: str
    blocker: str | None
    submitted_at: datetime
    active_seconds: int | None


@dataclass(frozen=True)
class CoachContext:
    """Everything the coach may reason from.

    Assembled server-side and passed as typed run context, never as tool
    arguments the model could forge (invariant 10). There is no `user_id` field
    the model can see, because it has no business knowing one.
    """

    level: str
    days_per_week: int
    minutes_per_day: int
    target_companies: tuple[str, ...]
    patterns: tuple[PatternSummary, ...]
    recent_attempts: tuple[AttemptSummary, ...]
    retention_due: int
    retention_lapses: int
    trigger_reasons: tuple[str, ...]
    max_size: int
    rating_range: tuple[int, int]


@dataclass(frozen=True)
class CoachOutcome:
    """What a run produced. Exactly one of `prescription` or `failure` is set."""

    prescription: Prescription | None = None
    failure: CoachFailure | None = None
    error_detail: str | None = None
    model: str | None = None
    trace_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.prescription is not None


class CoachRuntime(Protocol):
    """Ask the coach for the shape of the next block."""

    name: str

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        """Return a block shape, or a failure explaining why not.

        Must not raise. A runtime that throws would take the scheduler down with
        it, and the scheduler is required to keep working (invariant 4).
        """
        ...
