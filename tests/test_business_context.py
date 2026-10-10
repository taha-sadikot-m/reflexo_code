"""Free business_context storage — merge path and prompts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agent.broker_prompts import build_broker_system_prompt
from agent.specialist_prompts import build_config_mgmt_prompt, build_inquiry_prompt
from mcp_servers.reflexo_core.tools.config import (
    ALLOWED_FIELDS,
    merge_context_path,
    parse_context_path,
    upsert_business_context_impl,
)
from onboarding.prompts import build_onboarding_system_prompt


def test_parse_context_path_accepts_dotted_snake_case():
    assert parse_context_path("contact.email") == ["contact", "email"]
    assert parse_context_path(" extra/parking ") == ["extra", "parking"]
    assert parse_context_path("CONTACT.Email") == ["contact", "email"]


def test_parse_context_path_rejects_bad_input():
    assert parse_context_path("") is None
    assert parse_context_path("1email") is None
    assert parse_context_path("contact.bad-key") is None
    assert parse_context_path(".") is None


def test_merge_keeps_sibling_keys():
    old, ctx = merge_context_path({"contact": {"phone": "1"}}, ["contact", "email"], "a@b.com")
    assert old is None
    assert ctx == {"contact": {"phone": "1", "email": "a@b.com"}}
    old2, ctx2 = merge_context_path(ctx, ["extra", "parking"], "basement")
    assert old2 is None
    assert ctx2["contact"]["email"] == "a@b.com"
    assert ctx2["extra"]["parking"] == "basement"


def test_merge_overwrites_leaf_only():
    ctx0 = {"contact": {"email": "old@x.com", "phone": "9"}}
    old, ctx = merge_context_path(ctx0, ["contact", "email"], "new@x.com")
    assert old == "old@x.com"
    assert ctx["contact"]["phone"] == "9"
    assert ctx["contact"]["email"] == "new@x.com"


def test_update_business_info_does_not_allow_contact_email_column():
    assert "contact_email" not in ALLOWED_FIELDS
    assert "owner_email" not in ALLOWED_FIELDS
    assert "business_context" in ALLOWED_FIELDS


@pytest.mark.asyncio
async def test_upsert_business_context_impl_merges(monkeypatch):
    store = {"business_context": {"contact": {"phone": "+91"}}}

    async def fake_exists(_bid: str) -> bool:
        return True

    async def fake_row(_bid: str):
        return store

    executed: list[str] = []

    @asynccontextmanager
    async def fake_conn():
        class _C:
            async def execute(self, sql, payload, bid):
                executed.append(payload)
                store["business_context"] = json.loads(payload)

        yield _C()

    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.config.business_exists", fake_exists
    )
    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.config.get_business_row", fake_row
    )
    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.config.service_connection", fake_conn
    )
    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.config.cache_delete", AsyncMock()
    )

    first = await upsert_business_context_impl(
        "b1", "contact.email", "taha.sadikot.m@gmail.com"
    )
    assert first["status"] == "updated"
    assert first["context"]["contact"]["phone"] == "+91"
    assert first["context"]["contact"]["email"] == "taha.sadikot.m@gmail.com"

    second = await upsert_business_context_impl("b1", "extra.parking", "basement")
    assert second["context"]["contact"]["email"] == "taha.sadikot.m@gmail.com"
    assert second["context"]["extra"]["parking"] == "basement"
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_upsert_rejects_invalid_path(monkeypatch):
    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.config.business_exists",
        AsyncMock(return_value=True),
    )
    out = await upsert_business_context_impl("b1", "not a path!!!", "x")
    assert "error" in out
    assert out["error"]["code"] == "INVALID_INPUT"


def test_config_prompt_forbids_invented_columns():
    prompt = build_config_mgmt_prompt(
        business_config={"name": "Biz", "persona_name": "A", "owner_name": "Ravi"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "upsert_business_context" in prompt
    assert "contact.email" in prompt
    assert "contact_email" in prompt
    assert "Never invent" in prompt or "never invent" in prompt.lower()


def test_config_prompt_retries_field_not_allowed_with_upsert():
    prompt = build_config_mgmt_prompt(
        business_config={"name": "Biz", "persona_name": "A", "owner_name": "Ravi"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "FIELD_NOT_ALLOWED" in prompt
    assert "retry the same fact with upsert_business_context" in prompt


def test_broker_prompt_forbids_refusing_from_memory():
    prompt = build_broker_system_prompt({"name": "HolonCode", "owner_name": "Taha"})
    assert "NEVER REFUSE FROM MEMORY" in prompt
    assert "outdated failures" in prompt
    assert "THIS turn" in prompt
    assert "free-storage business facts" in prompt


def test_inquiry_prompt_uses_stored_facts():
    prompt = build_inquiry_prompt(
        business_config={
            "name": "Biz",
            "persona_name": "A",
            "business_context": {"contact": {"email": "hello@biz.test"}},
        },
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "hello@biz.test" in prompt
    assert "STORED BUSINESS FACTS" in prompt


def test_onboarding_prompt_uses_upsert_not_full_replace():
    prompt = build_onboarding_system_prompt(
        business_config={"name": "Biz", "owner_name": "Ravi"},
        onboarding_progress=None,
        channel="whatsapp",
        is_voice=False,
    )
    assert "upsert_business_context" in prompt
    assert "contact.email" in prompt
