"""Tests for the Codex coach runtime.

These never reach the network. A real Codex turn costs a ChatGPT quota and takes
tens of seconds, and neither belongs in a suite that runs on every change; the
SDK is replaced with fakes and the runtime's own logic is what gets exercised.

What is worth testing here is not "does the model answer well" — it is:

- **Containment.** The turn is started read-only, approves nothing, persists
  nothing and runs in an empty directory. Those arguments are a security
  boundary, so they are asserted rather than trusted to stay put.
- **Never raising.** Invariant 4 says the product works with the provider down,
  and the whole scheduler depends on `prescribe` returning a `CoachOutcome`
  instead of throwing. Every failure mode is therefore tested for a value.
- **Rejecting bad output.** A model can return anything. Nothing reaches the
  mechanism layer unless it parses into a real `Prescription`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from dsa_coach import tuning
from dsa_coach.coach.codex_runtime import (
    PRESCRIPTION_SCHEMA,
    CodexCoachRuntime,
    _parse,
    _render,
)
from dsa_coach.coach.runtime import AttemptSummary, CoachContext, CoachFailure, PatternSummary

PATTERN_ID = UUID("ff3d211a-806d-4db8-a1bd-7d1f0b3f9a1c")
ATTEMPT_ID = UUID("9878ce19-6912-4d43-a2fc-b3fce6cc561e")


def context(**overrides: Any) -> CoachContext:
    base: dict[str, Any] = {
        "level": "intermediate",
        "days_per_week": 5,
        "minutes_per_day": 60,
        "target_companies": ("Acme",),
        "patterns": (
            PatternSummary(
                pattern_id=PATTERN_ID,
                slug="two-pointers",
                name="Two Pointers",
                band="developing",
                calibrated=False,
                evidence_count=3,
                unlocked=True,
                provisional=False,
            ),
        ),
        "recent_attempts": (
            AttemptSummary(
                attempt_id=ATTEMPT_ID,
                problem_slug="max-consecutive-ones",
                problem_rating=1600,
                patterns=("two-pointers",),
                resolution="failed",
                blocker="complexity",
                submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
                active_seconds=1800,
            ),
        ),
        "retention_due": 2,
        "retention_lapses": 1,
        "trigger_reasons": ("two failures in a row",),
        "max_size": 5,
        "rating_range": (1100, 2400),
    }
    return CoachContext(**{**base, **overrides})


def valid_response() -> str:
    return json.dumps(
        {
            "focus_patterns": [{"pattern_id": str(PATTERN_ID), "weight": 1.0}],
            "rating_band_low": 1200,
            "rating_band_high": 1400,
            "size": 3,
            "mix_weakness": 0.6,
            "mix_interleaved": 0.3,
            "mix_retention": 0.1,
            "timed": False,
            "diagnosis": "Complexity, not recognition.",
            "rationale": "Three problems just below your ceiling.",
            "evidence_attempt_ids": [str(ATTEMPT_ID)],
            "confidence": "medium",
        }
    )


# --------------------------------------------------------------------------
# A fake `openai_codex`, installed into sys.modules for the duration of a test.
# --------------------------------------------------------------------------


class FakeResult:
    def __init__(self, response: str | None, status: str = "completed") -> None:
        self.final_response = response
        self.status = status
        self.id = "turn-1"
        self.usage = types.SimpleNamespace(
            total=types.SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120)
        )


class FakeThread:
    def __init__(self, recorder: dict[str, Any], result: Any, raises: Exception | None) -> None:
        self._recorder = recorder
        self._result = result
        self._raises = raises

    async def run(self, prompt: str, output_schema: dict[str, Any] | None = None) -> Any:
        self._recorder["prompt"] = prompt
        self._recorder["output_schema"] = output_schema
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeCodex:
    """Stands in for `AsyncCodex`, recording how the turn was configured."""

    def __init__(self, recorder: dict[str, Any], result: Any, raises: Exception | None) -> None:
        self._recorder = recorder
        self._result = result
        self._raises = raises
        recorder["closed"] = False

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self._recorder["thread_kwargs"] = kwargs
        return FakeThread(self._recorder, self._result, self._raises)

    async def close(self) -> None:
        self._recorder["closed"] = True


@pytest.fixture
def codex(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Install a fake `openai_codex` and hand back the recorder.

    The runtime imports the SDK inside `_run`, so patching `sys.modules` is
    enough — no import of the real package happens at all.
    """
    recorder: dict[str, Any] = {}

    def install(result: Any = None, raises: Exception | None = None) -> dict[str, Any]:
        module = types.ModuleType("openai_codex")
        module.AsyncCodex = lambda *a, **k: FakeCodex(recorder, result, raises)  # type: ignore[attr-defined]
        module.Sandbox = types.SimpleNamespace(read_only="read-only")  # type: ignore[attr-defined]
        module.ApprovalMode = types.SimpleNamespace(deny_all="deny-all")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "openai_codex", module)
        return recorder

    return install


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


class TestContainment:
    """Codex is a coding agent. Here it must be only an inference endpoint."""

    @pytest.mark.asyncio
    async def test_the_turn_is_read_only_unapproved_and_ephemeral(self, codex) -> None:  # type: ignore[no-untyped-def]
        recorder = codex(FakeResult(valid_response()))

        await CodexCoachRuntime().prescribe(context())

        kwargs = recorder["thread_kwargs"]
        assert kwargs["sandbox"] == "read-only"
        assert kwargs["approval_mode"] == "deny-all"
        assert kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_it_runs_in_an_empty_directory(self, codex) -> None:  # type: ignore[no-untyped-def]
        # Even a read-only agent should see nothing of the user's machine, so
        # the cwd must not be the repo or anything else with files in it.
        recorder = codex(FakeResult(valid_response()))

        await CodexCoachRuntime().prescribe(context())

        cwd = recorder["thread_kwargs"]["cwd"]
        assert "dsa-coach-" in cwd

    @pytest.mark.asyncio
    async def test_the_client_is_closed_even_when_the_turn_raises(self, codex) -> None:  # type: ignore[no-untyped-def]
        recorder = codex(raises=RuntimeError("boom"))

        await CodexCoachRuntime().prescribe(context())

        assert recorder["closed"] is True

    def test_the_schema_has_nowhere_to_put_a_problem(self) -> None:
        # Invariant 3, structurally: the coach prescribes shape, deterministic
        # code picks problems. It cannot name one because there is no field.
        properties = set(PRESCRIPTION_SCHEMA["properties"])

        assert not {p for p in properties if "problem" in p}
        assert PRESCRIPTION_SCHEMA["additionalProperties"] is False


# --------------------------------------------------------------------------
# Failure handling — invariant 4
# --------------------------------------------------------------------------


class TestNeverRaises:
    """`prescribe` returns an outcome for every failure. The scheduler needs it."""

    @pytest.mark.asyncio
    async def test_a_provider_exception_becomes_an_outcome(self, codex) -> None:  # type: ignore[no-untyped-def]
        codex(raises=RuntimeError("upstream exploded"))

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.prescription is None
        assert outcome.failure is CoachFailure.PROVIDER_ERROR

    @pytest.mark.asyncio
    async def test_the_error_detail_does_not_leak_the_prompt(self, codex) -> None:  # type: ignore[no-untyped-def]
        # A provider message can echo the prompt back, and the prompt is the
        # user's practice history. Only the exception *type* is recorded.
        codex(raises=RuntimeError(f"failed while handling: {ATTEMPT_ID} max-consecutive-ones"))

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.error_detail == "RuntimeError"
        assert str(ATTEMPT_ID) not in (outcome.error_detail or "")

    @pytest.mark.asyncio
    async def test_a_missing_sdk_becomes_an_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "openai_codex", None)

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.prescription is None
        assert outcome.failure is not None

    @pytest.mark.asyncio
    async def test_an_unfinished_turn_is_not_a_prescription(self, codex) -> None:  # type: ignore[no-untyped-def]
        codex(FakeResult(valid_response(), status="failed"))

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.prescription is None
        assert outcome.failure is CoachFailure.PROVIDER_ERROR

    @pytest.mark.asyncio
    async def test_a_slow_provider_times_out_rather_than_hanging(
        self, codex, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        # A hung provider must not hang the request. The timeout is shortened
        # rather than the clock faked, so the real `wait_for` path is exercised.
        async def sleep_forever(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(30)

        monkeypatch.setattr(tuning, "COACH_TIMEOUT_SECONDS", 0.05)
        recorder = codex(FakeResult(valid_response()))
        monkeypatch.setattr(FakeThread, "run", sleep_forever)

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.failure is CoachFailure.TIMEOUT
        assert outcome.prescription is None
        # The hung turn is still cleaned up rather than leaked.
        assert recorder["closed"] is True


# --------------------------------------------------------------------------
# Parsing — nothing unvalidated reaches the mechanism layer
# --------------------------------------------------------------------------


class TestParsing:
    @pytest.mark.asyncio
    async def test_a_valid_response_becomes_a_prescription(self, codex) -> None:  # type: ignore[no-untyped-def]
        codex(FakeResult(valid_response()))

        outcome = await CodexCoachRuntime().prescribe(context())

        assert outcome.failure is None
        assert outcome.prescription is not None
        assert outcome.prescription.size == 3
        assert outcome.prescription.rating_band == (1200, 1400)
        assert outcome.prescription.focus_patterns[0].pattern_id == PATTERN_ID
        assert outcome.usage["total_tokens"] == 120

    @pytest.mark.parametrize(
        ("label", "raw"),
        [
            ("empty", ""),
            ("none", None),
            ("prose instead of json", "Sure! Here is your block."),
            ("json but not an object", "[1, 2, 3]"),
            ("missing required fields", '{"size": 3}'),
        ],
    )
    def test_unusable_output_is_rejected(self, label: str, raw: str | None) -> None:
        assert _parse(raw) is None

    def test_an_invented_pattern_id_is_rejected(self) -> None:
        # A non-UUID id means the model made something up. Fail here, where the
        # reason is legible, rather than letting the mechanism layer drop it.
        raw = json.loads(valid_response())
        raw["focus_patterns"] = [{"pattern_id": "two-pointers", "weight": 1.0}]

        assert _parse(json.dumps(raw)) is None

    def test_an_unrecognized_confidence_falls_back_to_low(self) -> None:
        raw = json.loads(valid_response())
        raw["confidence"] = "certain"

        prescription = _parse(json.dumps(raw))

        assert prescription is not None
        assert prescription.confidence == "low"

    @pytest.mark.asyncio
    async def test_the_response_schema_is_sent_with_the_turn(self, codex) -> None:  # type: ignore[no-untyped-def]
        recorder = codex(FakeResult(valid_response()))

        await CodexCoachRuntime().prescribe(context())

        assert recorder["output_schema"] == PRESCRIPTION_SCHEMA


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


class TestPrompt:
    def test_it_carries_the_evidence_the_coach_must_cite(self) -> None:
        rendered = _render(context())

        assert str(PATTERN_ID) in rendered
        assert str(ATTEMPT_ID) in rendered
        assert "max-consecutive-ones" in rendered
        assert "complexity" in rendered

    def test_it_states_the_budget_and_the_size_ceiling(self) -> None:
        rendered = _render(context())

        assert "60 minutes/day" in rendered
        assert "at most 5 problems" in rendered

    def test_pattern_availability_is_spelled_out(self) -> None:
        # The three prerequisite states are the coach's main constraint, so they
        # must survive rendering as words rather than as flags it has to infer.
        rendered = _render(
            context(
                patterns=(
                    PatternSummary(
                        pattern_id=uuid4(),
                        slug="locked-one",
                        name="Locked",
                        band="unknown",
                        calibrated=False,
                        evidence_count=0,
                        unlocked=False,
                        provisional=False,
                    ),
                    PatternSummary(
                        pattern_id=uuid4(),
                        slug="provisional-one",
                        name="Provisional",
                        band="unknown",
                        calibrated=False,
                        evidence_count=0,
                        unlocked=True,
                        provisional=True,
                    ),
                )
            )
        )

        assert "locked" in rendered
        assert "provisional" in rendered

    def test_no_attempts_is_said_plainly_not_left_blank(self) -> None:
        # An empty section invites the model to fill the gap with an assumption.
        rendered = _render(context(recent_attempts=()))

        assert "none yet" in rendered
