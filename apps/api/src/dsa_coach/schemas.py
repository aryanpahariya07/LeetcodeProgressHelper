"""Request/response models.

Note what is *absent* from the inbound schemas: `user_id` and `source`. Identity
comes from server-side context (invariant 10) and the ingestion source is set by
the endpoint, so a client cannot claim its manual entry was captured telemetry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dsa_coach.models import (
    Blocker,
    CaptureConfidence,
    Difficulty,
    ItemRole,
    ItemStatus,
    ItemType,
    Level,
    PlanStatus,
    Resolution,
    SubmissionOutcome,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes are interpreted as UTC so SQLite and Postgres agree."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ------------------------------------------------------------------- onboarding


class OnboardingIn(BaseModel):
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    timezone: str = "UTC"
    preferred_language: str = "python"
    target_companies: list[str] = Field(default_factory=list, max_length=20)
    target_date: date | None = None
    days_per_week: Annotated[int, Field(ge=1, le=7)]
    minutes_per_day: Annotated[int, Field(ge=10, le=600)]
    self_assessed_level: Level
    approx_problems_solved: Annotated[int, Field(ge=0, le=10_000)] = 0


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    timezone: str
    preferred_language: str


class GoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_companies: list[str]
    target_date: date | None
    days_per_week: int
    minutes_per_day: int
    self_assessed_level: Level


# ------------------------------------------------------------------------- plan


class ProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    url: str
    difficulty: Difficulty
    rating: int
    rating_rd: int


class PlanItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    block_id: uuid.UUID
    sequence: int
    item_type: ItemType
    role: ItemRole
    target_minutes: int
    status: ItemStatus
    problem: ProblemOut | None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    status: PlanStatus
    summary: str
    generation_context: dict[str, Any]
    items: list[PlanItemOut]


class TodayOut(BaseModel):
    plan: PlanOut | None
    block_id: uuid.UUID | None
    items: list[PlanItemOut]
    total_target_minutes: int
    # Surfaced so the UI can say so plainly rather than implying the plan is earned.
    is_provisional: bool


class OnboardingOut(BaseModel):
    user: UserOut
    goal: GoalOut
    plan: PlanOut


# --------------------------------------------------------------------- attempts


class AttemptEventIn(BaseModel):
    """One captured attempt. `event_uuid` is client-generated and is the
    idempotency key (invariant 7)."""

    event_uuid: uuid.UUID
    problem_slug: Annotated[str, Field(min_length=1, max_length=200)]
    provider: str = "leetcode"

    submitted_at: datetime
    started_at: datetime | None = None
    language: str | None = Field(default=None, max_length=32)

    active_seconds: Annotated[int | None, Field(ge=0, le=86_400)] = None
    excluded_seconds: Annotated[int, Field(ge=0, le=86_400)] = 0
    run_count: Annotated[int, Field(ge=0, le=1000)] = 0
    submit_count: Annotated[int, Field(ge=0, le=1000)] = 1
    submission_outcome: SubmissionOutcome = SubmissionOutcome.UNKNOWN

    # Questionnaire (spec §3.2). Dismissal is valid and yields UNKNOWN.
    resolution: Resolution = Resolution.UNKNOWN
    blocker: Blocker | None = None
    confidence_cold_redo: Annotated[int | None, Field(ge=1, le=5)] = None
    hint_level_used: Annotated[int | None, Field(ge=1, le=5)] = None

    is_resolve: bool = False
    timed: bool = False
    capture_confidence: CaptureConfidence = CaptureConfidence.HIGH

    notes: str | None = Field(default=None, max_length=4000)
    raw_metadata: dict[str, Any] | None = None

    @field_validator("submitted_at", "started_at")
    @classmethod
    def _normalize_tz(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)


class AttemptBatchIn(BaseModel):
    events: Annotated[list[AttemptEventIn], Field(min_length=1)]


class EventResultOut(BaseModel):
    event_uuid: uuid.UUID
    status: Literal["accepted", "duplicate", "invalid"]
    attempt_id: uuid.UUID | None = None
    error: str | None = None


class BatchResultOut(BaseModel):
    """Partial success is the normal case, not an error (spec §14)."""

    received: int
    accepted: int
    duplicates: int
    invalid: int
    results: list[EventResultOut]
    server_received_at: datetime


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    problem: ProblemOut
    resolution: Resolution
    blocker: Blocker | None
    confidence_cold_redo: int | None
    language: str | None
    started_at: datetime | None
    submitted_at: datetime
    active_seconds: int | None
    excluded_seconds: int
    run_count: int
    submit_count: int
    submission_outcome: SubmissionOutcome
    is_resolve: bool
    timed: bool
    source: str
    capture_confidence: CaptureConfidence
    amended_at: datetime | None
    notes: str | None


# ------------------------------------------------------------------------ health


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    database: Literal["ok", "unavailable"]
    catalogue_problems: int


# -------------------------------------------------------------------- progress


class PatternReadinessOut(BaseModel):
    """Bands, never bare percentages, until calibration is proven (invariant 12)."""

    pattern_id: uuid.UUID
    slug: str
    name: str
    band: Literal["calibrating", "not_ready", "developing", "approaching", "ready"]
    calibrated: bool
    evidence_count: int
    #: The raw estimate. Present for debugging and the calibration report; the UI
    #: must render `band`, not this.
    estimate: float
    uncertainty: float


class ReadinessReportOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_version: str
    patterns: list[PatternReadinessOut]
    calibrated_count: int
    total_count: int
    disclaimer: str


class RetentionOut(BaseModel):
    tracked: int
    due: int
    lapses: int


class UnlockOut(BaseModel):
    slug: str
    unlocked: bool
    #: Unlocked only because prerequisites are unproven, not because they passed.
    #: Schedulable, but never chosen as a focus.
    provisional: bool
    blocked_by: list[str]
    unknown_prerequisites: list[str]
    reason: str


# ------------------------------------------------------------------- triggers


class TriggerBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    relevant_count: int
    material: bool
    reasons: list[str]
    outcome: Literal["no_change", "prescribed", "pending_agent"]
    explanation: str
    evaluated_at: datetime


# ------------------------------------------------------------------ placement


class PlacementOut(BaseModel):
    """Progress through placement (spec §9).

    Placement is the first practice block, not a gate — `complete: false` never
    means the user is blocked from anything.
    """

    complete: bool
    attempts: int
    max_attempts: int
    remaining: int
    covered: int
    calibrated: int
    target: int
    reason: str


class BlockResultOut(BaseModel):
    plan: PlanOut
    #: Present only while placement is still running.
    placement: PlacementOut | None = None
    focus_patterns: list[str]
    locked_patterns: list[str]
    total_minutes: int
    budget_minutes: int
    shortfalls: list[str]


# -------------------------------------------------------------------- devices


class PairingCodeOut(BaseModel):
    """Shown once, on screen, for the user to copy into the extension."""

    code: str
    expires_at: datetime
    expires_in_seconds: int


class PairingRequest(BaseModel):
    code: Annotated[str, Field(min_length=4, max_length=32)]
    device_name: Annotated[str, Field(min_length=1, max_length=120)] = "Browser extension"


class PairingResult(BaseModel):
    """The token appears here and nowhere else, ever again."""

    device_id: uuid.UUID
    device_name: str
    token: str
    scopes: list[str]


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    scopes: list[str]
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    active: bool


# ---------------------------------------------------------------------- coach


class ViolationOut(BaseModel):
    kind: str
    detail: str


class CoachRunOut(BaseModel):
    """What the coach did, and whether it was trusted.

    `used_fallback` is surfaced deliberately: a plan built without the coach is
    a normal outcome, not a hidden failure, and the UI says so (invariant 4).
    """

    run_id: uuid.UUID
    runtime: str
    model: str | None
    status: Literal["succeeded", "failed", "unavailable"]
    used_fallback: bool
    validation: Literal["accepted", "clamped", "rejected"] | None
    violations: list[ViolationOut]
    diagnosis: str | None
    message: str
    plan: PlanOut


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    agent_name: str
    trigger: str
    status: str
    runtime: str
    model: str | None
    input_summary: str
    output_summary: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
