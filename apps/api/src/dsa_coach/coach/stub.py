"""A deterministic coach that never calls a model.

This exists for two reasons, and the second is the important one:

1. **Tests.** Agent evaluations run against deterministic doubles in CI; live
   model runs stay opt-in (spec §15).
2. **It is what runs when no API key is configured.** Invariant 4 says the
   product must be fully usable with the provider unavailable. Rather than
   special-casing "no coach" throughout the system, the absence of a key simply
   selects a coach that reasons arithmetically instead of statistically.

What it produces is a defensible prescription — target the weakest established
patterns, aim near a coin flip, keep the default mix — not a placeholder. That
matters: it is the floor the real coach has to beat, and the thing a user falls
back to on a bad day for the provider.
"""

from __future__ import annotations

from dsa_coach import tuning
from dsa_coach.coach.runtime import CoachContext, CoachOutcome, PatternSummary
from dsa_coach.mechanism.prescription import PatternWeight, Prescription

#: Weakest first. "calibrating" sorts last: an uncalibrated pattern is unknown,
#: not weak, and targeting it would be targeting a guess.
_BAND_ORDER = {
    "not_ready": 0,
    "developing": 1,
    "approaching": 2,
    "ready": 3,
    "calibrating": 4,
}


class StubCoachRuntime:
    name = "deterministic-stub"

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        focus = self._focus(context)
        if not focus:
            # Nothing established to target. Say so honestly rather than
            # inventing a focus; the scheduler handles a broad block fine.
            return CoachOutcome(
                failure=None,
                prescription=Prescription(
                    focus_patterns=(),
                    rating_band=context.rating_range,
                    size=context.max_size,
                    mix=(
                        tuning.BLOCK_MIX_WEAKNESS,
                        tuning.BLOCK_MIX_INTERLEAVED,
                        tuning.BLOCK_MIX_RETENTION,
                    ),
                    diagnosis="Not enough established evidence to name a weakness yet.",
                    rationale=(
                        "Assembled without the coach: a broad block across whatever is "
                        "available, until there is enough evidence to target something."
                    ),
                    confidence="low",
                ),
                model=None,
            )

        low, high = context.rating_range
        return CoachOutcome(
            prescription=Prescription(
                focus_patterns=tuple(
                    PatternWeight(pattern_id=p.pattern_id, weight=1.0) for p in focus
                ),
                rating_band=(low, high),
                size=context.max_size,
                mix=(
                    tuning.BLOCK_MIX_WEAKNESS,
                    tuning.BLOCK_MIX_INTERLEAVED,
                    tuning.BLOCK_MIX_RETENTION,
                ),
                diagnosis=self._diagnosis(focus),
                rationale=(
                    "Assembled without the coach. Targets the patterns with the least "
                    "demonstrated readiness, using the standard mix."
                ),
                confidence="low",
            ),
            model=None,
        )

    @staticmethod
    def _focus(context: CoachContext) -> list[PatternSummary]:
        established = [
            p for p in context.patterns if p.unlocked and not p.provisional and p.evidence_count > 0
        ]
        established.sort(key=lambda p: (_BAND_ORDER.get(p.band, 5), -p.evidence_count))
        return established[: tuning.MAX_PRESCRIBED_FOCUS_PATTERNS]

    @staticmethod
    def _diagnosis(focus: list[PatternSummary]) -> str:
        names = ", ".join(p.slug for p in focus)
        return f"Least demonstrated readiness: {names}."
