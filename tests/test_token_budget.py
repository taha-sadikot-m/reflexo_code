"""Stage 3 token budget T3.4–T3.6 (mocked Redis)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.token_budget import (
    BUDGET_EXHAUSTED_MESSAGE,
    check_budget,
    record_usage,
    reset_usage,
    utc_day,
)
from identity.models import IdentityResult
from shared.redis_keys import token_budget_key


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.expires: dict[str, int] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def incrby(self, key: str, amount: int):
        n = int(self.data.get(key) or 0) + int(amount)
        self.data[key] = str(n)
        return n

    async def expire(self, key: str, ttl: int):
        self.expires[key] = ttl

    async def delete(self, key: str):
        self.data.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_t3_4_redis_counter_increments():
    redis = FakeRedis()
    bid = "biz-budget-1"
    used = await record_usage(bid, 40, redis=redis)
    assert used == 40
    used = await record_usage(bid, 60, redis=redis)
    assert used == 100
    key = token_budget_key(bid, utc_day())
    assert redis.data[key] == "100"
    assert redis.expires[key] == 86400 * 2
    assert await check_budget(bid, daily_budget=500_000, redis=redis) is True


@pytest.mark.asyncio
async def test_t3_5_budget_100_blocks():
    redis = FakeRedis()
    bid = "biz-budget-2"
    await record_usage(bid, 100, daily_budget=100, redis=redis)
    assert await check_budget(bid, daily_budget=100, redis=redis) is False
    assert await check_budget(bid, daily_budget=101, redis=redis) is True


@pytest.mark.asyncio
async def test_t3_6_reset_resumes():
    redis = FakeRedis()
    bid = "biz-budget-3"
    await record_usage(bid, 100, daily_budget=100, redis=redis)
    assert await check_budget(bid, daily_budget=100, redis=redis) is False
    await reset_usage(bid, redis=redis)
    assert await check_budget(bid, daily_budget=100, redis=redis) is True


@pytest.mark.asyncio
async def test_t3_5_session_returns_canned_skip_send(monkeypatch):
    from agent.session import handle_incoming_message

    async def _ident(**_kwargs):
        return IdentityResult(
            business_id="biz-1",
            customer_id="cust-1",
            is_owner=False,
            is_new_customer=False,
            customer_name=None,
            is_bot_suspected=False,
        )

    monkeypatch.setattr("agent.session.resolve_identity", _ident)
    monkeypatch.setattr("agent.session.check_budget", AsyncMock(return_value=False))
    send = AsyncMock()
    monkeypatch.setattr("agent.session.send_response", send)

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="What are your hours?",
        receiving_account_id="1054124847787624",
        skip_send=True,
    )
    assert out["status"] == "budget_exhausted"
    assert out["response_text"] == BUDGET_EXHAUSTED_MESSAGE
    send.assert_not_called()


@pytest.mark.asyncio
async def test_t3_5_session_sends_when_not_skip(monkeypatch):
    from agent.session import handle_incoming_message

    async def _ident(**_kwargs):
        return IdentityResult(
            business_id="biz-1",
            customer_id="cust-1",
            is_owner=False,
            is_new_customer=False,
            customer_name=None,
            is_bot_suspected=False,
        )

    monkeypatch.setattr("agent.session.resolve_identity", _ident)
    monkeypatch.setattr("agent.session.check_budget", AsyncMock(return_value=False))
    send = AsyncMock()
    monkeypatch.setattr("agent.session.send_response", send)

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="hours?",
        receiving_account_id="pnid",
        skip_send=False,
    )
    assert out["status"] == "budget_exhausted"
    send.assert_awaited()
    assert send.await_args.kwargs["text"] == BUDGET_EXHAUSTED_MESSAGE


class _FakeHuman:
    type = "human"

    def __init__(self, text: str) -> None:
        self.content = text


class _FakeAI:
    type = "ai"

    def __init__(self, total: int) -> None:
        self.content = "reply"
        self.usage_metadata = {
            "input_tokens": total - 10,
            "output_tokens": 10,
            "total_tokens": total,
        }


def test_only_current_turn_is_billed():
    """Checkpointed history keeps usage_metadata — billing it re-bills old turns."""
    from agent.observability import extract_token_usage
    from agent.session import _slice_current_turn

    messages = [
        _FakeHuman("turn 1"),
        _FakeAI(1000),
        _FakeHuman("turn 2"),
        _FakeAI(2000),
        _FakeHuman("turn 3 (current)"),
        _FakeAI(3000),
    ]

    whole_thread = extract_token_usage(messages)
    assert whole_thread is not None
    assert whole_thread["total"] == 6000

    current = extract_token_usage(_slice_current_turn(messages))
    assert current is not None
    assert current["total"] == 3000
    assert current["completion"] == 10


def test_current_turn_includes_react_tool_hops():
    """Multiple AI messages after the last human are all part of this turn."""
    from agent.observability import extract_token_usage
    from agent.session import _slice_current_turn

    messages = [
        _FakeHuman("old turn"),
        _FakeAI(5000),
        _FakeHuman("current"),
        _FakeAI(700),
        _FakeAI(300),
    ]
    usage = extract_token_usage(_slice_current_turn(messages))
    assert usage is not None
    assert usage["total"] == 1000
