"""Validating and clamping a coach prescription (spec §7.2).

This module is the boundary between judgment and mechanism, and it is the reason
invariant 3 holds. The coach describes the *shape* of a block; everything here
checks that shape against facts it cannot argue with — the time budget, the
prerequisite graph, what the catalogue actually contains — before any of it
reaches the scheduler.

Three outcomes, and the middle one matters most:

- **accepted** — the prescription was already feasible.
- **clamped** — it asked for something impossible, so it was adjusted to the
  nearest thing that is, and every adjustment is recorded. A prescription that
  violates a constraint is never silently dropped, and never silently applied.
- **rejected** — nothing usable survived. The deterministic scheduler takes over.

Pure functions. No database, no network, and — invariant 2 — no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.models import ValidationResult


class Violation(StrEnum):
    UNKNOWN_PATTERN = "unknown_pattern"
    LOCKED_PATTERN = "locked_pattern"
    UNPROVEN_PATTERN = "unproven_pattern"
    NO_FOCUS_PATTERNS = "no_focus_patterns"
    BAND_INVERTED = "band_inverted"
    BAND_OUT_OF_RANGE = "band_out_of_range"
    BAND_EMPTY = "band_empty"
    SIZE_OUT_OF_RANGE = "size_out_of_range"
    SIZE_OVER_BUDGET = "size_over_budget"
    MIX_NOT_NORMALISED = "mix_not_normalised"
    MIX_NO_INTERLEAVING = "mix_no_interleaving"


@dataclass(frozen=True)
class PatternWeight:
    pattern_id: UUID
    weight: float


@dataclass(frozen=True)
class Prescription:
    """A block shape. Note what is absent: any problem id.

    The coach cannot name problems, so it cannot hallucinate one, schedule one
    the user already solved, or reach past a prerequisite. That is structural,
    not a rule the model is asked to follow.
    """

    focus_patterns: tuple[PatternWeight, ...]
    rating_band: tuple[int, int]
    size: int
    mix: tuple[float, float, float]  # weakness, interleaved, retention
    timed: bool = False
    diagnosis: str = ""
    rationale: str = ""
    evidence_attempt_ids: tuple[UUID, ...] = ()
    confidence: str = "low"


@dataclass(frozen=True)
class CatalogueFacts:
    """What the mechanism layer knows and the coach does not get to dispute."""

    known_patterns: frozenset[UUID]
    unlocked_patterns: frozenset[UUID]
    #: Unlocked only because prerequisites are unproven (spec §6.4).
    provisional_patterns: frozenset[UUID]
    #: Active problems per pattern, keyed by pattern then counted in-band.
    available_in_band: dict[UUID, int]
    rating_range: tuple[int, int]
    budget_minutes: int
    max_size: int


@dataclass(frozen=True)
class ValidatedPrescription:
    result: ValidationResult
    prescription: Prescription | None
    violations: tuple[dict[str, str], ...]

    @property
    def usable(self) -> bool:
        return self.prescription is not None


def _violation(kind: Violation, detail: str) -> dict[str, str]:
    return {"kind": kind.value, "detail": detail}


def validate(prescription: Prescription, facts: CatalogueFacts) -> ValidatedPrescription:
    """Check a prescription against reality and clamp it where it can be saved."""
    violations: list[dict[str, str]] = []
    working = prescription

    working, focus_violations = _clamp_focus(working, facts)
    violations.extend(focus_violations)

    if not working.focus_patterns:
        violations.append(
            _violation(
                Violation.NO_FOCUS_PATTERNS,
                "no focus pattern survived validation; falling back to the deterministic scheduler",
            )
        )
        return ValidatedPrescription(
            result=ValidationResult.REJECTED,
            prescription=None,
            violations=tuple(violations),
        )

    working, band_violations = _clamp_band(working, facts)
    violations.extend(band_violations)

    working, size_violations = _clamp_size(working, facts)
    violations.extend(size_violations)

    working, mix_violations = _clamp_mix(working)
    violations.extend(mix_violations)

    result = ValidationResult.ACCEPTED if not violations else ValidationResult.CLAMPED
    return ValidatedPrescription(result=result, prescription=working, violations=tuple(violations))


def _clamp_focus(
    prescription: Prescription, facts: CatalogueFacts
) -> tuple[Prescription, list[dict[str, str]]]:
    """Drop focus patterns the coach may not target.

    Unknown ones are hallucinations. Locked ones have a prerequisite the user has
    demonstrably not reached. Provisionally-unlocked ones are available to
    practise but must not be *targeted* — aiming at a pattern whose foundations
    are unproven means aiming at a guess.
    """
    kept: list[PatternWeight] = []
    violations: list[dict[str, str]] = []

    for item in prescription.focus_patterns:
        short = str(item.pattern_id)[:8]
        if item.pattern_id not in facts.known_patterns:
            violations.append(
                _violation(Violation.UNKNOWN_PATTERN, f"pattern {short} is not in the catalogue")
            )
            continue
        if item.pattern_id not in facts.unlocked_patterns:
            violations.append(
                _violation(
                    Violation.LOCKED_PATTERN,
                    f"pattern {short} is behind an unmet prerequisite",
                )
            )
            continue
        if item.pattern_id in facts.provisional_patterns:
            violations.append(
                _violation(
                    Violation.UNPROVEN_PATTERN,
                    f"pattern {short} is available but its prerequisites are unproven, "
                    "so it cannot be a focus",
                )
            )
            continue
        if item.weight <= 0:
            violations.append(
                _violation(Violation.UNKNOWN_PATTERN, f"pattern {short} has non-positive weight")
            )
            continue
        kept.append(item)

    return replace(prescription, focus_patterns=tuple(kept)), violations


def _clamp_band(
    prescription: Prescription, facts: CatalogueFacts
) -> tuple[Prescription, list[dict[str, str]]]:
    low, high = prescription.rating_band
    floor, ceiling = facts.rating_range
    violations: list[dict[str, str]] = []

    if low > high:
        violations.append(
            _violation(Violation.BAND_INVERTED, f"band {low}-{high} is inverted; swapped")
        )
        low, high = high, low

    clamped_low = max(floor, min(low, ceiling))
    clamped_high = max(floor, min(high, ceiling))
    if (clamped_low, clamped_high) != (low, high):
        violations.append(
            _violation(
                Violation.BAND_OUT_OF_RANGE,
                f"band {low}-{high} falls outside the catalogue ({floor}-{ceiling}); "
                f"clamped to {clamped_low}-{clamped_high}",
            )
        )

    available = sum(
        facts.available_in_band.get(item.pattern_id, 0) for item in prescription.focus_patterns
    )
    if available == 0:
        # Asking for problems that do not exist. Widening to the whole catalogue
        # is better than returning an empty block.
        violations.append(
            _violation(
                Violation.BAND_EMPTY,
                f"no active problems for the focus patterns in {clamped_low}-{clamped_high}; "
                "widened to the full catalogue range",
            )
        )
        clamped_low, clamped_high = floor, ceiling

    return replace(prescription, rating_band=(clamped_low, clamped_high)), violations


def _clamp_size(
    prescription: Prescription, facts: CatalogueFacts
) -> tuple[Prescription, list[dict[str, str]]]:
    violations: list[dict[str, str]] = []
    size = prescription.size

    if size < 1:
        violations.append(
            _violation(Violation.SIZE_OUT_OF_RANGE, f"size {size} is not positive; raised to 1")
        )
        size = 1

    if size > facts.max_size:
        violations.append(
            _violation(
                Violation.SIZE_OVER_BUDGET,
                f"size {size} exceeds what {facts.budget_minutes} minutes allows; "
                f"reduced to {facts.max_size}",
            )
        )
        size = facts.max_size

    return replace(prescription, size=size), violations


def _clamp_mix(prescription: Prescription) -> tuple[Prescription, list[dict[str, str]]]:
    """Normalise the mix, and refuse to let interleaving be removed.

    Interleaving is not a preference. Blocked practice inflates in-session
    performance and degrades transfer, so a prescription that zeroes it out is
    asking for something that would feel better and work worse.
    """
    violations: list[dict[str, str]] = []
    weakness, interleaved, retention = (max(0.0, v) for v in prescription.mix)
    total = weakness + interleaved + retention

    if total <= 0:
        violations.append(
            _violation(Violation.MIX_NOT_NORMALISED, "mix was empty; reset to the default")
        )
        weakness, interleaved, retention = (
            tuning.BLOCK_MIX_WEAKNESS,
            tuning.BLOCK_MIX_INTERLEAVED,
            tuning.BLOCK_MIX_RETENTION,
        )
        total = 1.0
    elif abs(total - 1.0) > 1e-6:
        violations.append(
            _violation(
                Violation.MIX_NOT_NORMALISED, f"mix summed to {total:.2f}; normalised to 1.0"
            )
        )

    weakness, interleaved, retention = (v / total for v in (weakness, interleaved, retention))

    if interleaved < tuning.MIN_INTERLEAVED_SHARE:
        violations.append(
            _violation(
                Violation.MIX_NO_INTERLEAVING,
                f"interleaving was {interleaved:.0%}, below the {tuning.MIN_INTERLEAVED_SHARE:.0%} "
                "floor; raised. Blocked practice flatters in-session performance and "
                "degrades transfer",
            )
        )
        shortfall = tuning.MIN_INTERLEAVED_SHARE - interleaved
        interleaved = tuning.MIN_INTERLEAVED_SHARE
        # Take it from whichever of the others has more to give.
        if weakness >= retention:
            weakness = max(0.0, weakness - shortfall)
        else:
            retention = max(0.0, retention - shortfall)

    return replace(prescription, mix=(weakness, interleaved, retention)), violations
