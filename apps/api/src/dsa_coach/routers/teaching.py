"""Teaching and consent endpoints (spec §7.3, §8)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.models import AttemptCode, ConsentDecision
from dsa_coach.schemas import (
    ConsentIn,
    ConsentResultOut,
    ConsentStateOut,
    DiagnoseIn,
    HintIn,
    MockTurnIn,
    TeachingExchangeOut,
    TeachingOut,
)
from dsa_coach.services import consent as consent_service
from dsa_coach.services import teaching as teaching_service

router = APIRouter(tags=["teaching"], dependencies=[Depends(require_scope(Scope.DASHBOARD))])


def _to_out(result: teaching_service.TeachingResult) -> TeachingOut:
    return TeachingOut(
        ok=result.ok,
        kind=result.kind.value,
        text=result.text,
        hint_level=result.hint_level,
        level_description=result.level_description,
        degraded=result.degraded,
        runtime=result.runtime,
        cited_attempt_ids=list(result.cited_attempt_ids),
        reason=result.reason,
    )


# ------------------------------------------------------------------- consent


@router.get("/consents/code-capture", response_model=ConsentStateOut)
async def consent_state(user: CurrentUser, session: DbSession) -> ConsentStateOut:
    """The current decision, and the exact wording it was made against.

    Code capture is off by default, so a fresh user gets `decision: null` and
    `needs_prompt: true`.
    """
    state = await consent_service.state(session, user)
    stored = (
        await session.execute(
            select(func.count()).select_from(AttemptCode).where(AttemptCode.user_id == user.id)
        )
    ).scalar_one()

    return ConsentStateOut(
        decision=state.decision.value if state.decision else None,
        needs_prompt=state.needs_prompt,
        disclosure=state.disclosure,
        version=state.version,
        stored_snippets=stored,
    )


@router.post("/consents/code-capture", response_model=ConsentResultOut)
async def set_consent(
    payload: ConsentIn, user: CurrentUser, session: DbSession
) -> ConsentResultOut:
    decision = ConsentDecision(payload.decision)
    before = (
        await session.execute(
            select(func.count()).select_from(AttemptCode).where(AttemptCode.user_id == user.id)
        )
    ).scalar_one()

    await consent_service.record(session, user, decision)

    deleted = before if decision is ConsentDecision.NEVER else 0
    messages = {
        ConsentDecision.ONCE: (
            "Your code will be sent to OpenAI for this one request and not stored."
        ),
        ConsentDecision.ALWAYS: (
            "Your code will be sent to OpenAI when you ask for a diagnosis or review, "
            "and kept locally for 90 days. You can withdraw this at any time."
        ),
        ConsentDecision.NEVER: (
            f"Code capture is off and {deleted} stored snippet(s) were deleted."
        ),
    }
    return ConsentResultOut(
        decision=payload.decision, deleted_snippets=deleted, message=messages[decision]
    )


@router.delete("/consents/code-capture", response_model=ConsentResultOut)
async def revoke_consent(user: CurrentUser, session: DbSession) -> ConsentResultOut:
    """Withdraw consent and delete what was stored under it."""
    deleted = await consent_service.revoke(session, user)
    return ConsentResultOut(
        decision="never",
        deleted_snippets=deleted,
        message=f"Code capture withdrawn and {deleted} stored snippet(s) deleted.",
    )


# ------------------------------------------------------------------ teaching


@router.post("/coach/hint", response_model=TeachingOut)
async def hint(payload: HintIn, user: CurrentUser, session: DbSession) -> TeachingOut:
    """One rung up the ladder.

    `level` is a ceiling, not a jump: you get the next level up from what you
    have already seen. The level used is recorded against the problem, because a
    solve at level 4 is not the same evidence as a solve cold.
    """
    result = await teaching_service.give_hint(session, user, payload.problem_slug, payload.level)
    if not result.ok and result.reason.startswith("Unknown problem"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.reason)
    return _to_out(result)


@router.post("/coach/diagnose", response_model=TeachingOut)
async def diagnose(payload: DiagnoseIn, user: CurrentUser, session: DbSession) -> TeachingOut:
    """Explain what went wrong.

    Works without code — the answer is thinner and `degraded` says so — because
    requiring consent to get any help at all would make the consent meaningless.
    """
    result = await teaching_service.diagnose(session, user, payload.attempt_id, payload.code)
    if not result.ok and result.reason == "Unknown attempt.":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.reason)
    return _to_out(result)


@router.post("/coach/review", response_model=TeachingOut)
async def review(payload: DiagnoseIn, user: CurrentUser, session: DbSession) -> TeachingOut:
    """Review a solution that already passed. Accepted is not the interview bar."""
    result = await teaching_service.review(session, user, payload.attempt_id, payload.code)
    if not result.ok and result.reason == "Unknown attempt.":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.reason)
    return _to_out(result)


@router.post("/coach/mock/turn", response_model=TeachingOut)
async def mock_turn(payload: MockTurnIn, user: CurrentUser, session: DbSession) -> TeachingOut:
    """One exchange in a timed mock interview."""
    result = await teaching_service.mock_turn(session, user, payload.problem_slug, payload.message)
    if not result.ok and result.reason.startswith("Unknown problem"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.reason)
    return _to_out(result)


@router.get("/coach/history", response_model=list[TeachingExchangeOut])
async def history(user: CurrentUser, session: DbSession) -> list[TeachingExchangeOut]:
    exchanges = await teaching_service.history(session, user)
    return [TeachingExchangeOut.model_validate(e) for e in exchanges]
