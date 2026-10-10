"""Tests for complete_onboarding gate (mocked DB/progress)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from onboarding.flow import complete_onboarding
from onboarding.progress import OnboardingProgress


@pytest.mark.asyncio
async def test_complete_refuses_incomplete():
    progress = OnboardingProgress(business_id="b1", business_name="Only Name")

    with (
        patch("onboarding.flow.business_exists", AsyncMock(return_value=True)),
        patch("onboarding.flow.load_onboarding_progress", AsyncMock(return_value=progress)),
    ):
        result = await complete_onboarding("b1")

    assert "error" in result
    assert result["error"]["code"] == "ONBOARDING_INCOMPLETE"
    assert "owner_name" in result["error"]["required_remaining"]


@pytest.mark.asyncio
async def test_complete_success_path():
    progress = OnboardingProgress(
        business_id="b1",
        business_name="Cool Style",
        owner_name="Ravi",
        what_they_sell="clothes",
        payment_methods=["UPI"],
        business_hours={"mon": {"open": "10:00", "close": "20:00"}},
        return_policy="7 days",
        delivery_info="2-3 days",
        vertical="ecommerce",
    )

    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with (
        patch("onboarding.flow.business_exists", AsyncMock(return_value=True)),
        patch("onboarding.flow.load_onboarding_progress", AsyncMock(return_value=progress)),
        patch("onboarding.flow.save_onboarding_progress", AsyncMock()),
        patch(
            "onboarding.flow.get_business_row",
            AsyncMock(return_value={"vertical": "ecommerce"}),
        ),
        patch("onboarding.flow.service_connection", return_value=conn),
        patch("onboarding.flow.seed_vertical_knowledge", AsyncMock(return_value=5)),
        patch("onboarding.flow.cache_delete", AsyncMock()),
    ):
        result = await complete_onboarding("b1")

    assert result["status"] == "completed"
    assert result["vertical"] == "ecommerce"
    assert result["knowledge_seeded"] == 5


@pytest.mark.asyncio
async def test_complete_business_not_found():
    with patch("onboarding.flow.business_exists", AsyncMock(return_value=False)):
        result = await complete_onboarding("missing")
    assert result["error"]["code"] == "BUSINESS_NOT_FOUND"
