"""The teaching runtime: hints, diagnosis, review, mock interview (spec §7.3).

This is where an LLM genuinely outperforms deterministic code. A scheduler can
tell you *what* to practise; only something that reads your actual code can tell
you that the shrink condition in your sliding window is wrong in the same way it
was wrong last Tuesday.

The same shape as `CoachRuntime`: never raises, always returns an outcome, and a
deterministic stub stands in when the provider is absent. The stub's answers are
deliberately thin — there is no arithmetic substitute for reading someone's code,
and pretending otherwise would be worse than admitting it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.mechanism.complexity import ComplexityEstimate
from dsa_coach.mechanism.hints import LEVEL_BRIEFS, HintLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorFailure:
    """An earlier attempt, so the coach can spot a repeating mistake."""

    attempt_id: UUID
    problem_slug: str
    resolution: str
    blocker: str | None


@dataclass(frozen=True)
class HintRequest:
    problem_slug: str
    problem_title: str
    patterns: tuple[str, ...]
    level: HintLevel
    already_seen: int


@dataclass(frozen=True)
class DiagnosisRequest:
    problem_slug: str
    problem_title: str
    patterns: tuple[str, ...]
    resolution: str
    blocker: str | None
    #: None when the user has not consented to code capture. The diagnosis is
    #: still produced, just from less.
    code: str | None
    language: str | None
    complexity: ComplexityEstimate | None
    prior_failures: tuple[PriorFailure, ...]


@dataclass(frozen=True)
class ReviewRequest:
    problem_slug: str
    problem_title: str
    patterns: tuple[str, ...]
    code: str | None
    language: str | None
    complexity: ComplexityEstimate | None
    active_seconds: int | None


@dataclass(frozen=True)
class MockTurnRequest:
    problem_slug: str
    problem_title: str
    patterns: tuple[str, ...]
    #: What the candidate has said so far, oldest first.
    transcript: tuple[tuple[str, str], ...]
    candidate_message: str


@dataclass(frozen=True)
class TeachingOutcome:
    text: str | None = None
    failure: CoachFailure | None = None
    error_detail: str | None = None
    model: str | None = None
    #: True when the answer was produced without the user's code.
    degraded: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    cited_attempt_ids: tuple[UUID, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.text is not None


class TeachingRuntime(Protocol):
    name: str

    async def hint(self, request: HintRequest) -> TeachingOutcome: ...
    async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome: ...
    async def review(self, request: ReviewRequest) -> TeachingOutcome: ...
    async def mock_turn(self, request: MockTurnRequest) -> TeachingOutcome: ...


# ------------------------------------------------------------------- the stub


class StubTeachingRuntime:
    """What teaching looks like with no model available.

    It says what it can from structure alone and is explicit about the rest.
    That is the honest floor: there is no deterministic substitute for reading
    someone's code, and a confident-sounding generic answer would be worse than
    none.
    """

    name = "deterministic-stub"

    async def hint(self, request: HintRequest) -> TeachingOutcome:
        family = ", ".join(request.patterns) or "no tagged pattern"
        if request.level is HintLevel.PATTERN_FAMILY:
            return TeachingOutcome(
                text=(
                    f"This problem is tagged: {family}. "
                    "The coach is unavailable, so that tag is all that can be offered."
                )
            )
        return TeachingOutcome(
            text=(
                "The coach is unavailable, so hints beyond the pattern family are not "
                f"available right now. This problem is tagged: {family}."
            )
        )

    async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome:
        parts = [
            "The coach is unavailable, so this is what can be said without it.",
            f"You reported: {request.resolution}"
            + (f", blocked by {request.blocker}." if request.blocker else "."),
        ]
        if request.complexity is not None:
            parts.append(
                f"Static read of your code: {request.complexity.display}"
                + (
                    f" ({', '.join(request.complexity.signals)})."
                    if request.complexity.signals
                    else "."
                )
            )
        repeats = [f for f in request.prior_failures if f.blocker == request.blocker]
        if request.blocker and len(repeats) >= 2:
            parts.append(
                f"This is the {len(repeats) + 1}th time {request.blocker} has stopped you. "
                "That repetition is the thing worth looking at."
            )
        return TeachingOutcome(
            text=" ".join(parts),
            degraded=request.code is None,
            cited_attempt_ids=tuple(f.attempt_id for f in repeats),
        )

    async def review(self, request: ReviewRequest) -> TeachingOutcome:
        if request.complexity is None:
            return TeachingOutcome(
                text=(
                    "The coach is unavailable and no code was stored, so there is "
                    "nothing to review here."
                ),
                degraded=True,
            )
        return TeachingOutcome(
            text=(
                f"The coach is unavailable. From structure alone your solution "
                f"{request.complexity.display}"
                + (
                    f", based on {', '.join(request.complexity.signals)}."
                    if request.complexity.signals
                    else "."
                )
                + " Whether that is the intended complexity is not something this "
                "fallback can judge."
            ),
            degraded=False,
        )

    async def mock_turn(self, request: MockTurnRequest) -> TeachingOutcome:
        return TeachingOutcome(
            text=(
                "The coach is unavailable, so a mock interview cannot run. "
                "Try again once it is reachable."
            ),
            failure=CoachFailure.UNAVAILABLE,
            degraded=True,
        )


# ------------------------------------------------------------------ the real one


HINT_INSTRUCTIONS = """
You are giving one hint, at exactly the level asked for, and no further.

The whole value of a hint ladder is that it stops where it is told to. A level 2
hint that gives away the approach has destroyed the exercise and made the
recorded hint level a lie.

Never include code, pseudocode, or a snippet below level 5. Not even one line.
Keep it to a few sentences.
""".strip()

DIAGNOSIS_INSTRUCTIONS = """
You are diagnosing why an attempt went wrong, from the person's actual code.

Be specific. "You had an off-by-one" is useless; "your window shrinks before you
record the answer, so the last valid window is never counted" is the point.

If you are shown earlier failures with the same blocker, say so plainly — the
repeating failure mode is more useful than any single bug.

Only refer to attempts you were given. Do not invent history.

If no code was provided, say what can and cannot be determined without it rather
than guessing.
""".strip()

REVIEW_INSTRUCTIONS = """
You are reviewing a solution that already passed.

"Accepted" and "would pass an interview" are different bars, and your job is the
second one: real complexity versus claimed, edge cases that got lucky,
unidiomatic constructions, and how to explain the solution out loud.

Be brief and concrete. If it is genuinely good, say so and stop.
""".strip()

MOCK_INSTRUCTIONS = """
You are conducting a technical interview.

Make the candidate state their approach and its complexity before they write
anything, and push back on it — the way a real interviewer would. Ask about edge
cases. Do not solve it for them, and do not accept a vague plan.

One question or challenge per turn.
""".strip()


class OpenAITeachingRuntime:
    """Teaching through the OpenAI Agents SDK."""

    name = "openai-agents"

    def __init__(self, model: str, api_key: str, tracing_enabled: bool = False) -> None:
        self._model = model
        self._api_key = api_key
        self._tracing_enabled = tracing_enabled

    async def hint(self, request: HintRequest) -> TeachingOutcome:
        prompt = "\n".join(
            [
                f"Problem: {request.problem_title} ({request.problem_slug})",
                f"Tagged patterns: {', '.join(request.patterns) or 'none'}",
                f"Hint level requested: {int(request.level)} of 5.",
                f"Level brief: {LEVEL_BRIEFS[request.level]}",
                f"The user has already seen {request.already_seen} hint(s) on this problem.",
            ]
        )
        return await self._ask("hint", HINT_INSTRUCTIONS, prompt)

    async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome:
        lines = [
            f"Problem: {request.problem_title} ({request.problem_slug})",
            f"Tagged patterns: {', '.join(request.patterns) or 'none'}",
            f"Outcome: {request.resolution}",
            f"Reported blocker: {request.blocker or 'none reported'}",
        ]
        if request.complexity is not None:
            lines.append(f"Static complexity read: {request.complexity.display}")
        if request.prior_failures:
            lines.append("Earlier attempts:")
            lines.extend(
                f"- id {f.attempt_id}: {f.problem_slug} -> {f.resolution}"
                + (f" (blocker {f.blocker})" if f.blocker else "")
                for f in request.prior_failures
            )
        if request.code:
            lines.append(
                f"\nTheir code ({request.language or 'unknown language'}):\n{request.code}"
            )
        else:
            lines.append(
                "\nNo code was provided — the user has not consented to code capture. "
                "Diagnose from the reported blocker and history only, and say what you "
                "cannot determine."
            )

        outcome = await self._ask("diagnosis", DIAGNOSIS_INSTRUCTIONS, "\n".join(lines))
        return TeachingOutcome(
            text=outcome.text,
            failure=outcome.failure,
            error_detail=outcome.error_detail,
            model=outcome.model,
            degraded=request.code is None,
            usage=outcome.usage,
            cited_attempt_ids=tuple(f.attempt_id for f in request.prior_failures),
        )

    async def review(self, request: ReviewRequest) -> TeachingOutcome:
        lines = [
            f"Problem: {request.problem_title} ({request.problem_slug})",
            f"Tagged patterns: {', '.join(request.patterns) or 'none'}",
        ]
        if request.complexity is not None:
            lines.append(f"Static complexity read: {request.complexity.display}")
        if request.active_seconds:
            lines.append(f"Time taken: {request.active_seconds // 60} minutes.")
        if request.code:
            lines.append(f"\nAccepted solution ({request.language or 'unknown'}):\n{request.code}")
        else:
            return TeachingOutcome(
                text=(
                    "No code was stored for this attempt, so there is nothing to review. "
                    "Enable code capture in Settings if you want reviews."
                ),
                degraded=True,
            )
        return await self._ask("review", REVIEW_INSTRUCTIONS, "\n".join(lines))

    async def mock_turn(self, request: MockTurnRequest) -> TeachingOutcome:
        lines = [
            f"Problem: {request.problem_title} ({request.problem_slug})",
            f"Tagged patterns: {', '.join(request.patterns) or 'none'}",
            "",
            "Transcript so far:",
        ]
        lines.extend(f"{speaker}: {text}" for speaker, text in request.transcript)
        lines.append(f"candidate: {request.candidate_message}")
        return await self._ask("mock", MOCK_INSTRUCTIONS, "\n".join(lines))

    async def _ask(self, kind: str, instructions: str, prompt: str) -> TeachingOutcome:
        try:
            return await asyncio.wait_for(
                self._run(kind, instructions, prompt), timeout=tuning.COACH_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return TeachingOutcome(
                failure=CoachFailure.TIMEOUT,
                error_detail="The coach did not respond in time.",
                model=self._model,
            )
        except Exception as error:
            from dsa_coach.coach.openai_runtime import _classify

            return TeachingOutcome(
                # Sanitized: a provider message can echo the prompt, and this
                # prompt may contain the user's code.
                failure=_classify(error),
                error_detail=type(error).__name__,
                model=self._model,
            )

    async def _run(self, kind: str, instructions: str, prompt: str) -> TeachingOutcome:
        import os

        from agents import Agent, RunConfig, Runner, set_default_openai_key

        set_default_openai_key(self._api_key)
        os.environ.setdefault("OPENAI_API_KEY", self._api_key)

        agent: Agent[None] = Agent(
            name=f"DSA Coach ({kind})", instructions=instructions, model=self._model
        )
        result = await Runner.run(
            agent,
            input=prompt,
            max_turns=2,
            run_config=RunConfig(
                workflow_name=f"dsa-coach-{kind}", tracing_disabled=not self._tracing_enabled
            ),
        )
        text = str(result.final_output or "").strip()
        if not text:
            return TeachingOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="Empty response.",
                model=self._model,
            )
        return TeachingOutcome(text=text, model=self._model)


def build_teaching_runtime(settings: Any | None = None) -> TeachingRuntime:
    from dsa_coach.config import get_settings

    settings = settings or get_settings()
    if not settings.openai_api_key:
        return StubTeachingRuntime()
    try:
        import agents  # noqa: F401
    except ImportError:
        logger.warning("openai-agents missing; teaching falls back to the stub.")
        return StubTeachingRuntime()
    return OpenAITeachingRuntime(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        tracing_enabled=settings.agent_tracing_enabled,
    )
