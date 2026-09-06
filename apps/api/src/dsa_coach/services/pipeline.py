"""What happens after an attempt is recorded.

Ordering matters:

1. **Retention** first — a re-solve's grade decides whether it is relevant.
2. **Relevance** is then marked on the attempt and stored, so the three-attempt
   trigger is durable and a retry cannot double-count.
3. **Readiness** models update, each logging a prediction.
4. **The trigger** evaluates, comparing against the last snapshot.

The evidence layer is already committed before any of this runs. If the mechanism
layer fails, the attempt is still recorded — facts are never lost because a
derived calculation went wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.mechanism.evidence import counted_attempt_index
from dsa_coach.models import Attempt, TriggerBatch, User
from dsa_coach.services import readiness as readiness_service
from dsa_coach.services import retention as retention_service
from dsa_coach.services import triggers as trigger_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingResult:
    counted_as_evidence: bool
    relevant_to_trigger: bool
    retention_scheduled: bool
    trigger: TriggerBatch | None


async def process_attempt(session: AsyncSession, user: User, attempt: Attempt) -> ProcessingResult:
    """Run the mechanism layer over one newly recorded attempt."""
    retention = await retention_service.apply_attempt(session, user, attempt)

    facts = await readiness_service.build_facts(session, user, attempt)
    already_counted = not attempt.is_resolve and counted_attempt_index(facts) is None

    relevant = trigger_service.mark_relevance(
        attempt,
        resolve_lapsed=retention.lapsed,
        already_counted=already_counted,
    )

    evidence = await readiness_service.apply_attempt(session, user, attempt)
    batch = await trigger_service.evaluate_if_due(session, user)

    return ProcessingResult(
        counted_as_evidence=evidence is not None,
        relevant_to_trigger=relevant,
        retention_scheduled=retention.scheduled,
        trigger=batch,
    )


async def process_safely(
    session: AsyncSession, user: User, attempt: Attempt
) -> ProcessingResult | None:
    """Process an attempt, never letting a mechanism failure lose the evidence."""
    try:
        return await process_attempt(session, user, attempt)
    except Exception:
        logger.exception("mechanism processing failed for attempt %s", attempt.id)
        return None
