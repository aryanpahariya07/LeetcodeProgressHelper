"""Onboarding validation and provisional plan generation (spec §9)."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import Plan, PlanStatus


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("days_per_week", 0),
            ("days_per_week", 8),
            ("minutes_per_day", 5),
            ("minutes_per_day", 601),
            ("self_assessed_level", "expert"),
            ("display_name", ""),
            ("approx_problems_solved", -1),
        ],
    )
    async def test_rejects_out_of_range_input(
        self,
        client: AsyncClient,
        onboarding_payload: dict[str, Any],
        field: str,
        value: Any,
    ) -> None:
        onboarding_payload[field] = value

        response = await client.post("/onboarding", json=onboarding_payload)

        assert response.status_code == 422

    async def test_rejects_a_second_onboarding(
        self, onboarded: AsyncClient, onboarding_payload: dict[str, Any]
    ) -> None:
        response = await onboarded.post("/onboarding", json=onboarding_payload)

        assert response.status_code == 409

    async def test_me_requires_onboarding_first(self, client: AsyncClient) -> None:
        response = await client.get("/me")

        assert response.status_code == 409


class TestProvisionalPlan:
    async def test_plan_is_returned_immediately(
        self, client: AsyncClient, onboarding_payload: dict[str, Any]
    ) -> None:
        """Spec §9: the user is never blocked from starting."""
        response = await client.post("/onboarding", json=onboarding_payload)

        plan = response.json()["plan"]
        assert plan["status"] == "provisional"
        assert len(plan["items"]) > 0

    async def test_plan_is_labelled_provisional_not_earned(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        plan = (await session.execute(select(Plan))).scalar_one()

        assert plan.status is PlanStatus.PROVISIONAL
        assert plan.version == 1
        assert "not yet based on any evidence" in plan.summary

    async def test_generation_context_records_that_no_scheduler_ran(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Phase 0 must not imply a scheduler exists."""
        plan = (await session.execute(select(Plan))).scalar_one()

        assert plan.generation_context["generator"] == "static_curriculum_lookup"

    async def test_today_returns_one_block_within_the_daily_budget(
        self, onboarded: AsyncClient
    ) -> None:
        response = await onboarded.get("/today")

        body = response.json()
        assert body["is_provisional"] is True
        assert len(body["items"]) > 0
        # 60 min/day * 0.85 fill = 51 min budget; under-schedules by design.
        assert body["total_target_minutes"] <= 60
        assert len({i["block_id"] for i in body["items"]}) == 1

    async def test_every_planned_problem_exists_in_the_catalogue(
        self, onboarded: AsyncClient
    ) -> None:
        """Invariant 5: no fabricated problem references."""
        plan = (await onboarded.get("/plan/current")).json()

        for item in plan["items"]:
            assert item["problem"] is not None
            assert item["problem"]["slug"]
            assert item["problem"]["url"].startswith("https://leetcode.com/problems/")

    async def test_beginner_plan_starts_with_low_rated_problems(
        self, onboarded: AsyncClient
    ) -> None:
        plan = (await onboarded.get("/plan/current")).json()

        ratings = [i["problem"]["rating"] for i in plan["items"]]
        assert max(ratings) <= 1600

    async def test_advanced_plan_differs_from_beginner_plan(
        self, client: AsyncClient, onboarding_payload: dict[str, Any]
    ) -> None:
        onboarding_payload["self_assessed_level"] = "advanced"

        response = await client.post("/onboarding", json=onboarding_payload)

        slugs = {i["problem"]["slug"] for i in response.json()["plan"]["items"]}
        assert "two-sum" not in slugs

    async def test_tight_budget_still_produces_a_usable_plan(
        self, client: AsyncClient, onboarding_payload: dict[str, Any]
    ) -> None:
        """A 10-minute day is shorter than any problem estimate; the plan must
        still contain something rather than being empty."""
        onboarding_payload["minutes_per_day"] = 10

        response = await client.post("/onboarding", json=onboarding_payload)

        assert len(response.json()["plan"]["items"]) > 0
