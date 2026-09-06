"""The OpenAI Agents SDK coach (spec §2, §7).

The whole provider dependency lives here. Note what the agent is *not* given:

- **No tool that returns problem IDs.** Spec §7.1 has no `search_candidate_problems`
  for exactly this reason. The agent cannot name a problem, so it cannot
  hallucinate one, re-prescribe one already solved, or reach past a prerequisite.
  Invariant 3 is structural rather than a rule the model is asked to follow.
- **No user id, and no way to ask for one.** Everything it may see is assembled
  server-side and handed in as typed run context (invariant 10).
- **No write access of any kind.** It returns a shape; the mechanism layer
  validates it and the scheduler fills it.

`prescribe` never raises. A runtime that threw would take the scheduler down with
it, and the scheduler is required to keep working (invariant 4).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field

from dsa_coach import tuning
from dsa_coach.coach.runtime import CoachContext, CoachFailure, CoachOutcome
from dsa_coach.mechanism.prescription import PatternWeight, Prescription

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are the DSA Coach. You decide the *shape* of someone's next practice block.

You will be given their readiness per pattern, their recent attempts with the
blocker they reported, their retention status, and their time budget. Everything
you need is in that context — there is nothing else to look up.

You do not choose problems. You cannot: deterministic code selects the actual
problems from your prescription. Do not name problems, and do not assume any
particular problem exists.

Rules you must follow:

- Only target patterns marked unlocked AND not provisional. A provisional
  pattern is one whose prerequisites are unproven; targeting it means aiming at
  a guess.
- Never remove interleaving. Mixed practice transfers; blocked practice only
  feels better.
- Keep the block inside the stated time budget.
- Ground the diagnosis in the attempts you were given. Cite their ids in
  evidence_attempt_ids. If the evidence is thin, say so and set confidence low
  rather than inventing a pattern in the data.
- If nothing has changed enough to act on, say so in the diagnosis and prescribe
  a continuation rather than manufacturing a change.

Write the rationale for the person practising, in one or two plain sentences.
""".strip()


class FocusPatternOut(BaseModel):
    pattern_id: str = Field(description="A pattern id copied exactly from the context.")
    weight: Annotated[float, Field(gt=0, le=1)] = Field(
        description="Relative emphasis within the block."
    )


class BlockPrescriptionOut(BaseModel):
    """The structured output. Consequential fields are never parsed from prose."""

    focus_patterns: list[FocusPatternOut]
    rating_band_low: int
    rating_band_high: int
    size: Annotated[int, Field(ge=1, le=50)]
    mix_weakness: Annotated[float, Field(ge=0, le=1)]
    mix_interleaved: Annotated[float, Field(ge=0, le=1)]
    mix_retention: Annotated[float, Field(ge=0, le=1)]
    timed: bool = False
    diagnosis: str
    rationale: str
    evidence_attempt_ids: list[str] = Field(default_factory=list)
    confidence: str = "low"


class OpenAICoachRuntime:
    """Runs the coach through the OpenAI Agents SDK."""

    name = "openai-agents"

    def __init__(self, model: str, api_key: str, tracing_enabled: bool = False) -> None:
        self._model = model
        self._api_key = api_key
        self._tracing_enabled = tracing_enabled

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        try:
            return await asyncio.wait_for(self._run(context), timeout=tuning.COACH_TIMEOUT_SECONDS)
        except TimeoutError:
            return CoachOutcome(
                failure=CoachFailure.TIMEOUT,
                error_detail="The coach did not respond in time.",
                model=self._model,
            )
        except Exception as error:
            return CoachOutcome(
                failure=_classify(error),
                # Sanitized: a provider message can echo the prompt back, and the
                # prompt contains the user's practice history.
                error_detail=type(error).__name__,
                model=self._model,
            )

    async def _run(self, context: CoachContext) -> CoachOutcome:
        import os

        from agents import Agent, RunConfig, Runner, set_default_openai_key

        set_default_openai_key(self._api_key)
        os.environ.setdefault("OPENAI_API_KEY", self._api_key)

        agent: Agent[CoachContext] = Agent(
            name="DSA Coach",
            instructions=INSTRUCTIONS,
            model=self._model,
            output_type=BlockPrescriptionOut,
        )

        result = await Runner.run(
            agent,
            input=_render(context),
            context=context,
            max_turns=3,
            run_config=RunConfig(
                workflow_name="dsa-coach-prescription",
                tracing_disabled=not self._tracing_enabled,
            ),
        )

        output = result.final_output
        if not isinstance(output, BlockPrescriptionOut):
            return CoachOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="The coach did not return a prescription.",
                model=self._model,
            )

        prescription = _to_prescription(output)
        if prescription is None:
            return CoachOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="The prescription referenced ids that are not valid UUIDs.",
                model=self._model,
            )

        return CoachOutcome(
            prescription=prescription,
            model=self._model,
            trace_id=getattr(result, "trace_id", None),
            usage=_usage(result),
        )


def _classify(error: Exception) -> CoachFailure:
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return CoachFailure.RATE_LIMITED
    if "timeout" in name or "timeout" in text:
        return CoachFailure.TIMEOUT
    if "authentication" in name or "api key" in text or "401" in text:
        # Not retryable: a wrong key stays wrong.
        return CoachFailure.UNAVAILABLE
    return CoachFailure.PROVIDER_ERROR


def _to_prescription(output: BlockPrescriptionOut) -> Prescription | None:
    try:
        focus = tuple(
            PatternWeight(pattern_id=UUID(item.pattern_id), weight=item.weight)
            for item in output.focus_patterns
        )
        evidence = tuple(UUID(value) for value in output.evidence_attempt_ids)
    except (ValueError, AttributeError):
        # A malformed id is the model inventing something. The mechanism layer
        # would drop it anyway; failing here says why more clearly.
        return None

    return Prescription(
        focus_patterns=focus,
        rating_band=(output.rating_band_low, output.rating_band_high),
        size=output.size,
        mix=(output.mix_weakness, output.mix_interleaved, output.mix_retention),
        timed=output.timed,
        diagnosis=output.diagnosis,
        rationale=output.rationale,
        evidence_attempt_ids=evidence,
        confidence=output.confidence if output.confidence in {"low", "medium", "high"} else "low",
    )


def _usage(result: Any) -> dict[str, Any]:
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "requests": getattr(usage, "requests", None),
    }


def _render(context: CoachContext) -> str:
    """The prompt. Plain text, because the model reads it, not a parser."""
    lines = [
        f"Level: {context.level}. Budget: {context.minutes_per_day} minutes/day, "
        f"{context.days_per_week} days/week.",
        f"A block may hold at most {context.max_size} problems.",
        f"Catalogue ratings run {context.rating_range[0]}-{context.rating_range[1]}.",
    ]
    if context.target_companies:
        lines.append(f"Target companies: {', '.join(context.target_companies)}.")

    lines.append("")
    lines.append("Readiness by pattern:")
    for pattern in context.patterns:
        state = "locked"
        if pattern.unlocked:
            state = "provisional" if pattern.provisional else "available"
        lines.append(
            f"- {pattern.slug} (id {pattern.pattern_id}): {pattern.band}, "
            f"{pattern.evidence_count} observations, {state}"
        )

    lines.append("")
    lines.append("Recent attempts:")
    for attempt in context.recent_attempts:
        blocker = f", blocker {attempt.blocker}" if attempt.blocker else ""
        lines.append(
            f"- id {attempt.attempt_id}: {attempt.problem_slug} "
            f"(rating {attempt.problem_rating}, patterns {', '.join(attempt.patterns)}) "
            f"-> {attempt.resolution}{blocker}"
        )

    lines.append("")
    lines.append(
        f"Retention: {context.retention_due} reviews due, "
        f"{context.retention_lapses} lapses recorded."
    )
    if context.trigger_reasons:
        lines.append("What prompted this review: " + "; ".join(context.trigger_reasons))

    lines.append("")
    lines.append("Prescribe the shape of the next block.")
    return "\n".join(lines)
