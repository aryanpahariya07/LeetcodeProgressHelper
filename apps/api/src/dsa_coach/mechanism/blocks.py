"""Block assembly (spec §6.7).

Pure selection: given candidate pools and a budget, produce the block. The
service layer builds the pools from the database; nothing here touches it.

Two properties this must have, both tested:

- **Deterministic.** Same inputs and seed, same block. The mechanism layer is
  reproducible or it cannot be reasoned about.
- **Under-scheduling.** A block you finish builds momentum; one you never finish
  trains you to ignore the plan.

The mix is deliberately contaminated with interleaved problems. Practising one
pattern in a row teaches you to solve it *when you already know which pattern it
is* — which is not the interview condition.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.models import ItemRole


@dataclass(frozen=True)
class Candidate:
    problem_id: UUID
    rating: int
    minutes: int
    #: Predicted success from the primary readiness model, for ordering.
    predicted_score: float
    #: Patterns this problem exercises. Placement uses them to spread coverage
    #: across the foundations rather than drilling into one (spec §9).
    pattern_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class BlockMix:
    weakness: float = tuning.BLOCK_MIX_WEAKNESS
    interleaved: float = tuning.BLOCK_MIX_INTERLEAVED
    retention: float = tuning.BLOCK_MIX_RETENTION

    def counts(self, size: int) -> dict[ItemRole, int]:
        """Split a block size across roles, largest-remainder so it sums exactly."""
        raw = {
            ItemRole.WEAKNESS: self.weakness * size,
            ItemRole.INTERLEAVED: self.interleaved * size,
            ItemRole.RETENTION: self.retention * size,
        }
        counts = {role: int(value) for role, value in raw.items()}
        remainder = size - sum(counts.values())
        by_fraction = sorted(raw, key=lambda r: raw[r] - counts[r], reverse=True)
        for role in by_fraction[:remainder]:
            counts[role] += 1
        return counts


@dataclass(frozen=True)
class BlockItem:
    candidate: Candidate
    role: ItemRole


@dataclass(frozen=True)
class Block:
    items: tuple[BlockItem, ...]
    total_minutes: int
    budget_minutes: int
    shortfalls: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.items


def estimate_minutes(problem_rating: int, predicted_score: float) -> int:
    """Minutes a problem is likely to take.

    Two inputs, because they are known at different times. The rating is a fact
    from day one; readiness is not. Rating alone ignores that a problem gets
    quicker as you get better at its pattern; readiness alone gives every problem
    an identical estimate before any evidence exists — which is how a fresh
    account ended up with a one-item block.

    Clamped, so one bad estimate cannot distort a whole plan.
    """
    difficulty = (
        problem_rating / tuning.MINUTES_REFERENCE_RATING
    ) ** tuning.MINUTES_RATING_EXPONENT
    skill = 1.0 + tuning.MINUTES_SCORE_SPREAD * (0.5 - predicted_score)
    minutes = tuning.BASE_PROBLEM_MINUTES * difficulty * skill
    return int(max(tuning.MIN_PROBLEM_MINUTES, min(tuning.MAX_PROBLEM_MINUTES, minutes)))


def assemble_block(
    *,
    weakness: list[Candidate],
    interleaved: list[Candidate],
    retention: list[Candidate],
    budget_minutes: int,
    size: int,
    mix: BlockMix | None = None,
    seed: int = 0,
) -> Block:
    """Compose one block from the three pools.

    Roles are filled in priority order (retention, weakness, interleaved) and any
    unfilled quota is redistributed to whatever pools still have candidates, so a
    thin catalogue produces a smaller block rather than none. Every substitution
    is reported in `shortfalls` rather than passing silently.
    """
    mix = mix or BlockMix()
    quotas = mix.counts(size)
    rng = random.Random(seed)

    # On a small block the retention share rounds to zero, so overdue reviews are
    # crowded out entirely — the precise failure the retention-first ordering
    # below exists to prevent. Guarantee one slot whenever a review is actually
    # due; forgetting solved problems is the silent failure mode of self-directed
    # practice, and it must not lose to rounding.
    if retention and quotas[ItemRole.RETENTION] == 0:
        quotas[ItemRole.RETENTION] = 1
        donor = max((ItemRole.WEAKNESS, ItemRole.INTERLEAVED), key=lambda r: quotas[r])
        if quotas[donor] > 0:
            quotas[donor] -= 1

    pools: dict[ItemRole, list[Candidate]] = {
        ItemRole.WEAKNESS: _ordered(weakness, rng),
        ItemRole.INTERLEAVED: _ordered(interleaved, rng),
        ItemRole.RETENTION: _ordered(retention, rng),
    }

    budget = int(budget_minutes * tuning.BLOCK_BUDGET_FILL)
    chosen: list[BlockItem] = []
    used: set[UUID] = set()
    spent = 0
    shortfalls: list[str] = []

    # Retention first: overdue reviews are the thing most easily crowded out.
    order = (ItemRole.RETENTION, ItemRole.WEAKNESS, ItemRole.INTERLEAVED)

    for role in order:
        wanted = quotas[role]
        taken = 0
        for candidate in pools[role]:
            if taken >= wanted:
                break
            if candidate.problem_id in used:
                continue
            if spent + candidate.minutes > budget and chosen:
                break
            chosen.append(BlockItem(candidate=candidate, role=role))
            used.add(candidate.problem_id)
            spent += candidate.minutes
            taken += 1
        if taken < wanted:
            shortfalls.append(f"{role.value}: wanted {wanted}, placed {taken}")

    # Redistribute the shortfall so the block still fills the budget.
    if len(chosen) < size:
        for role in order:
            for candidate in pools[role]:
                if len(chosen) >= size:
                    break
                if candidate.problem_id in used:
                    continue
                if spent + candidate.minutes > budget:
                    continue
                chosen.append(BlockItem(candidate=candidate, role=role))
                used.add(candidate.problem_id)
                spent += candidate.minutes

    return Block(
        items=tuple(chosen),
        total_minutes=spent,
        budget_minutes=budget,
        shortfalls=tuple(shortfalls),
    )


def _ordered(pool: list[Candidate], rng: random.Random) -> list[Candidate]:
    """Deterministic ordering: most informative first.

    An attempt is most informative where the outcome is least certain, so
    candidates are sorted by distance from a coin flip. Ties break on problem id
    via a seeded shuffle, keeping the order stable but not alphabetical.
    """
    shuffled = list(pool)
    rng.shuffle(shuffled)
    return sorted(shuffled, key=lambda c: abs(c.predicted_score - 0.5))
