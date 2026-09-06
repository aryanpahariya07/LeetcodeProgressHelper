"""Teaching features and code consent (spec §7.3, §8).

Two guarantees carry this phase:

- **A hint below level 5 never contains code.** The model is told not to and is
  not trusted; the check runs on every hint before the user sees it.
- **Withdrawing consent deletes what was stored.** Not "stops collecting" —
  deletes. A revocation that leaves the data behind is not a revocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.coach.teaching import (
    DiagnosisRequest,
    HintRequest,
    MockTurnRequest,
    ReviewRequest,
    StubTeachingRuntime,
    TeachingOutcome,
)
from dsa_coach.mechanism.complexity import Complexity, estimate, is_worse_than
from dsa_coach.mechanism.hints import (
    HintLevel,
    check_hint,
    contains_code,
    next_level,
)
from dsa_coach.models import Attempt, AttemptCode, Consent, TeachingExchange, User
from dsa_coach.services import consent as consent_service
from dsa_coach.services import teaching as teaching_service

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

PYTHON_SOLUTION = """
def two_sum(nums, target):
    seen = {}
    for i, n in enumerate(nums):
        if target - n in seen:
            return [seen[target - n], i]
        seen[n] = i
    return []
"""

QUADRATIC_SOLUTION = """
def two_sum(nums, target):
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            if nums[i] + nums[j] == target:
                return [i, j]
    return []
"""


def event(slug: str = "two-sum", *, resolution: str = "failed") -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": BASE.isoformat(),
        "resolution": resolution,
        "submission_outcome": "accepted" if resolution == "independent" else "wrong_answer",
        "blocker": None if resolution == "independent" else "pattern_known_impl_failed",
        "language": "python3",
        "active_seconds": 1200,
    }


async def log(client: AsyncClient, payload: dict[str, object]) -> str:
    response = await client.post("/attempts", json=payload)
    assert response.status_code == 201, response.text
    return str(response.json()["attempt_id"])


async def the_user(session: AsyncSession) -> User:
    return (await session.execute(select(User))).scalar_one()


class LeakyRuntime:
    """A model that ignores the no-code rule. It must not get through."""

    name = "leaky"

    async def hint(self, request: HintRequest) -> TeachingOutcome:
        return TeachingOutcome(
            text="Use a hash map:\n\n```python\nseen = {}\nfor i, n in enumerate(nums):\n    ...\n```"
        )

    async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome:
        return TeachingOutcome(text="ok")

    async def review(self, request: ReviewRequest) -> TeachingOutcome:
        return TeachingOutcome(text="ok")

    async def mock_turn(self, request: MockTurnRequest) -> TeachingOutcome:
        return TeachingOutcome(text="ok")


class ScriptedTeaching(StubTeachingRuntime):
    """Records what it was asked, so the request can be inspected."""

    name = "scripted"

    def __init__(self) -> None:
        self.last_diagnosis: DiagnosisRequest | None = None
        self.last_hint: HintRequest | None = None

    async def hint(self, request: HintRequest) -> TeachingOutcome:
        self.last_hint = request
        return TeachingOutcome(text=f"A nudge about {', '.join(request.patterns) or 'this'}.")

    async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome:
        self.last_diagnosis = request
        return TeachingOutcome(
            text="Your window shrinks before recording the answer.",
            degraded=request.code is None,
            cited_attempt_ids=tuple(f.attempt_id for f in request.prior_failures),
        )


# ------------------------------------------------------------- the hint ladder


class TestHintLadder:
    def test_levels_start_at_one(self) -> None:
        assert next_level(None) is HintLevel.PATTERN_FAMILY

    def test_each_request_advances_by_one(self) -> None:
        assert next_level(1) is HintLevel.GUIDING_QUESTION
        assert next_level(3) is HintLevel.APPROACH_OUTLINE

    def test_a_request_cannot_skip_ahead(self) -> None:
        """Otherwise 'give me a hint' quietly becomes 'give me the answer'."""
        assert next_level(1, requested=5) is HintLevel.GUIDING_QUESTION

    def test_a_lower_request_is_honoured(self) -> None:
        assert next_level(4, requested=2) is HintLevel.GUIDING_QUESTION

    def test_the_ladder_stops_at_five(self) -> None:
        assert next_level(5) is HintLevel.FULL_APPROACH
        assert next_level(99) is HintLevel.FULL_APPROACH

    @pytest.mark.parametrize(
        "text",
        [
            "```python\nx = 1\n```",
            "def solve(nums):",
            "for i in range(n):",
            "    indented_code_line = 2",
            "while (i < n) {",
            "if x > 0:",
            "return dp[i]",
            "arr[i] = arr[j];",
        ],
    )
    def test_code_is_detected(self, text: str) -> None:
        assert contains_code(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Think about what stays true as the window grows.",
            "This is a sliding window problem.",
            "What has to hold every time you move the right edge?",
            "Keep a running count of characters you have seen so far.",
        ],
    )
    def test_ordinary_prose_is_not_flagged(self, text: str) -> None:
        assert not contains_code(text)

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_a_hint_with_code_is_refused_below_level_five(self, level: int) -> None:
        result = check_hint(HintLevel(level), "Try:\n```python\nseen = {}\n```")

        assert not result.ok
        assert "must not contain code" in result.reason

    def test_level_five_may_include_a_snippet(self) -> None:
        result = check_hint(HintLevel.FULL_APPROACH, "```python\nseen = {}\n```")

        assert result.ok

    def test_an_empty_hint_is_refused(self) -> None:
        assert not check_hint(HintLevel.PATTERN_FAMILY, "   ").ok


class TestHintFlow:
    async def test_a_hint_is_recorded_with_its_level(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """The level is evidence: a solve at level 4 is not a solve cold."""
        result = await teaching_service.give_hint(
            session, await the_user(session), "two-sum", runtime=ScriptedTeaching()
        )

        assert result.ok
        assert result.hint_level == 1
        exchange = (await session.execute(select(TeachingExchange))).scalar_one()
        assert exchange.hint_level == 1

    async def test_successive_hints_climb_one_rung_at_a_time(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        user = await the_user(session)
        runtime = ScriptedTeaching()

        levels = []
        for _ in range(3):
            result = await teaching_service.give_hint(session, user, "two-sum", runtime=runtime)
            levels.append(result.hint_level)

        assert levels == [1, 2, 3]

    async def test_asking_for_level_five_first_gets_level_one(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        result = await teaching_service.give_hint(
            session, await the_user(session), "two-sum", 5, runtime=ScriptedTeaching()
        )

        assert result.hint_level == 1

    async def test_a_leaking_model_is_stopped(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """The guarantee that makes the ladder worth having."""
        result = await teaching_service.give_hint(
            session, await the_user(session), "two-sum", runtime=LeakyRuntime()
        )

        assert not result.ok
        assert result.text == ""
        assert "must not contain code" in result.reason

    async def test_a_refused_hint_is_not_recorded(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """A hint the user never saw must not count against their ladder."""
        await teaching_service.give_hint(
            session, await the_user(session), "two-sum", runtime=LeakyRuntime()
        )

        count = (
            await session.execute(select(func.count()).select_from(TeachingExchange))
        ).scalar_one()
        assert count == 0

    async def test_an_unknown_problem_is_a_404(self, onboarded: AsyncClient) -> None:
        response = await onboarded.post("/coach/hint", json={"problem_slug": "nope"})

        assert response.status_code == 404

    async def test_the_endpoint_says_what_the_level_reveals(self, onboarded: AsyncClient) -> None:
        result = (await onboarded.post("/coach/hint", json={"problem_slug": "two-sum"})).json()

        assert result["hint_level"] == 1
        assert result["level_description"]


# ----------------------------------------------------------------- complexity


class TestComplexity:
    def test_a_single_loop_is_linear(self) -> None:
        assert estimate(PYTHON_SOLUTION).time is Complexity.LINEAR

    def test_nested_loops_are_quadratic(self) -> None:
        assert estimate(QUADRATIC_SOLUTION).time is Complexity.QUADRATIC

    def test_a_sort_makes_it_linearithmic(self) -> None:
        code = "def f(a):\n    a.sort()\n    return a[0]\n"

        assert estimate(code).time is Complexity.LINEARITHMIC

    def test_empty_code_is_unknown(self) -> None:
        assert estimate("").time is Complexity.UNKNOWN
        assert estimate("# just a comment\n").time is Complexity.UNKNOWN

    def test_confidence_is_never_high(self) -> None:
        """It reads syntax, not meaning, and must not pretend otherwise."""
        assert estimate(QUADRATIC_SOLUTION).confidence in {"low", "medium"}

    def test_it_reports_what_it_noticed(self) -> None:
        assert estimate(QUADRATIC_SOLUTION).signals

    def test_the_display_hedges(self) -> None:
        assert estimate(PYTHON_SOLUTION).display.startswith("looks like")

    def test_a_worse_solution_is_flagged(self) -> None:
        """The one comparison worth surfacing on an accepted submission."""
        assert is_worse_than(Complexity.QUADRATIC, Complexity.LINEAR)
        assert not is_worse_than(Complexity.LINEAR, Complexity.QUADRATIC)

    def test_unknown_is_never_called_worse(self) -> None:
        assert not is_worse_than(Complexity.UNKNOWN, Complexity.LINEAR)


# -------------------------------------------------------------------- consent


class TestConsent:
    async def test_code_capture_is_off_by_default(self, onboarded: AsyncClient) -> None:
        state = (await onboarded.get("/consents/code-capture")).json()

        assert state["decision"] is None
        assert state["needs_prompt"] is True
        assert state["stored_snippets"] == 0

    async def test_the_disclosure_names_the_provider(self, onboarded: AsyncClient) -> None:
        """The user must know code leaves the machine before agreeing."""
        state = (await onboarded.get("/consents/code-capture")).json()

        assert "OpenAI" in state["disclosure"]
        assert "90 days" in state["disclosure"]
        assert "delete" in state["disclosure"].lower()

    async def test_once_does_not_persist_code(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "once"})

        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        count = (await session.execute(select(func.count()).select_from(AttemptCode))).scalar_one()
        assert count == 0, "'once' means once"

    async def test_always_persists_code(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "always"})

        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        stored = (await session.execute(select(AttemptCode))).scalar_one()
        assert PYTHON_SOLUTION.strip() in stored.code

    async def test_never_stores_nothing(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "never"})

        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        count = (await session.execute(select(func.count()).select_from(AttemptCode))).scalar_one()
        assert count == 0

    async def test_withdrawing_deletes_what_was_stored(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """A revocation that leaves the data behind is not a revocation."""
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        result = (await onboarded.delete("/consents/code-capture")).json()

        assert result["deleted_snippets"] == 1
        count = (await session.execute(select(func.count()).select_from(AttemptCode))).scalar_one()
        assert count == 0

    async def test_choosing_never_also_deletes(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        result = (await onboarded.post("/consents/code-capture", json={"decision": "never"})).json()

        assert result["deleted_snippets"] == 1

    async def test_deleting_code_leaves_the_evidence_intact(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Practice history is not collateral damage of a privacy choice."""
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        await onboarded.delete("/consents/code-capture")

        attempts = (await session.execute(select(func.count()).select_from(Attempt))).scalar_one()
        assert attempts == 1

    async def test_changed_wording_requires_asking_again(
        self, onboarded: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consent to a weaker disclosure is not consent to this one."""
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        before = (await onboarded.get("/consents/code-capture")).json()
        assert before["needs_prompt"] is False

        monkeypatch.setattr(
            consent_service, "CODE_CAPTURE_DISCLOSURE", "Materially different wording."
        )
        after = await consent_service.state(session, await the_user(session))

        assert after.needs_prompt is True

    async def test_a_new_decision_supersedes_the_old_one(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        await onboarded.post("/consents/code-capture", json={"decision": "never"})

        active = (
            (await session.execute(select(Consent).where(Consent.revoked_at.is_(None))))
            .scalars()
            .all()
        )
        assert len(active) == 1
        assert active[0].decision.value == "never"

    async def test_expired_code_is_purged(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "always"})
        await onboarded.post(
            "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
        )

        purged = await consent_service.purge_expired(
            session, now=datetime.now(UTC) + timedelta(days=consent_service.RETENTION_DAYS + 1)
        )

        assert purged == 1


# ------------------------------------------------------------------ diagnosis


class TestDiagnosis:
    async def test_it_works_without_code(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Requiring consent to get any help would make the consent meaningless."""
        attempt_id = await log(onboarded, event())

        result = (await onboarded.post("/coach/diagnose", json={"attempt_id": attempt_id})).json()

        assert result["ok"] is True
        assert result["degraded"] is True
        assert result["text"]

    async def test_it_is_not_degraded_when_code_is_supplied(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "once"})

        result = (
            await onboarded.post(
                "/coach/diagnose", json={"attempt_id": attempt_id, "code": PYTHON_SOLUTION}
            )
        ).json()

        assert result["degraded"] is False

    async def test_code_is_ignored_without_consent(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Sending code in the payload is not a substitute for agreeing to send it."""
        attempt_id = await log(onboarded, event())
        await onboarded.post("/consents/code-capture", json={"decision": "never"})
        runtime = ScriptedTeaching()

        await teaching_service.diagnose(
            session,
            await the_user(session),
            uuid.UUID(attempt_id),
            code=PYTHON_SOLUTION,
            runtime=runtime,
        )

        assert runtime.last_diagnosis is not None
        assert runtime.last_diagnosis.code is None

    async def test_only_supplied_attempts_may_be_cited(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Invariant 5: the coach does not get to invent history."""

        class Fabricator(ScriptedTeaching):
            async def diagnose(self, request: DiagnosisRequest) -> TeachingOutcome:
                return TeachingOutcome(
                    text="I remember this from before.",
                    cited_attempt_ids=(uuid.uuid4(), uuid.uuid4()),
                )

        attempt_id = await log(onboarded, event())

        result = await teaching_service.diagnose(
            session, await the_user(session), uuid.UUID(attempt_id), runtime=Fabricator()
        )

        assert result.cited_attempt_ids == ()

    async def test_real_prior_attempts_survive_citation(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("3sum"))
        attempt_id = await log(onboarded, event("two-sum"))
        runtime = ScriptedTeaching()

        result = await teaching_service.diagnose(
            session, await the_user(session), uuid.UUID(attempt_id), runtime=runtime
        )

        assert len(result.cited_attempt_ids) == 1

    async def test_an_unknown_attempt_is_a_404(self, onboarded: AsyncClient) -> None:
        response = await onboarded.post("/coach/diagnose", json={"attempt_id": str(uuid.uuid4())})

        assert response.status_code == 404


class TestReviewAndMock:
    async def test_review_without_code_says_so(self, onboarded: AsyncClient) -> None:
        attempt_id = await log(onboarded, event(resolution="independent"))

        result = (await onboarded.post("/coach/review", json={"attempt_id": attempt_id})).json()

        assert result["degraded"] is True
        assert "nothing to review" in result["text"].lower()

    async def test_review_uses_the_static_complexity_read(self, onboarded: AsyncClient) -> None:
        attempt_id = await log(onboarded, event(resolution="independent"))
        await onboarded.post("/consents/code-capture", json={"decision": "once"})

        result = (
            await onboarded.post(
                "/coach/review", json={"attempt_id": attempt_id, "code": QUADRATIC_SOLUTION}
            )
        ).json()

        assert "O(n^2)" in result["text"]

    async def test_a_mock_interview_needs_the_coach(self, onboarded: AsyncClient) -> None:
        """Honest refusal beats a scripted impression of an interviewer."""
        result = (
            await onboarded.post(
                "/coach/mock/turn",
                json={"problem_slug": "two-sum", "message": "I would use a hash map."},
            )
        ).json()

        assert result["ok"] is False
        assert "coach" in result["reason"].lower() or "coach" in result["text"].lower()

    async def test_history_is_available(self, onboarded: AsyncClient) -> None:
        await onboarded.post("/coach/hint", json={"problem_slug": "two-sum"})

        history = (await onboarded.get("/coach/history")).json()

        assert len(history) == 1
        assert history[0]["kind"] == "hint"


class TestStubHonesty:
    """The fallback must not pretend to abilities it does not have."""

    async def test_the_stub_admits_it_cannot_give_deep_hints(self) -> None:
        outcome = await StubTeachingRuntime().hint(
            HintRequest("two-sum", "Two Sum", ("hashmap-counting",), HintLevel.INVARIANT, 2)
        )

        assert outcome.text is not None
        assert "unavailable" in outcome.text

    async def test_the_stub_flags_a_repeating_blocker(self) -> None:
        """Something genuinely useful it can do without a model."""
        priors = tuple(
            teaching_service.PriorFailure(uuid.uuid4(), "x", "failed", "edge_cases")
            for _ in range(2)
        )
        outcome = await StubTeachingRuntime().diagnose(
            DiagnosisRequest(
                "two-sum", "Two Sum", (), "failed", "edge_cases", None, None, None, priors
            )
        )

        assert outcome.text is not None
        assert "3th time" in outcome.text or "repetition" in outcome.text

    async def test_the_stub_refuses_a_mock_interview(self) -> None:
        outcome = await StubTeachingRuntime().mock_turn(
            MockTurnRequest("two-sum", "Two Sum", (), (), "hello")
        )

        assert outcome.failure is CoachFailure.UNAVAILABLE
