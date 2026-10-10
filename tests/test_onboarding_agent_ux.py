"""Onboarding / owner UX prompt and tool-allowlist checks."""

from __future__ import annotations

from agent.prompts import build_owner_system_prompt
from agent.tools import OWNER_CHANNEL_ACTION_TOOLS, OWNER_CONNECT_TOOLS, _OWNER_ONLY_CORE_TOOLS
from onboarding.prompts import build_onboarding_system_prompt
from onboarding.progress import OnboardingProgress
from onboarding.tools import ONBOARDING_CONNECT_TOOLS, ONBOARDING_EXTRA_CORE_TOOLS


def test_owner_allowlist_includes_all_channel_actions() -> None:
    for name in OWNER_CHANNEL_ACTION_TOOLS:
        assert name in _OWNER_ONLY_CORE_TOOLS, name
    for name in OWNER_CONNECT_TOOLS:
        assert name in _OWNER_ONLY_CORE_TOOLS, name
    assert "upsert_business_context" in _OWNER_ONLY_CORE_TOOLS


def test_onboarding_connect_tools_cover_oauth_and_api_key() -> None:
    assert "get_oauth_connect_url_tool" in ONBOARDING_CONNECT_TOOLS
    assert "list_connected_integrations" in ONBOARDING_CONNECT_TOOLS
    assert "get_integration_connect_instructions" in ONBOARDING_CONNECT_TOOLS
    assert "complete_onboarding" in ONBOARDING_EXTRA_CORE_TOOLS


def test_onboarding_prompt_turn_shape_and_connect_recovery() -> None:
    progress = OnboardingProgress(
        business_id="biz-1",
        owner_name="Taha",
        business_name="HolonCode",
        what_they_sell="IT services",
    )
    prompt = build_onboarding_system_prompt(
        {"id": "biz-1", "name": "HolonCode", "owner_name": "Taha"},
        progress,
        channel="whatsapp",
        is_voice=False,
    )
    assert "TURN SHAPE" in prompt
    assert "what next" in prompt.lower()
    assert "NO SILENT FAIL" in prompt
    assert "get_oauth_connect_url_tool" in prompt
    assert "NEXT_REQUIRED" in prompt
    assert "complete_onboarding" in prompt
    assert "WORKING EMPLOYEE DURING SETUP" in prompt


def test_onboarding_prompt_next_required_when_essentials_done() -> None:
    progress = OnboardingProgress(
        business_id="biz-1",
        business_name="HolonCode",
        owner_name="Taha",
        what_they_sell="IT",
        payment_methods=["manual"],
        business_hours={"mon-fri": {"open": "09:00", "close": "17:00"}},
        return_policy="n/a",
        delivery_info="remote",
    )
    assert progress.is_essential_complete()
    prompt = build_onboarding_system_prompt(
        {"id": "biz-1", "name": "HolonCode"},
        progress,
        channel="whatsapp",
        is_voice=False,
    )
    assert "required setup is complete" in prompt.lower()
    assert "activate" in prompt.lower()


def test_onboarding_prompt_lists_next_required_field() -> None:
    progress = OnboardingProgress(business_id="biz-1", owner_name="Taha")
    remaining = progress.get_required_remaining()
    assert "business_name" in remaining
    prompt = build_onboarding_system_prompt(
        {"id": "biz-1"},
        progress,
        channel="whatsapp",
        is_voice=False,
    )
    assert "NEXT_REQUIRED" in prompt
    assert "business_name" in prompt


def test_owner_prompt_connect_recovery_and_integrations() -> None:
    prompt = build_owner_system_prompt(
        {"name": "HolonCode", "owner_name": "Taha", "persona_name": "Maya"},
        channel="whatsapp",
        is_voice=False,
    )
    assert "NO SILENT FAIL" in prompt
    assert "send_email" in prompt
    assert "get_oauth_connect_url_tool" in prompt
    assert "list_connected_integrations" in prompt
    assert "generate_instagram_oauth_url" in prompt
    assert "generate_facebook_oauth_url" in prompt


def test_onboarding_prompt_names_meta_oauth_tools() -> None:
    progress = OnboardingProgress(
        business_id="biz-1",
        owner_name="Taha",
        business_name="HolonCode",
    )
    prompt = build_onboarding_system_prompt(
        {"id": "biz-1", "name": "HolonCode"},
        progress,
        channel="whatsapp",
        is_voice=False,
    )
    assert "generate_instagram_oauth_url" in prompt
    assert "generate_facebook_oauth_url" in prompt
