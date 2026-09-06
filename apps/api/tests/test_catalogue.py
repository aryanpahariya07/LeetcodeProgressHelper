"""Catalogue seed integrity and provenance (spec §11).

The point of these tests is honesty about data quality: the catalogue must not
claim its ratings are measured when they are curator estimates, and nothing may
enter `problems` without a provenance row.
"""

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.catalogue import import_catalogue, load_catalogue
from dsa_coach.models import (
    CatalogueSource,
    Pattern,
    PatternPrerequisite,
    Problem,
    ProblemPattern,
    RatingSource,
)
from dsa_coach.tuning import CURRICULUM_BY_LEVEL, MANUAL_RATING_RD


class TestSeedIntegrity:
    async def test_seeds_the_expected_scale(self, seeded: AsyncSession) -> None:
        """Spec §11: 30-50 verified problems in Phase 0, not 150."""
        count = (await seeded.execute(select(func.count()).select_from(Problem))).scalar_one()

        assert 30 <= count <= 50

    async def test_every_problem_has_at_least_one_pattern(self, seeded: AsyncSession) -> None:
        orphans = (
            (
                await seeded.execute(
                    select(Problem.slug)
                    .outerjoin(ProblemPattern, ProblemPattern.problem_id == Problem.id)
                    .where(ProblemPattern.problem_id.is_(None))
                )
            )
            .scalars()
            .all()
        )

        assert orphans == []

    async def test_pattern_weights_are_valid(self, seeded: AsyncSession) -> None:
        links = (await seeded.execute(select(ProblemPattern))).scalars().all()

        assert all(0 < link.weight <= 1 for link in links)

    async def test_multi_pattern_weights_sum_to_one(self, seeded: AsyncSession) -> None:
        """Weights split an attempt's evidence across patterns (spec §6.2), so
        they must not inflate or lose evidence."""
        totals: dict[object, float] = defaultdict(float)
        for link in (await seeded.execute(select(ProblemPattern))).scalars().all():
            totals[link.problem_id] += link.weight

        assert all(abs(total - 1.0) < 1e-6 for total in totals.values())

    async def test_prerequisite_graph_is_acyclic(self, seeded: AsyncSession) -> None:
        edges = (await seeded.execute(select(PatternPrerequisite))).scalars().all()
        graph: dict[object, list[object]] = defaultdict(list)
        for edge in edges:
            graph[edge.pattern_id].append(edge.requires_pattern_id)

        visiting: set[object] = set()
        done: set[object] = set()

        def has_cycle(node: object) -> bool:
            if node in visiting:
                return True
            if node in done:
                return False
            visiting.add(node)
            found = any(has_cycle(child) for child in graph[node])
            visiting.discard(node)
            done.add(node)
            return found

        assert not any(has_cycle(node) for node in list(graph))

    async def test_no_pattern_requires_itself(self, seeded: AsyncSession) -> None:
        edges = (await seeded.execute(select(PatternPrerequisite))).scalars().all()

        assert all(e.pattern_id != e.requires_pattern_id for e in edges)

    async def test_every_curriculum_pattern_has_problems(self, seeded: AsyncSession) -> None:
        """A curriculum entry with no tagged problems would silently produce a
        shorter plan than intended."""
        for level, slugs in CURRICULUM_BY_LEVEL.items():
            for slug in slugs:
                count = (
                    await seeded.execute(
                        select(func.count())
                        .select_from(ProblemPattern)
                        .join(Pattern, Pattern.id == ProblemPattern.pattern_id)
                        .where(Pattern.slug == slug)
                    )
                ).scalar_one()
                assert count >= 2, f"{level.value}: pattern {slug!r} has {count} problems"


class TestProvenance:
    async def test_ratings_are_declared_manual_not_measured(self, seeded: AsyncSession) -> None:
        """Invariant 5: never present an estimate as a measurement."""
        problems = (await seeded.execute(select(Problem))).scalars().all()

        assert all(p.rating_source is RatingSource.MANUAL for p in problems)
        assert all(p.rating_rd == MANUAL_RATING_RD for p in problems)

    async def test_every_problem_links_to_a_provenance_row(self, seeded: AsyncSession) -> None:
        unsourced = (
            await seeded.execute(
                select(func.count())
                .select_from(Problem)
                .where(Problem.catalogue_source_id.is_(None))
            )
        ).scalar_one()

        assert unsourced == 0

    async def test_source_records_licence_and_known_limitations(self, seeded: AsyncSession) -> None:
        source = (await seeded.execute(select(CatalogueSource))).scalar_one()

        assert source.license
        assert source.checksum
        assert "UNVALIDATED ESTIMATES" in source.known_limitations

    async def test_checksum_matches_the_file_on_disk(self, seeded: AsyncSession) -> None:
        _, checksum = load_catalogue()
        source = (await seeded.execute(select(CatalogueSource))).scalar_one()

        assert source.checksum == checksum


class TestImportIdempotency:
    async def test_reimporting_creates_no_duplicates(self, seeded: AsyncSession) -> None:
        before = (await seeded.execute(select(func.count()).select_from(Problem))).scalar_one()

        result = await import_catalogue(seeded)
        await seeded.commit()

        after = (await seeded.execute(select(func.count()).select_from(Problem))).scalar_one()
        assert after == before
        assert result.problems_created == 0
        assert result.problems_updated == before

    async def test_reimporting_does_not_duplicate_pattern_links(self, seeded: AsyncSession) -> None:
        before = (
            await seeded.execute(select(func.count()).select_from(ProblemPattern))
        ).scalar_one()

        await import_catalogue(seeded)
        await seeded.commit()

        after = (
            await seeded.execute(select(func.count()).select_from(ProblemPattern))
        ).scalar_one()
        assert after == before
