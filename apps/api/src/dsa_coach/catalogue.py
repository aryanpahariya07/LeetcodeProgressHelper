"""Catalogue import.

Idempotent: re-running updates existing rows and inserts new ones, keyed by
(provider, slug). Never fabricates a rating — every problem inherits the
`rating_source` and RD implied by its `catalogue_sources` row (spec §11).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import (
    CatalogueSource,
    Difficulty,
    Pattern,
    PatternPrerequisite,
    Problem,
    ProblemPattern,
    RatingSource,
)
from dsa_coach.tuning import CONTEST_RATING_RD, MANUAL_RATING_RD

DEFAULT_CATALOGUE = Path(__file__).parent / "data" / "catalogue.json"
LEETCODE_URL = "https://leetcode.com/problems/{slug}/"


@dataclass(frozen=True)
class ImportResult:
    source_name: str
    checksum: str
    patterns_created: int
    patterns_updated: int
    prerequisites_created: int
    problems_created: int
    problems_updated: int
    pattern_links: int

    def summary(self) -> str:
        return (
            f"{self.source_name} @ {self.checksum[:12]}: "
            f"patterns +{self.patterns_created}/~{self.patterns_updated}, "
            f"prereqs +{self.prerequisites_created}, "
            f"problems +{self.problems_created}/~{self.problems_updated}, "
            f"pattern links {self.pattern_links}"
        )


def load_catalogue(path: Path = DEFAULT_CATALOGUE) -> tuple[dict[str, Any], str]:
    """Read the catalogue file and return it with a checksum of its exact bytes."""
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    return json.loads(raw), checksum


async def import_catalogue(
    session: AsyncSession,
    path: Path = DEFAULT_CATALOGUE,
    *,
    rating_source: RatingSource = RatingSource.MANUAL,
) -> ImportResult:
    data, checksum = load_catalogue(path)
    meta = data["source"]

    source = await _upsert_source(session, meta, checksum)
    await session.flush()

    patterns_created, patterns_updated = await _upsert_patterns(session, data["patterns"])
    await session.flush()

    pattern_ids = await _pattern_id_map(session)
    prerequisites_created = await _upsert_prerequisites(
        session, data.get("prerequisites", []), pattern_ids
    )

    created, updated, links = await _upsert_problems(
        session, data["problems"], pattern_ids, source, rating_source
    )

    return ImportResult(
        source_name=meta["name"],
        checksum=checksum,
        patterns_created=patterns_created,
        patterns_updated=patterns_updated,
        prerequisites_created=prerequisites_created,
        problems_created=created,
        problems_updated=updated,
        pattern_links=links,
    )


async def _upsert_source(
    session: AsyncSession, meta: dict[str, Any], checksum: str
) -> CatalogueSource:
    existing = (
        await session.execute(select(CatalogueSource).where(CatalogueSource.name == meta["name"]))
    ).scalar_one_or_none()

    snapshot = date.fromisoformat(meta["snapshot_date"]) if meta.get("snapshot_date") else None
    fields = {
        "url": meta.get("url"),
        "license": meta["license"],
        "snapshot_date": snapshot,
        "version": meta["version"],
        "checksum": checksum,
        "transformation_notes": meta.get("transformation_notes", ""),
        "known_limitations": meta.get("known_limitations", ""),
    }

    if existing is None:
        source = CatalogueSource(name=meta["name"], **fields)
        session.add(source)
        return source

    for key, value in fields.items():
        setattr(existing, key, value)
    return existing


async def _upsert_patterns(session: AsyncSession, rows: list[dict[str, Any]]) -> tuple[int, int]:
    created = updated = 0
    for row in rows:
        existing = (
            await session.execute(select(Pattern).where(Pattern.slug == row["slug"]))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Pattern(
                    slug=row["slug"],
                    name=row["name"],
                    description=row.get("description", ""),
                    foundational=row.get("foundational", False),
                )
            )
            created += 1
        else:
            existing.name = row["name"]
            existing.description = row.get("description", "")
            existing.foundational = row.get("foundational", False)
            updated += 1
    return created, updated


async def _pattern_id_map(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Pattern.slug, Pattern.id))).all()
    return dict(rows)  # type: ignore[arg-type]


async def _upsert_prerequisites(
    session: AsyncSession, rows: list[dict[str, Any]], pattern_ids: dict[str, Any]
) -> int:
    created = 0
    for row in rows:
        pattern_id = pattern_ids[row["pattern"]]
        requires_id = pattern_ids[row["requires"]]
        existing = (
            await session.execute(
                select(PatternPrerequisite).where(
                    PatternPrerequisite.pattern_id == pattern_id,
                    PatternPrerequisite.requires_pattern_id == requires_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PatternPrerequisite(
                    pattern_id=pattern_id,
                    requires_pattern_id=requires_id,
                    strength=row.get("strength", 1.0),
                )
            )
            created += 1
        else:
            existing.strength = row.get("strength", 1.0)
    return created


async def _upsert_problems(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    pattern_ids: dict[str, Any],
    source: CatalogueSource,
    rating_source: RatingSource,
) -> tuple[int, int, int]:
    rd = MANUAL_RATING_RD if rating_source is RatingSource.MANUAL else CONTEST_RATING_RD
    created = updated = links = 0

    for row in rows:
        slug = row["slug"]
        existing = (
            await session.execute(
                select(Problem).where(Problem.provider == "leetcode", Problem.slug == slug)
            )
        ).scalar_one_or_none()

        fields = {
            "external_id": str(row["external_id"]),
            "title": row["title"],
            "url": LEETCODE_URL.format(slug=slug),
            "difficulty": Difficulty(row["difficulty"]),
            "rating": int(row["rating"]),
            "rating_rd": rd,
            "rating_source": rating_source,
            "catalogue_source_id": source.id,
            "is_active": row.get("is_active", True),
        }

        if existing is None:
            problem = Problem(provider="leetcode", slug=slug, **fields)
            session.add(problem)
            await session.flush()
            created += 1
        else:
            for key, value in fields.items():
                setattr(existing, key, value)
            problem = existing
            updated += 1

        links += await _sync_problem_patterns(session, problem, row["patterns"], pattern_ids)

    return created, updated, links


async def _sync_problem_patterns(
    session: AsyncSession,
    problem: Problem,
    pairs: list[list[Any]],
    pattern_ids: dict[str, Any],
) -> int:
    """Replace this problem's pattern edges with exactly what the file specifies."""
    wanted = {pattern_ids[slug]: float(weight) for slug, weight in pairs}

    existing_rows = (
        (
            await session.execute(
                select(ProblemPattern).where(ProblemPattern.problem_id == problem.id)
            )
        )
        .scalars()
        .all()
    )

    for link in existing_rows:
        if link.pattern_id not in wanted:
            await session.delete(link)
        else:
            link.weight = wanted.pop(link.pattern_id)

    for pattern_id, weight in wanted.items():
        session.add(ProblemPattern(problem_id=problem.id, pattern_id=pattern_id, weight=weight))

    return len(pairs)
