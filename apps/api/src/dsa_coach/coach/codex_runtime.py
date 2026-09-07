"""The Codex coach runtime (spec §2).

Codex authenticates with a ChatGPT account rather than an API key, which is why
this is the runtime for a local single-user install: no separate metered billing,
no key to manage, and the code never leaves the machine except as the prompt.

## Containment

Codex is a *coding agent* — it can normally read files, run commands and edit a
workspace. None of that is wanted here, and every turn is started with all of it
switched off:

- `Sandbox.read_only` — no writes to anything.
- `ApprovalMode.deny_all` — no command it proposes is ever approved.
- `ephemeral=True` — no thread history persists between calls.
- `cwd` set to an empty temporary directory — even reads see nothing of yours.

So Codex here is an inference endpoint that happens to be reached through an
agent runtime. It receives a bounded, sanitized `CoachContext` and returns a
block *shape*. It has no database handle, no tools, and structurally no way to
name a problem (invariant 3) — `BlockPrescription` has no field for one.

## Scope

This is licensed for local, single-user development use under a ChatGPT plan.
**Deploying or distributing this application would require a separate
authentication, billing and terms review** — a ChatGPT subscription is not
production API capacity for a service with users. See `docs/spec.md` §2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from typing import Any
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.coach.runtime import CoachContext, CoachFailure, CoachOutcome
from dsa_coach.mechanism.prescription import PatternWeight, Prescription

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are the DSA Coach. You decide the *shape* of someone's next practice block.

You will be given their readiness per pattern, their recent attempts with the
blocker they reported, their retention status, and their time budget. Everything
you need is in that context. Do not read files, run commands, or search — there
is nothing else to look up, and you have no access to anything.

You do not choose problems. Deterministic code selects the actual problems from
your prescription. Do not name problems and do not assume any exist.

Rules:

- Only target patterns marked "available". A "provisional" pattern has unproven
  prerequisites; targeting it means aiming at a guess.
- Never remove interleaving. Mixed practice transfers; blocked practice only
  feels better.
- Keep the block within the stated time budget.
- Ground the diagnosis in the attempts you were given, and cite their ids. If
  the evidence is thin, say so and set confidence low rather than inventing a
  pattern in the data.
- If nothing has changed enough to act on, say so and prescribe a continuation
  rather than manufacturing a change.

Reply with JSON matching the schema. The rationale is read by the person
practising, so write it as one or two plain sentences to them.
""".strip()

#: The response contract. Note the absence of any problem field — the coach
#: cannot name a problem because there is nowhere to put one.
PRESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "focus_patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["pattern_id", "weight"],
                "additionalProperties": False,
            },
        },
        "rating_band_low": {"type": "integer"},
        "rating_band_high": {"type": "integer"},
        "size": {"type": "integer"},
        "mix_weakness": {"type": "number"},
        "mix_interleaved": {"type": "number"},
        "mix_retention": {"type": "number"},
        "timed": {"type": "boolean"},
        "diagnosis": {"type": "string"},
        "rationale": {"type": "string"},
        "evidence_attempt_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "focus_patterns",
        "rating_band_low",
        "rating_band_high",
        "size",
        "mix_weakness",
        "mix_interleaved",
        "mix_retention",
        "timed",
        "diagnosis",
        "rationale",
        "evidence_attempt_ids",
        "confidence",
    ],
    "additionalProperties": False,
}


class CodexCoachRuntime:
    """Runs the coach through the local Codex app-server."""

    name = "codex"

    def __init__(self, model: str | None = None) -> None:
        self._model = model

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        """Ask for a block shape. Never raises — invariant 4 depends on it."""
        try:
            return await asyncio.wait_for(self._run(context), timeout=tuning.COACH_TIMEOUT_SECONDS)
        except TimeoutError:
            return CoachOutcome(
                failure=CoachFailure.TIMEOUT,
                error_detail="Codex did not respond in time.",
                model=self._model,
            )
        except Exception as error:
            return CoachOutcome(
                failure=_classify(error),
                # Sanitized: a provider message can echo the prompt back, and
                # the prompt contains the user's practice history.
                error_detail=type(error).__name__,
                model=self._model,
            )

    async def _run(self, context: CoachContext) -> CoachOutcome:
        from openai_codex import ApprovalMode, AsyncCodex, Sandbox

        # An empty directory, so even a read-only agent sees nothing of the
        # user's machine.
        #
        # `ignore_cleanup_errors` is not tidiness, it is correctness on Windows:
        # the Codex app-server keeps a handle on its cwd, so removing the
        # directory raises WinError 32 and — because that happens in
        # `__exit__` — throws away an otherwise successful turn. Codex is closed
        # inside the block to give it the best chance of releasing the handle
        # first; a stray temp directory is a far smaller problem than a
        # prescription lost to a cleanup error.
        with tempfile.TemporaryDirectory(prefix="dsa-coach-", ignore_cleanup_errors=True) as empty:
            codex = AsyncCodex()
            try:
                thread = await codex.thread_start(
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=True,
                    cwd=empty,
                    developer_instructions=INSTRUCTIONS,
                    # None means "let Codex choose", which is what we want by
                    # default — the SDK's own default tracks the current model.
                    model=self._model,
                )
                result = await thread.run(_render(context), output_schema=PRESCRIPTION_SCHEMA)
            finally:
                await codex.close()

        if str(getattr(result.status, "value", result.status)) != "completed":
            return CoachOutcome(
                failure=CoachFailure.PROVIDER_ERROR,
                error_detail=f"Codex turn ended as {result.status}.",
                model=self._model,
            )

        prescription = _parse(result.final_response)
        if prescription is None:
            return CoachOutcome(
                failure=CoachFailure.INVALID_OUTPUT,
                error_detail="Codex returned something that is not a prescription.",
                model=self._model,
            )

        return CoachOutcome(
            prescription=prescription,
            model=self._model or "codex",
            trace_id=getattr(result, "id", None),
            usage=_usage(result),
        )


def _classify(error: Exception) -> CoachFailure:
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "serverbusy" in name or "retrylimit" in name or "overload" in text:
        return CoachFailure.RATE_LIMITED
    if "timeout" in name or "timeout" in text:
        return CoachFailure.TIMEOUT
    if "transportclosed" in name:
        return CoachFailure.PROVIDER_ERROR
    # Not logged in, or Codex is not installed. Neither fixes itself on a retry.
    if "login" in text or "auth" in text or "not found" in text:
        return CoachFailure.UNAVAILABLE
    return CoachFailure.PROVIDER_ERROR


def _parse(raw: str | None) -> Prescription | None:
    """Turn the JSON response into a prescription, or None if it is not one."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    try:
        focus = tuple(
            PatternWeight(pattern_id=UUID(str(item["pattern_id"])), weight=float(item["weight"]))
            for item in data.get("focus_patterns", [])
        )
        evidence = tuple(UUID(str(value)) for value in data.get("evidence_attempt_ids", []))
    except (ValueError, KeyError, TypeError):
        # A malformed id is the model inventing something. The mechanism layer
        # would drop it anyway; failing here says why more clearly.
        return None

    try:
        return Prescription(
            focus_patterns=focus,
            rating_band=(int(data["rating_band_low"]), int(data["rating_band_high"])),
            size=int(data["size"]),
            mix=(
                float(data["mix_weakness"]),
                float(data["mix_interleaved"]),
                float(data["mix_retention"]),
            ),
            timed=bool(data.get("timed", False)),
            diagnosis=str(data.get("diagnosis", "")),
            rationale=str(data.get("rationale", "")),
            evidence_attempt_ids=evidence,
            confidence=(
                str(data.get("confidence", "low"))
                if data.get("confidence") in {"low", "medium", "high"}
                else "low"
            ),
        )
    except (KeyError, ValueError, TypeError):
        return None


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


def _render(context: CoachContext) -> str:
    """The prompt. Plain text, because a person reads it when the coach misbehaves."""
    lines = [
        f"Level: {context.level}. Budget: {context.minutes_per_day} minutes/day, "
        f"{context.days_per_week} days/week.",
        f"A block may hold at most {context.max_size} problems.",
        f"Catalogue ratings run {context.rating_range[0]}-{context.rating_range[1]}.",
    ]
    if context.target_companies:
        lines.append(f"Target companies: {', '.join(context.target_companies)}.")

    lines += ["", "Readiness by pattern:"]
    for pattern in context.patterns:
        state = "locked"
        if pattern.unlocked:
            state = "provisional" if pattern.provisional else "available"
        lines.append(
            f"- {pattern.slug} (id {pattern.pattern_id}): {pattern.band}, "
            f"{pattern.evidence_count} observations, {state}"
        )

    lines += ["", "Recent attempts:"]
    if context.recent_attempts:
        for attempt in context.recent_attempts:
            blocker = f", blocker {attempt.blocker}" if attempt.blocker else ""
            lines.append(
                f"- id {attempt.attempt_id}: {attempt.problem_slug} "
                f"(rating {attempt.problem_rating}, patterns {', '.join(attempt.patterns)}) "
                f"-> {attempt.resolution}{blocker}"
            )
    else:
        lines.append("- none yet")

    lines += [
        "",
        f"Retention: {context.retention_due} reviews due, "
        f"{context.retention_lapses} lapses recorded.",
    ]
    if context.trigger_reasons:
        lines.append("What prompted this review: " + "; ".join(context.trigger_reasons))

    lines += ["", "Prescribe the shape of the next block."]
    return "\n".join(lines)
