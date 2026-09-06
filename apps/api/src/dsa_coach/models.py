"""ORM models.

Phase 0 subset of the data model in spec §10. Tables not needed yet (devices,
consents, pattern_ratings, review_schedule, prescriptions, agent_runs, ...) arrive
with the phase that uses them, each behind its own migration.

Conventions:
  - UUID primary keys (`sa.Uuid` — native on Postgres, CHAR(32) on SQLite).
  - Timezone-aware timestamps, set Python-side so SQLite behaves like Postgres.
  - Enums stored as VARCHAR + CHECK, so both dialects agree.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dsa_coach.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------- enums


class Level(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class RatingSource(StrEnum):
    """Provenance of `problems.rating` (spec §11).

    `manual` ratings are approximate curator estimates and MUST carry a high
    `rating_rd` so the readiness model discounts them accordingly.
    """

    MANUAL = "manual"
    CONTEST_DERIVED = "contest_derived"


class Resolution(StrEnum):
    """How the attempt was resolved (spec §3.2). Questionnaire-supplied."""

    INDEPENDENT = "independent"
    AFTER_HINT = "after_hint"
    AFTER_EDITORIAL = "after_editorial"
    FAILED = "failed"
    UNKNOWN = "unknown"  # questionnaire dismissed — weak evidence, not a failure


class Blocker(StrEnum):
    PATTERN_NOT_RECOGNIZED = "pattern_not_recognized"
    PATTERN_KNOWN_IMPL_FAILED = "pattern_known_impl_failed"
    EDGE_CASES = "edge_cases"
    COMPLEXITY = "complexity"
    DATA_STRUCTURE_CHOICE = "data_structure_choice"
    LANGUAGE_API = "language_api"
    MISREAD_PROBLEM = "misread_problem"


class SubmissionOutcome(StrEnum):
    ACCEPTED = "accepted"
    WRONG_ANSWER = "wrong_answer"
    RUNTIME_ERROR = "runtime_error"
    COMPILE_ERROR = "compile_error"
    TLE = "tle"
    UNKNOWN = "unknown"


class AttemptSource(StrEnum):
    EXTENSION = "extension"
    MANUAL = "manual"
    PUBLIC_SYNC = "public_sync"


class CaptureConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


class PlanStatus(StrEnum):
    PROVISIONAL = "provisional"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class ItemType(StrEnum):
    PRACTICE = "practice"
    RESOLVE = "resolve"
    ASSESSMENT = "assessment"
    LEARN = "learn"


class ItemRole(StrEnum):
    WEAKNESS = "weakness"
    INTERLEAVED = "interleaved"
    RETENTION = "retention"


class ItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


def _enum(e: type[StrEnum], name: str) -> Enum:
    """VARCHAR + CHECK rather than a native DB enum, for dialect portability."""
    return Enum(e, name=name, native_enum=False, values_callable=lambda x: [i.value for i in x])


# --------------------------------------------------------------------------- user


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    preferred_language: Mapped[str] = mapped_column(String(32), default="python")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    goals: Mapped[list[UserGoal]] = relationship(back_populates="user")


class UserGoal(Base):
    __tablename__ = "user_goals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Free-text list; no company difficulty *numbers* are stored anywhere (spec §12).
    target_companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_per_week: Mapped[int] = mapped_column(Integer)
    minutes_per_day: Mapped[int] = mapped_column(Integer)
    self_assessed_level: Mapped[Level] = mapped_column(_enum(Level, "level"))
    approx_problems_solved: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="goals")

    __table_args__ = (
        CheckConstraint("days_per_week BETWEEN 1 AND 7", name="ck_goal_days_per_week"),
        CheckConstraint("minutes_per_day BETWEEN 10 AND 600", name="ck_goal_minutes"),
    )


# ----------------------------------------------------------------------- catalogue


class CatalogueSource(Base):
    """Provenance for every imported catalogue dataset (spec §11).

    Nothing enters `problems` without a row here recording where it came from,
    under what licence, and what is known to be wrong with it.
    """

    __tablename__ = "catalogue_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    license: Mapped[str] = mapped_column(String(200))
    snapshot_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[str] = mapped_column(String(64))
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transformation_notes: Mapped[str] = mapped_column(Text, default="")
    known_limitations: Mapped[str] = mapped_column(Text, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Pattern(Base):
    """A transferable solving technique — the unit the skill model runs on (spec §6.1)."""

    __tablename__ = "patterns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    # Ordering hint for curriculum display only. Not a scheduler input.
    foundational: Mapped[bool] = mapped_column(Boolean, default=False)


class PatternPrerequisite(Base):
    """Explicit DAG edge (spec §6.4). `parent` containment is NOT a prerequisite."""

    __tablename__ = "pattern_prerequisites"

    pattern_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE"), primary_key=True
    )
    requires_pattern_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE"), primary_key=True
    )
    strength: Mapped[float] = mapped_column(Float, default=1.0)

    __table_args__ = (
        CheckConstraint("pattern_id != requires_pattern_id", name="ck_prereq_not_self"),
        CheckConstraint("strength > 0 AND strength <= 1", name="ck_prereq_strength"),
    )


class Problem(Base):
    __tablename__ = "problems"

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), default="leetcode")
    external_id: Mapped[str] = mapped_column(String(64))
    slug: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(500))
    difficulty: Mapped[Difficulty] = mapped_column(_enum(Difficulty, "difficulty"))
    # Numeric rating on an Elo-like scale. `rating_rd` is its uncertainty:
    # manual estimates carry a deliberately high RD (spec §6.2, §11).
    rating: Mapped[int] = mapped_column(Integer)
    rating_rd: Mapped[int] = mapped_column(Integer, default=350)
    rating_source: Mapped[RatingSource] = mapped_column(_enum(RatingSource, "rating_source"))
    catalogue_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalogue_sources.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    patterns: Mapped[list[ProblemPattern]] = relationship(back_populates="problem")

    __table_args__ = (
        UniqueConstraint("provider", "slug", name="uq_problem_provider_slug"),
        UniqueConstraint("provider", "external_id", name="uq_problem_provider_external_id"),
        CheckConstraint("rating BETWEEN 800 AND 3500", name="ck_problem_rating_range"),
        CheckConstraint("rating_rd BETWEEN 0 AND 500", name="ck_problem_rating_rd_range"),
        Index("ix_problems_active_rating", "is_active", "rating"),
    )


class ProblemPattern(Base):
    """Weighted edge: a problem may be 0.7 sliding-window, 0.3 hashmap (spec §6.1)."""

    __tablename__ = "problem_patterns"

    problem_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True
    )
    pattern_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE"), primary_key=True
    )
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    problem: Mapped[Problem] = relationship(back_populates="patterns")
    pattern: Mapped[Pattern] = relationship()

    __table_args__ = (
        CheckConstraint("weight > 0 AND weight <= 1", name="ck_problem_pattern_weight"),
    )


# ------------------------------------------------------------------------- attempts


class AttemptEvent(Base):
    """Raw inbound event. The idempotency boundary (invariant 7).

    Every ingestion path writes here first, keyed by the client-generated
    `event_uuid`. A replayed batch collides on the unique index and is reported
    as a duplicate rather than creating a second attempt.
    """

    __tablename__ = "attempt_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_uuid: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, index=True)
    # FK added in Phase 2 alongside the `devices` table; null for manual entry.
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        _enum(ProcessingStatus, "processing_status"), default=ProcessingStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("problems.id", ondelete="RESTRICT"))

    # --- questionnaire (spec §3.2) — all optional; dismissal is valid
    resolution: Mapped[Resolution] = mapped_column(
        _enum(Resolution, "resolution"), default=Resolution.UNKNOWN
    )
    blocker: Mapped[Blocker | None] = mapped_column(_enum(Blocker, "blocker"), nullable=True)
    confidence_cold_redo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hint_level_used: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- telemetry (spec §3.1)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Recorded so the active-time heuristic stays auditable (spec §3.4).
    excluded_seconds: Mapped[int] = mapped_column(Integer, default=0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    submit_count: Mapped[int] = mapped_column(Integer, default=1)
    submission_outcome: Mapped[SubmissionOutcome] = mapped_column(
        _enum(SubmissionOutcome, "submission_outcome"), default=SubmissionOutcome.UNKNOWN
    )

    # --- classification
    is_resolve: Mapped[bool] = mapped_column(Boolean, default=False)
    timed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[AttemptSource] = mapped_column(_enum(AttemptSource, "attempt_source"))
    capture_confidence: Mapped[CaptureConfidence] = mapped_column(
        _enum(CaptureConfidence, "capture_confidence"), default=CaptureConfidence.HIGH
    )

    # --- amendment (spec §3.3): corrections are recorded, never overwritten
    amended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prior_values: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    problem: Mapped[Problem] = relationship()

    __table_args__ = (
        CheckConstraint(
            "confidence_cold_redo IS NULL OR confidence_cold_redo BETWEEN 1 AND 5",
            name="ck_attempt_confidence_range",
        ),
        CheckConstraint(
            "hint_level_used IS NULL OR hint_level_used BETWEEN 1 AND 5",
            name="ck_attempt_hint_level_range",
        ),
        CheckConstraint("active_seconds IS NULL OR active_seconds >= 0", name="ck_attempt_active"),
        CheckConstraint("excluded_seconds >= 0", name="ck_attempt_excluded"),
        Index("ix_attempts_user_submitted", "user_id", "submitted_at"),
    )


# ---------------------------------------------------------------------------- plan


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_goals.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[PlanStatus] = mapped_column(
        _enum(PlanStatus, "plan_status"), default=PlanStatus.PROVISIONAL
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    # How this plan was produced. In Phase 0 always the static curriculum lookup.
    generation_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    items: Mapped[list[PlanItem]] = relationship(
        back_populates="plan", order_by="PlanItem.sequence"
    )

    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_plan_user_version"),)


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sequence: Mapped[int] = mapped_column(Integer)
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("problems.id", ondelete="SET NULL"), nullable=True
    )
    item_type: Mapped[ItemType] = mapped_column(_enum(ItemType, "item_type"))
    role: Mapped[ItemRole] = mapped_column(_enum(ItemRole, "item_role"))
    target_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"), default=ItemStatus.PENDING
    )

    plan: Mapped[Plan] = relationship(back_populates="items")
    problem: Mapped[Problem | None] = relationship()

    __table_args__ = (
        UniqueConstraint("plan_id", "sequence", name="uq_plan_item_sequence"),
        Index("ix_plan_items_block", "block_id"),
    )
