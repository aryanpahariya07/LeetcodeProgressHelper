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
    # Null for a problem seen in the wild but never catalogued (§3.1). The UI
    # shows "unrated" rather than a number nobody measured.
    difficulty: Difficulty | None
    rating: int | None
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

    #: The submitted source, sent only while code capture is consented.
    #:
    #: Whether it is *stored* is decided server-side regardless of what arrives
    #: here (invariant 9): the extension asks `/extension/config` for
    #: permission, but a stale answer, a revoked consent or a modified client
    #: must not be able to persist code the user has not agreed to keep. So the
    #: server re-checks before writing, and silently drops it otherwise.
    code: Annotated[str | None, Field(max_length=200_000)] = None

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


class AttemptAmendIn(BaseModel):
    """A correction to an attempt already recorded (spec §3.3).

    Only the questionnaire answers are amendable. Telemetry — timings, run
    counts, the judge's verdict — is observed fact and is not editable after
    the event; changing it by hand would be fabrication (invariant 5).

    Every field is optional; omitting one leaves it alone. `None` is not usable
    as "clear this", because it is indistinguishable from "not supplied" —
    `clear_blocker` exists for that, since removing a wrongly-reported blocker
    is a real correction.
    """

    resolution: Resolution | None = None
    blocker: Blocker | None = None
    clear_blocker: bool = False
    confidence_cold_redo: Annotated[int, Field(ge=1, le=5)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None


class AmendmentResultOut(BaseModel):
    attempt: AttemptOut
    #: What the readiness replay did, so a correction is never silently inert.
    attempts_replayed: int
    changed_fields: list[str]


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


# ------------------------------------------------------------------- teaching


class ConsentStateOut(BaseModel):
    """What the user has decided, and the exact words they decided against."""

    decision: Literal["once", "always", "never"] | None
    needs_prompt: bool
    disclosure: str
    version: str
    stored_snippets: int


class ConsentIn(BaseModel):
    decision: Literal["once", "always", "never"]


class ConsentResultOut(BaseModel):
    decision: Literal["once", "always", "never"]
    #: Choosing `never` deletes what was already stored, and says how much.
    deleted_snippets: int
    message: str


class HintIn(BaseModel):
    problem_slug: Annotated[str, Field(min_length=1, max_length=200)]
    #: A ceiling, not a jump — you always get the next level up from what you
    #: have seen, never more (spec §7.3).
    level: Annotated[int | None, Field(ge=1, le=5)] = None


class TeachingOut(BaseModel):
    ok: bool
    kind: Literal["hint", "diagnosis", "review", "mock"]
    text: str
    hint_level: int | None = None
    level_description: str | None = None
    #: True when the answer was produced without the user's code.
    degraded: bool = False
    runtime: str = ""
    cited_attempt_ids: list[uuid.UUID] = Field(default_factory=list)
    reason: str = ""


class DiagnoseIn(BaseModel):
    attempt_id: uuid.UUID
    #: Sent only for this request unless consent is `always`.
    code: str | None = Field(default=None, max_length=100_000)


class MockTurnIn(BaseModel):
    problem_slug: Annotated[str, Field(min_length=1, max_length=200)]
    message: Annotated[str, Field(min_length=1, max_length=8000)]


class TeachingExchangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    hint_level: int | None
    content: str
    degraded: bool
    created_at: datetime


class ComplexityOut(BaseModel):
    """Presented as "looks like", never as fact — it reads syntax, not meaning."""

    estimate: str
    confidence: str
    signals: list[str]


# ------------------------------------------------------------------- snapshots


class SnapshotIn(BaseModel):
    """One Run or Submit's source, sent as it happens (spec §3.6)."""

    #: Client-generated idempotency key (invariant 7): a retried send must not
    #: store the same run twice.
    snapshot_uuid: uuid.UUID
    problem_slug: Annotated[str, Field(min_length=1, max_length=200)]
    provider: str = "leetcode"
    kind: Literal["run", "submit"]
    language: Annotated[str, Field(max_length=32)] | None = None
    code: Annotated[str, Field(min_length=1, max_length=200_000)]
    captured_at: datetime


class SnapshotBatchIn(BaseModel):
    snapshots: Annotated[list[SnapshotIn], Field(min_length=1, max_length=50)]


class SnapshotResultOut(BaseModel):
    stored: int
    #: How many were dropped because code capture is not consented. Reported
    #: rather than silently swallowed — an extension sending code that is never
    #: kept should be able to tell (invariant 9).
    refused_no_consent: int


class UnfinishedProblemOut(BaseModel):
    problem_id: uuid.UUID
    slug: str
    title: str
    url: str
    run_count: int
    submit_count: int
    first_seen_at: datetime
    last_seen_at: datetime


class AbandonResultOut(BaseModel):
    problem_id: uuid.UUID
    conclusion_id: uuid.UUID
    #: Reported so giving up still shows what the attempt consisted of.
    runs_recorded: int
