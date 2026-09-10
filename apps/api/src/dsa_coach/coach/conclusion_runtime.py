"""Turning a run sequence into a conclusion (spec §3.6).

One Codex call per solved or abandoned problem, over every snapshot at once.
Nothing runs during practice: the whole point of storing runs raw is that a
practice session never waits on a model, and a provider outage costs a delayed
conclusion rather than lost evidence.

**The sequence is the subject, not the final code.** Going straight to the
optimal approach and brute-forcing then rewriting after a TLE produce identical
final source and represent completely different skills. Every snapshot goes in,
in order, because the diffs between them are the evidence.

**What comes back is judgment, not fact** (invariant 1). It is validated, its
defect tags are forced back into the closed vocabulary, and it is stored in its
own table with a confidence that decides how far it moves anything. It never
touches `attempts`.

Containment is identical to the prescription runtime: read-only sandbox, no
approvals, ephemeral thread, empty working directory. Codex is a coding agent
and here it is only an inference endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from typing import Any

from dsa_coach import tuning
from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.mechanism import defects as defect_vocab

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotView:
    """One Run or Submit, as the coach sees it."""

    ordinal: int
    kind: str
    code: str


@dataclass(frozen=True)
class ConclusionRequest:
    """Everything the coach may reason from. Assembled server-side."""

    problem_slug: str
    problem_title: str
    language: str | None
    #: Catalogue-tagged patterns, so the coach can say which were *actually*
    #: used rather than inventing pattern names of its own.
    candidate_patterns: tuple[tuple[str, str], ...]  # (pattern_id, slug)
    outcome: str  # solved | abandoned
    snapshots: tuple[SnapshotView, ...]


@dataclass(frozen=True)
class Conclusion:
    """The validated result. Every field is the coach's judgement."""

    patterns_used: list[dict[str, Any]] = field(default_factory=list)
    blocker_observed: str | None = None
    final_complexity: str | None = None
    runs_before_pass: int = 0
    approach_changed: bool = False
    converged_at_run: int | None = None
    defects: list[dict[str, str | None]] = field(default_factory=list)
    confidence: str = "low"
    notes: str = ""


@dataclass(frozen=True)
class ConclusionOutcome:
    """Result of one attempt to conclude. Never raises — invariant 4."""

    conclusion: Conclusion | None = None
    failure: CoachFailure | None = None
    error_detail: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


BLOCKERS = (
    "pattern_not_recognized",
    "pattern_known_impl_failed",
    "edge_cases",
    "complexity",
    "data_structure_choice",
    "language_api",
    "misread_problem",
)

CONCLUSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patterns_used": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "string"},
                    "used": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["pattern_id", "used", "confidence"],
                "additionalProperties": False,
            },
        },
        "blocker_observed": {"type": ["string", "null"], "enum": [*BLOCKERS, None]},
        "final_complexity": {"type": ["string", "null"]},
        "runs_before_pass": {"type": "integer"},
        "approach_changed": {"type": "boolean"},
        "converged_at_run": {"type": ["integer", "null"]},
        "defects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "enum": list(defect_vocab.DEFECT_TAGS)},
                    "detail": {"type": ["string", "null"]},
                },
                "required": ["tag", "detail"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"},
    },
    "required": [
        "patterns_used",
        "blocker_observed",
        "final_complexity",
        "runs_before_pass",
        "approach_changed",
        "converged_at_run",
        "defects",
        "confidence",
        "notes",
    ],
    "additionalProperties": False,
}


def instructions() -> str:
    return f"""
You are reviewing one person's attempt at a programming problem, from the code
they ran at each step. Your job is to say what actually happened — not to teach,
not to rewrite their solution, and not to encourage.

You will be given every Run and Submit in order. The order matters more than the
final code: someone who went straight to the right approach and someone who
brute-forced it and rewrote after a timeout can finish with identical source and
have done something completely different.

Rules:

- Report only what the code shows. If the evidence is thin, set confidence low
  and say less. An invented observation is worse than a missing one.
- `patterns_used` may only name pattern ids from the list you are given. Say
  which were genuinely used. A problem tagged "hash map" solved with nested
  loops did not use a hash map, and saying so is the point of this exercise.
- `defects` must use tags from the vocabulary below. If something real does not
  fit any of them, use `other` and say what it was in `detail`. Do not invent
  tags — they are counted, and a name used once counts for nothing.
- `detail` is optional on known tags and useful when it locates the problem.
- `runs_before_pass` is how many runs happened before it worked; on an abandoned
  problem, how many happened in total.
- `approach_changed` means the strategy changed between runs, not that lines
  were edited.
- `notes` is one or two plain sentences to the person who wrote this code.

The defect vocabulary:
{defect_vocab.prompt_vocabulary()}
""".strip()


def render(request: ConclusionRequest) -> str:
    """The prompt. Plain text, because a person reads it when this misbehaves."""
    lines = [
        f"Problem: {request.problem_title} ({request.problem_slug})",
        f"Language: {request.language or 'unknown'}",
        f"Outcome: {request.outcome}",
        "",
        "Patterns this problem is tagged with (use these ids, no others):",
    ]
    if request.candidate_patterns:
        lines.extend(f"- {slug} (id {pid})" for pid, slug in request.candidate_patterns)
    else:
        lines.append("- none recorded; return an empty patterns_used")

    lines += ["", f"The sequence, {len(request.snapshots)} step(s):"]
    for snapshot in request.snapshots:
        lines += [
            "",
            f"--- step {snapshot.ordinal} ({snapshot.kind}) ---",
            snapshot.code,
        ]

    lines += ["", "Report what happened."]
    return "\n".join(lines)


class CodexConclusionRuntime:
    """Produces a conclusion through the local Codex app-server."""

    name = "codex"

    def __init__(self, model: str | None = None) -> None:
        self._model = model

    async def conclude(self, request: ConclusionRequest) -> ConclusionOutcome:
        """Never raises. A failed conclusion leaves the episode closed and empty."""
        if not request.snapshots:
            return ConclusionOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="No snapshots to conclude from.",
            )
        try:
            return await asyncio.wait_for(self._run(request), timeout=tuning.COACH_TIMEOUT_SECONDS)
        except TimeoutError:
            return ConclusionOutcome(
                failure=CoachFailure.TIMEOUT,
                error_detail="Codex did not respond in time.",
                model=self._model,
            )
        except Exception as error:
            # Sanitized: a provider message can echo the prompt back, and the
            # prompt is the user's source code.
            return ConclusionOutcome(
                failure=CoachFailure.PROVIDER_ERROR,
                error_detail=type(error).__name__,
                model=self._model,
            )

    async def _run(self, request: ConclusionRequest) -> ConclusionOutcome:
        from openai_codex import ApprovalMode, AsyncCodex, Sandbox

        with tempfile.TemporaryDirectory(prefix="dsa-coach-", ignore_cleanup_errors=True) as empty:
            codex = AsyncCodex()
            try:
                thread = await codex.thread_start(
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=True,
                    cwd=empty,
                    developer_instructions=instructions(),
                    model=self._model,
                )
                result = await thread.run(render(request), output_schema=CONCLUSION_SCHEMA)
            finally:
                await codex.close()

        if str(getattr(result.status, "value", result.status)) != "completed":
            return ConclusionOutcome(
                failure=CoachFailure.PROVIDER_ERROR,
                error_detail=f"Codex turn ended as {result.status}.",
                model=self._model,
            )

        conclusion = parse(result.final_response, request)
        if conclusion is None:
            return ConclusionOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="Codex returned something that is not a conclusion.",
                model=self._model,
            )

        return ConclusionOutcome(
            conclusion=conclusion,
            model=self._model or "codex",
            usage=_usage(result),
        )


def parse(raw: str | None, request: ConclusionRequest) -> Conclusion | None:
    """Validate a response into a `Conclusion`, or None if it is not one.

    Defect tags are forced back into the vocabulary and pattern ids are
    restricted to the ones offered. Both are the same guard: the coach may
    describe the attempt, but it does not get to extend the vocabulary or invent
    a pattern, because everything downstream counts these.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    offered = {pid for pid, _ in request.candidate_patterns}
    patterns: list[dict[str, Any]] = []
    for item in data.get("patterns_used", []):
        if not isinstance(item, dict):
            continue
        pattern_id = str(item.get("pattern_id", ""))
        if pattern_id not in offered:
            # An id nobody offered is invented. Dropped rather than stored,
            # since crediting or discrediting a pattern that is not on this
            # problem would corrupt readiness on nothing at all.
            continue
        patterns.append(
            {
                "pattern_id": pattern_id,
                "used": bool(item.get("used", False)),
                "confidence": _one_of(item.get("confidence"), ("low", "medium", "high"), "low"),
            }
        )

    blocker = data.get("blocker_observed")
    if blocker not in BLOCKERS:
        blocker = None

    raw_defects = data.get("defects", [])
    defect_list = defect_vocab.normalise(raw_defects if isinstance(raw_defects, list) else [])

    return Conclusion(
        patterns_used=patterns,
        blocker_observed=blocker,
        final_complexity=_str_or_none(data.get("final_complexity")),
        runs_before_pass=_int_or(data.get("runs_before_pass"), 0),
        approach_changed=bool(data.get("approach_changed", False)),
        converged_at_run=_int_or_none(data.get("converged_at_run")),
        defects=defect_list,
        confidence=_one_of(data.get("confidence"), ("low", "medium", "high"), "low"),
        notes=str(data.get("notes", "")),
    )


def _one_of(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _str_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_or(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(result: Any) -> dict[str, Any]:
    usage = getattr(result, "usage", None)
    total = getattr(usage, "total", None)
    if total is None:
        return {}
    return {
        "input_tokens": getattr(total, "input_tokens", None),
        "output_tokens": getattr(total, "output_tokens", None),
        "total_tokens": getattr(total, "total_tokens", None),
    }
