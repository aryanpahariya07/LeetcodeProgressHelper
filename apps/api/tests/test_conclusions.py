"""Turning a run sequence into a conclusion (spec §3.6).

No network. A real Codex call costs a ChatGPT quota and takes tens of seconds,
so the runtime is replaced with a scripted double and what gets tested is the
part that has to be right regardless of what the model says.

Two guarantees carry this file:

- **The vocabulary stays closed.** An invented defect tag is folded into
  `other` with the invented name kept in `detail`. Letting it through would mean
  a tag used once, and a tag used once cannot be counted — which is the only
  reason the vocabulary exists.
- **Patterns must be offered, not invented.** Crediting or discrediting a
  pattern that is not even on the problem would corrupt readiness on nothing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.coach.conclusion_runtime import (
    Conclusion,
    ConclusionOutcome,
    ConclusionRequest,
    SnapshotView,
    parse,
    render,
)
from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.mechanism import defects as defect_vocab
from dsa_coach.models import AttemptConclusion, Problem, User
from dsa_coach.services import snapshots as snapshot_service

NOW = datetime.now(UTC)
PATTERN_ID = "11111111-2222-3333-4444-555555555555"


def request(**overrides: object) -> ConclusionRequest:
    base: dict[str, object] = {
        "problem_slug": "two-sum",
        "problem_title": "Two Sum",
        "language": "cpp",
        "candidate_patterns": ((PATTERN_ID, "hashmap-counting"),),
        "outcome": "solved",
        "snapshots": (
            SnapshotView(1, "run", "brute force"),
            SnapshotView(2, "submit", "hash map"),
        ),
    }
    return ConclusionRequest(**{**base, **overrides})  # type: ignore[arg-type]


def response(**overrides: object) -> str:
    base: dict[str, object] = {
        "patterns_used": [{"pattern_id": PATTERN_ID, "used": True, "confidence": "high"}],
        "blocker_observed": "pattern_not_recognized",
        "final_complexity": "O(n)",
        "runs_before_pass": 3,
        "approach_changed": True,
        "converged_at_run": 2,
        "defects": [{"tag": "nested_loop_where_hash", "detail": "in the first two runs"}],
        "confidence": "medium",
        "notes": "Brute force for two runs, then a hash map.",
    }
    return json.dumps({**base, **overrides})


class TestVocabularyStaysClosed:
    def test_an_invented_tag_becomes_other(self) -> None:
        # The guarantee the whole vocabulary rests on. A tag used once counts
        # for nothing, so a new one must not enter by the model asserting it.
        result = parse(
            response(defects=[{"tag": "quadratic_disaster", "detail": "nested loops"}]),
            request(),
        )

        assert result is not None
        assert result.defects == [{"tag": "other", "detail": "[quadratic_disaster] nested loops"}]

    def test_the_invented_name_is_kept_not_discarded(self) -> None:
        # Reviewing what accumulates under `other` is how the vocabulary grows,
        # so the name the model reached for is the useful part.
        result = parse(response(defects=[{"tag": "some_new_idea", "detail": None}]), request())

        assert result is not None
        assert "some_new_idea" in (result.defects[0]["detail"] or "")

    def test_known_tags_pass_through_untouched(self) -> None:
        result = parse(
            response(defects=[{"tag": "off_by_one_bounds", "detail": "in the while"}]),
            request(),
        )

        assert result is not None
        assert result.defects == [{"tag": "off_by_one_bounds", "detail": "in the while"}]

    def test_every_schema_tag_is_in_the_vocabulary(self) -> None:
        # The schema's enum and the vocabulary are generated from the same dict;
        # this fails loudly if they ever drift apart.
        from dsa_coach.coach.conclusion_runtime import CONCLUSION_SCHEMA

        enum = CONCLUSION_SCHEMA["properties"]["defects"]["items"]["properties"]["tag"]["enum"]

        assert set(enum) == set(defect_vocab.DEFECT_TAGS)
        assert defect_vocab.OTHER in enum


class TestPatternsMustBeOffered:
    def test_an_uninvited_pattern_is_dropped(self) -> None:
        # Readiness is per-pattern. Crediting one that is not on this problem
        # would move an estimate on evidence about nothing.
        result = parse(
            response(
                patterns_used=[
                    {"pattern_id": PATTERN_ID, "used": True, "confidence": "high"},
                    {"pattern_id": str(uuid.uuid4()), "used": True, "confidence": "high"},
                ]
            ),
            request(),
        )

        assert result is not None
        assert [p["pattern_id"] for p in result.patterns_used] == [PATTERN_ID]

    def test_not_used_is_recorded_as_readily_as_used(self) -> None:
        # The correction that matters: a hash-map problem solved with nested
        # loops did not exercise hash maps, and saying so is the point.
        result = parse(
            response(
                patterns_used=[{"pattern_id": PATTERN_ID, "used": False, "confidence": "high"}]
            ),
            request(),
        )

        assert result is not None
        assert result.patterns_used[0]["used"] is False


class TestParsingIsDefensive:
    def test_an_unknown_blocker_becomes_none(self) -> None:
        result = parse(response(blocker_observed="vibes"), request())

        assert result is not None
        assert result.blocker_observed is None

    def test_an_unknown_confidence_falls_back_to_low(self) -> None:
        result = parse(response(confidence="certain"), request())

        assert result is not None
        assert result.confidence == "low"

    def test_prose_instead_of_json_is_rejected(self) -> None:
        assert parse("Here is your conclusion!", request()) is None

    def test_empty_output_is_rejected(self) -> None:
        assert parse("", request()) is None
        assert parse(None, request()) is None

    def test_a_valid_response_survives_intact(self) -> None:
        result = parse(response(), request())

        assert result is not None
        assert result.final_complexity == "O(n)"
        assert result.runs_before_pass == 3
        assert result.approach_changed is True
        assert result.converged_at_run == 2


class TestPrompt:
    def test_the_sequence_is_sent_in_order(self) -> None:
        # The order is the evidence: identical final code can come from going
        # straight to the answer or from rewriting after a timeout.
        rendered = render(request())

        assert rendered.index("step 1 (run)") < rendered.index("step 2 (submit)")
        assert "brute force" in rendered
        assert "hash map" in rendered

    def test_only_the_problem_s_own_patterns_are_offered(self) -> None:
        rendered = render(request())

        assert "hashmap-counting" in rendered
        assert PATTERN_ID in rendered

    def test_a_problem_with_no_patterns_says_so(self) -> None:
        rendered = render(request(candidate_patterns=()))

        assert "none recorded" in rendered

    def test_the_vocabulary_reaches_the_model(self) -> None:
        from dsa_coach.coach.conclusion_runtime import instructions

        text = instructions()

        assert "nested_loop_where_hash" in text
        assert "other" in text


# --------------------------------------------------------------------- service


class ScriptedRuntime:
    """Returns whatever it was handed. One scripted model behaviour each."""

    name = "scripted"

    def __init__(self, outcome: ConclusionOutcome) -> None:
        self.outcome = outcome
        self.seen: ConclusionRequest | None = None

    async def conclude(self, request: ConclusionRequest) -> ConclusionOutcome:
        self.seen = request
        return self.outcome


def snap(slug: str = "two-sum", kind: str = "run") -> dict:
    return {
        "snapshot_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "kind": kind,
        "language": "cpp",
        "code": f"// {kind}",
        "captured_at": NOW.isoformat(),
    }


async def abandoned_episode(client: AsyncClient, auth: dict[str, str]) -> str:
    await client.post("/consents/code-capture", json={"decision": "always"})
    # Distinct timestamps, as real runs have — a run and the submit that follows
    # it are not simultaneous.
    first, second = snap(), snap(kind="submit")
    second["captured_at"] = (NOW + timedelta(minutes=1)).isoformat()
    await client.post(
        "/extension/snapshots",
        json={"snapshots": [first, second]},
        headers=auth,
    )
    listed = (await client.get("/progress/unfinished")).json()
    problem_id = listed[0]["problem_id"]
    await client.post(f"/progress/unfinished/{problem_id}/abandon")
    return problem_id


class TestConclusionService:
    async def test_the_conclusion_is_written_to_its_own_row(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await abandoned_episode(onboarded, extension_auth)
        row = (await session.execute(select(AttemptConclusion))).scalar_one()
        runtime = ScriptedRuntime(
            ConclusionOutcome(
                conclusion=Conclusion(
                    blocker_observed="edge_cases",
                    defects=[{"tag": "empty_input_unhandled", "detail": None}],
                    confidence="high",
                    notes="Missed the empty case.",
                ),
                model="scripted-1",
            )
        )
        user = (await session.execute(select(User))).scalar_one()

        await snapshot_service.conclude(session, user, row, runtime=runtime)

        assert row.blocker_observed is not None
        assert row.blocker_observed.value == "edge_cases"
        assert row.defects == [{"tag": "empty_input_unhandled", "detail": None}]
        assert row.confidence == "high"
        assert row.runtime == "scripted"
        assert row.vocabulary_version == defect_vocab.VOCABULARY_VERSION

    async def test_the_whole_sequence_is_handed_over(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await abandoned_episode(onboarded, extension_auth)
        row = (await session.execute(select(AttemptConclusion))).scalar_one()
        runtime = ScriptedRuntime(ConclusionOutcome(conclusion=Conclusion()))
        user = (await session.execute(select(User))).scalar_one()

        await snapshot_service.conclude(session, user, row, runtime=runtime)

        assert runtime.seen is not None
        assert len(runtime.seen.snapshots) == 2
        assert [s.kind for s in runtime.seen.snapshots] == ["run", "submit"]

    async def test_a_failed_conclusion_leaves_the_episode_closed(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # Invariant 4. The coach being unavailable must not reopen an episode
        # the user has already given up on.
        await abandoned_episode(onboarded, extension_auth)
        row = (await session.execute(select(AttemptConclusion))).scalar_one()
        runtime = ScriptedRuntime(ConclusionOutcome(failure=CoachFailure.UNAVAILABLE))
        user = (await session.execute(select(User))).scalar_one()

        outcome = await snapshot_service.conclude(session, user, row, runtime=runtime)

        assert outcome.conclusion is None
        assert (await onboarded.get("/progress/unfinished")).json() == []
        # The row survives, empty, as the record that the episode ended.
        assert (await session.execute(select(AttemptConclusion))).scalar_one() is not None

    async def test_the_problem_s_own_patterns_are_offered(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await abandoned_episode(onboarded, extension_auth)
        row = (await session.execute(select(AttemptConclusion))).scalar_one()
        runtime = ScriptedRuntime(ConclusionOutcome(conclusion=Conclusion()))
        user = (await session.execute(select(User))).scalar_one()

        await snapshot_service.conclude(session, user, row, runtime=runtime)

        problem = (
            await session.execute(select(Problem).where(Problem.slug == "two-sum"))
        ).scalar_one()
        assert runtime.seen is not None
        assert runtime.seen.problem_slug == problem.slug
        assert len(runtime.seen.candidate_patterns) > 0
