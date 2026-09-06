"""Retention scheduling (spec §6.5).

Wraps the pinned FSRS implementation. Two decisions matter here:

**Grading comes from demonstrated recall, not from confidence.** Confidence and
recall are different things, and fluency during a session systematically inflates
the former. `confidence_cold_redo` is stored as a secondary signal only — it
feeds the "how well do you know what you know" metric, never the interval.

**Difficulty is derived, not asked.** A 1-2 click questionnaire cannot afford a
third question, so the grade comes from how the re-solve actually went relative
to the user's own first-exposure time on that same problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fsrs import Card, Rating, Scheduler, State

from dsa_coach import tuning


def _scheduler() -> Scheduler:
    """The pinned, reproducible scheduler.

    Fuzzing is off: the mechanism layer must be deterministic. Learning steps are
    empty because a re-solve is a whole problem, not a flashcard you can review
    again ten minutes later.
    """
    return Scheduler(
        desired_retention=tuning.FSRS_DESIRED_RETENTION,
        learning_steps=tuning.FSRS_LEARNING_STEPS,
        relearning_steps=tuning.FSRS_RELEARNING_STEPS,
        enable_fuzzing=tuning.FSRS_ENABLE_FUZZING,
    )


@dataclass(frozen=True)
class ReviewState:
    """Persisted FSRS card state."""

    stability: float | None
    difficulty: float | None
    due_at: datetime
    last_reviewed_at: datetime | None
    lapses: int
    reps: int
    fsrs_state: int
    fsrs_step: int | None
    fsrs_version: str = tuning.FSRS_VERSION


@dataclass(frozen=True)
class ResolvePerformance:
    """How a scheduled re-solve actually went."""

    solved: bool
    used_help: bool
    submit_count: int
    active_seconds: int | None = None
    #: The user's own time on their first exposure to this problem.
    baseline_active_seconds: int | None = None


def derive_grade(performance: ResolvePerformance) -> Rating:
    """Map observed re-solve performance to an FSRS grade (spec §6.5).

    Failing to reproduce is `Again`. Otherwise the grade starts from how the
    re-solve compared to the first exposure, then is reduced for help or for
    needing more than one submission.
    """
    if not performance.solved:
        return Rating.Again

    grade = _grade_from_time(performance)

    if performance.submit_count > tuning.RESOLVE_CLEAN_SUBMIT_COUNT:
        grade = min(grade, Rating.Hard, key=lambda r: r.value)

    if performance.used_help:
        # Reduced by one level, floored at Again.
        grade = Rating(max(Rating.Again.value, grade.value - 1))

    return grade


def _grade_from_time(performance: ResolvePerformance) -> Rating:
    baseline = performance.baseline_active_seconds
    actual = performance.active_seconds

    # Missing timing is recorded as missing, never guessed (invariant 6). A
    # neutral Good keeps the schedule moving without inventing a signal.
    if not baseline or actual is None:
        return Rating.Good

    ratio = actual / baseline
    if ratio >= tuning.RESOLVE_HARD_TIME_RATIO:
        return Rating.Hard
    if ratio <= tuning.RESOLVE_EASY_TIME_RATIO:
        return Rating.Easy
    return Rating.Good


def _to_card(state: ReviewState | None) -> Card:
    if state is None:
        return Card()
    return Card(
        state=State(state.fsrs_state),
        step=state.fsrs_step,
        stability=state.stability,
        difficulty=state.difficulty,
        due=state.due_at,
        last_review=state.last_reviewed_at,
    )


def _from_card(card: Card, previous: ReviewState | None, lapsed: bool) -> ReviewState:
    return ReviewState(
        stability=card.stability,
        difficulty=card.difficulty,
        due_at=card.due,
        last_reviewed_at=card.last_review,
        lapses=(previous.lapses if previous else 0) + (1 if lapsed else 0),
        reps=(previous.reps if previous else 0) + 1,
        fsrs_state=int(card.state.value),
        fsrs_step=card.step,
        fsrs_version=tuning.FSRS_VERSION,
    )


def schedule_first_solve(now: datetime) -> ReviewState:
    """Start a retention schedule after a problem is first solved."""
    card, _ = _scheduler().review_card(Card(), Rating.Good, review_datetime=now)
    return _from_card(card, previous=None, lapsed=False)


def apply_review(
    state: ReviewState | None, performance: ResolvePerformance, now: datetime
) -> tuple[ReviewState, Rating]:
    """Fold a completed re-solve into the schedule. Returns the new state and grade."""
    grade = derive_grade(performance)
    card, _ = _scheduler().review_card(_to_card(state), grade, review_datetime=now)
    return _from_card(card, previous=state, lapsed=grade is Rating.Again), grade


def is_lapsed(grade: Rating) -> bool:
    """Whether a re-solve counts as a lapse — relevant for the trigger (spec §6.6)."""
    return grade in (Rating.Again, Rating.Hard)
