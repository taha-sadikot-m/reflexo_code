"""One inbound message must produce exactly one customer-visible reply.

The loop this locks down: a 15s specialist timeout fired, the customer was told
"I am experiencing a temporary issue", the turn was requeued with no message_id,
and each replay spoke again — the last one straight from the semantic cache at
similarity 1.0. Four messages for one question.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.error_handling import (
    HOLDING_MESSAGE,
    LLM_FALLBACK_MESSAGE,
    RETRY_MAX_ATTEMPTS,
    process_retry_queues,
    queue_for_retry,
)
from identity.models import IdentityResult
from shared.redis_keys import answered_message_key, retry_queue_key

BUSINESS = "918750d7-2254-4cfd-bd02-c22c3ab708ec"
MESSAGE_ID = "wamid.HBgMOTE5NDI3MDMwNjY2"


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key: str):
        return self.data.get(key)

    async def exists(self, key: str):
        return 1 if key in self.data else 0

    async def delete(self, key: str):
        self.data.pop(key, None)
        return 1

    async def expire(self, key: str, ttl: int):
        return True

    async def rpush(self, key: str, value: str):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key: str):
        items = self.lists.get(key) or []
        return items.pop(0) if items else None

    async def sadd(self, key: str, member: str):
        self.sets.setdefault(key, set()).add(member)
        return 1

    async def srem(self, key: str, member: str):
        self.sets.get(key, set()).discard(member)
        return 1

    async def smembers(self, key: str):
        return set(self.sets.get(key, set()))


class _Human:
    type = "human"

    def __init__(self, text: str) -> None:
        self.content = text


class _AI:
    type = "ai"

    def __init__(self, text: str) -> None:
        self.content = text
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """One shared FakeRedis behind every get_redis() the code paths reach."""
    fake = FakeRedis()
    for target in (
        "shared.redis_client.get_redis",
        "agent.error_handling.get_redis",
    ):
        monkeypatch.setattr(target, lambda: fake)
    return fake


@pytest.fixture
def session_env(monkeypatch: pytest.MonkeyPatch):
    """Stub everything around the agent invoke so the send path is testable.

    Runs as the owner: the customer post-hoc confidence rewrite is irrelevant to
    reply counting and would otherwise need a whole knowledge fixture.
    """

    async def _identity(**_kwargs):
        return IdentityResult(
            business_id=BUSINESS,
            customer_id=None,
            is_owner=True,
            is_new_customer=False,
            customer_name="Taha",
            is_bot_suspected=False,
        )

    monkeypatch.setattr("agent.session.resolve_identity", _identity)
    monkeypatch.setattr("agent.session.check_budget", AsyncMock(return_value=True))
    monkeypatch.setattr("agent.session.is_multi_agent_enabled", lambda: False)
    monkeypatch.setattr("agent.session.is_guardrails_enabled", lambda: False)
    monkeypatch.setattr("agent.session.is_semantic_cache_enabled", lambda: False)
    monkeypatch.setattr(
        "agent.session.load_business_config",
        AsyncMock(return_value={"name": "HolonCode", "onboarding_completed": True}),
    )
    monkeypatch.setattr(
        "agent.session.get_or_create_conversation",
        AsyncMock(return_value={"conversation_id": "conv-1", "recent_messages": []}),
    )
    monkeypatch.setattr(
        "agent.session.load_owner_cross_channel_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("agent.session.ensure_store", AsyncMock(return_value=None))
    monkeypatch.setattr("agent.session.get_checkpointer", AsyncMock(return_value=None))
    monkeypatch.setattr("agent.session.get_customer_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr("agent.session.create_agent_for_session", AsyncMock(return_value=object()))
    monkeypatch.setattr("agent.session.log_messages", AsyncMock())
    monkeypatch.setattr("agent.session.post_process", AsyncMock())
    monkeypatch.setattr("agent.session.record_usage", AsyncMock())
    monkeypatch.setattr("agent.session.notify_owner_of_outage", AsyncMock())
    monkeypatch.setattr(
        "agent.memory_tasks.enqueue_memory_extraction",
        AsyncMock(return_value=True),
    )

    send = AsyncMock()
    monkeypatch.setattr("agent.session.send_response", send)
    return send


class _Confidence:
    tier = "HIGH"
    score = 0.9
    reasons: list[str] = []
    hedge_response = False


@pytest.fixture
def as_customer(monkeypatch: pytest.MonkeyPatch):
    """Switch session_env to the customer path, which is where caching applies."""

    def _apply() -> None:
        async def _identity(**_kwargs):
            return IdentityResult(
                business_id=BUSINESS,
                customer_id="cust-1",
                is_owner=False,
                is_new_customer=False,
                customer_name="Taha",
                is_bot_suspected=False,
            )

        monkeypatch.setattr("agent.session.resolve_identity", _identity)
        monkeypatch.setattr("agent.session.check_message_limit", AsyncMock(return_value=True))
        monkeypatch.setattr("agent.session.check_bot_detection", AsyncMock(return_value=False))
        monkeypatch.setattr("agent.session.load_customer_profile", AsyncMock(return_value={}))
        monkeypatch.setattr("agent.session.load_cross_channel_context", AsyncMock(return_value=[]))
        monkeypatch.setattr("agent.session.load_customer_memory", AsyncMock(return_value=None))
        # Without tool calls the real scorer lands on LOW and rewrites the reply,
        # which would hide whether the cache or the model produced the text.
        monkeypatch.setattr("agent.session.calculate_confidence", lambda **_kw: _Confidence())

    return _apply


def _invoke_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def _boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr("agent.session._invoke_agent_with_llm_retries", _boom)


def _invoke_returns(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    async def _ok(*_args, **_kwargs):
        return {"messages": [_Human("hello"), _AI(text)]}

    monkeypatch.setattr("agent.session._invoke_agent_with_llm_retries", _ok)


# ── Part 1: never tell the customer we broke ─────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_sends_nothing_and_queues_one_retry(redis, session_env, monkeypatch):
    from agent.session import handle_incoming_message

    _invoke_raises(monkeypatch, TimeoutError("specialist timed out"))

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="what services do you provide",
        receiving_account_id="1054124847787624",
        external_message_id=MESSAGE_ID,
    )

    assert out["status"] == "error"
    assert out["outbound_sent"] is False
    session_env.assert_not_called()

    queued = redis.lists.get(retry_queue_key(BUSINESS)) or []
    assert len(queued) == 1
    entry = json.loads(queued[0])
    assert entry["message_id"] == MESSAGE_ID


@pytest.mark.asyncio
async def test_failed_turn_leaves_no_answered_marker(redis, session_env, monkeypatch):
    """Nothing was said, so the retry must still be free to speak."""
    from agent.session import handle_incoming_message

    _invoke_raises(monkeypatch, TimeoutError("boom"))
    await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="hours?",
        receiving_account_id="pnid",
        external_message_id=MESSAGE_ID,
    )

    assert answered_message_key("whatsapp", MESSAGE_ID) not in redis.data


def test_the_error_confession_is_not_customer_facing():
    assert "temporary issue" not in HOLDING_MESSAGE
    assert "temporary issue" in LLM_FALLBACK_MESSAGE, "kept for logs and owner alerts"

    import agent.session as session

    source = (session.__file__ or "").replace("\\", "/")
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "LLM_FALLBACK_MESSAGE" not in body


# ── Part 2: idempotent retries ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_send_marks_the_message_answered(redis, session_env, monkeypatch):
    from agent.session import handle_incoming_message

    _invoke_returns(monkeypatch, "We build AI assistants.")
    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="what services do you provide",
        receiving_account_id="pnid",
        external_message_id=MESSAGE_ID,
    )

    assert out["outbound"] == "pipeline_sent"
    session_env.assert_awaited_once()
    assert redis.data[answered_message_key("whatsapp", MESSAGE_ID)] == "1"


@pytest.mark.asyncio
async def test_replay_of_an_answered_message_stays_silent(redis, session_env, monkeypatch):
    from agent.session import handle_incoming_message

    redis.data[answered_message_key("whatsapp", MESSAGE_ID)] = "1"
    _invoke_returns(monkeypatch, "We build AI assistants.")

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="what services do you provide",
        receiving_account_id="pnid",
        external_message_id=MESSAGE_ID,
        is_retry=True,
    )

    assert out["outbound"] == "already_answered"
    session_env.assert_not_called()


@pytest.mark.asyncio
async def test_two_runs_of_one_message_send_once(redis, session_env, monkeypatch):
    """The end-to-end guarantee: run the same turn twice, hear back once."""
    from agent.session import handle_incoming_message

    _invoke_returns(monkeypatch, "We build AI assistants.")
    kwargs = dict(
        channel="instagram_dm",
        external_sender_id="17841400000000",
        message_text="what services do you provide",
        receiving_account_id="ig-account",
        external_message_id=MESSAGE_ID,
    )

    await handle_incoming_message(**kwargs)
    await handle_incoming_message(**kwargs, is_retry=True)

    assert session_env.await_count == 1


@pytest.mark.asyncio
async def test_send_failure_requeues_with_the_original_id(redis, session_env, monkeypatch):
    from agent.pipeline import SendFailedError
    from agent.session import handle_incoming_message

    session_env.side_effect = SendFailedError("whatsapp send failed: (#131005) Access denied")
    _invoke_returns(monkeypatch, "We build AI assistants.")

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="hours?",
        receiving_account_id="pnid",
        external_message_id=MESSAGE_ID,
    )

    assert out["outbound"] == "send_failed"
    entry = json.loads((redis.lists.get(retry_queue_key(BUSINESS)) or [""])[0])
    assert entry["message_id"] == MESSAGE_ID
    assert answered_message_key("whatsapp", MESSAGE_ID) not in redis.data


@pytest.mark.asyncio
async def test_a_retry_bypasses_the_inbound_claim_gate(redis, monkeypatch):
    """The claim is for Meta's re-deliveries; our own replay must get through."""
    import agent.session as session

    monkeypatch.setattr(session, "claim_inbound_message", AsyncMock(return_value=False))
    handled = AsyncMock(return_value={"status": "responded"})
    monkeypatch.setattr(session, "handle_incoming_message", handled)

    result = await session.handle_redis_event(
        {
            "event_type": "retry",
            "channel": "whatsapp",
            "sender_id": "+919427030666",
            "receiving_account_id": "pnid",
            "content": {"text": "hours?"},
            "message_id": MESSAGE_ID,
        }
    )

    assert result == {"status": "responded"}
    assert handled.await_args.kwargs["is_retry"] is True
    assert handled.await_args.kwargs["external_message_id"] == MESSAGE_ID


@pytest.mark.asyncio
async def test_a_genuine_redelivery_is_still_dropped(redis, monkeypatch):
    import agent.session as session

    monkeypatch.setattr(session, "claim_inbound_message", AsyncMock(return_value=False))
    handled = AsyncMock()
    monkeypatch.setattr(session, "handle_incoming_message", handled)

    result = await session.handle_redis_event(
        {
            "event_type": "message",
            "channel": "whatsapp",
            "sender_id": "+919427030666",
            "receiving_account_id": "pnid",
            "content": {"text": "hours?"},
            "message_id": MESSAGE_ID,
        }
    )

    assert result["status"] == "duplicate"
    handled.assert_not_called()


# ── Retry cap and holding message ────────────────────────────────────────────


def test_retry_cap_is_two():
    assert RETRY_MAX_ATTEMPTS == 2


@pytest.mark.asyncio
async def test_retries_stop_at_the_cap_then_hold(redis, monkeypatch):
    sends: list[dict[str, Any]] = []

    async def _send(**kwargs):
        sends.append(kwargs)

    monkeypatch.setattr("agent.pipeline.send_response", _send)

    await queue_for_retry(
        business_id=BUSINESS,
        customer_id=None,
        channel="whatsapp",
        message_text="what services do you provide",
        error="TimeoutError",
        external_sender_id="+919427030666",
        receiving_account_id="pnid",
        message_id=MESSAGE_ID,
    )

    attempts = 0

    async def _always_fails(_event):
        nonlocal attempts
        attempts += 1
        assert attempts <= RETRY_MAX_ATTEMPTS, "requeue never terminated"
        return {"status": "error"}

    ticks = 0
    while await process_retry_queues(_always_fails):
        ticks += 1
        assert ticks <= RETRY_MAX_ATTEMPTS, "requeue never terminated"

    assert attempts == RETRY_MAX_ATTEMPTS
    assert ticks == RETRY_MAX_ATTEMPTS, "each attempt must wait for the next tick"
    assert redis.lists.get(retry_queue_key(BUSINESS)) == [], "queue must drain"
    assert len(sends) == 1
    assert sends[0]["text"] == HOLDING_MESSAGE
    assert redis.data[answered_message_key("whatsapp", MESSAGE_ID)] == "1"


@pytest.mark.asyncio
async def test_a_failed_attempt_waits_for_the_next_tick(redis, monkeypatch):
    """An immediate re-attempt would burn the cap in seconds on a slow turn."""
    monkeypatch.setattr("agent.pipeline.send_response", AsyncMock())

    await queue_for_retry(
        business_id=BUSINESS,
        customer_id=None,
        channel="whatsapp",
        message_text="hours?",
        error="TimeoutError",
        external_sender_id="+919427030666",
        receiving_account_id="pnid",
        message_id=MESSAGE_ID,
    )

    calls = 0

    async def _fails(_event):
        nonlocal calls
        calls += 1
        return {"status": "error"}

    await process_retry_queues(_fails)

    assert calls == 1
    queued = redis.lists.get(retry_queue_key(BUSINESS)) or []
    assert len(queued) == 1
    assert json.loads(queued[0])["attempts"] == 1


@pytest.mark.asyncio
async def test_no_holding_message_when_the_customer_already_heard_back(redis, monkeypatch):
    sends: list[dict[str, Any]] = []

    async def _send(**kwargs):
        sends.append(kwargs)

    monkeypatch.setattr("agent.pipeline.send_response", _send)
    redis.data[answered_message_key("whatsapp", MESSAGE_ID)] = "1"

    from agent.error_handling import send_holding_message

    await send_holding_message(
        {
            "channel": "whatsapp",
            "external_sender_id": "+919427030666",
            "business_id": BUSINESS,
            "message_id": MESSAGE_ID,
        }
    )

    assert sends == []


@pytest.mark.asyncio
async def test_the_replayed_event_carries_the_message_id(redis):
    seen: list[dict[str, Any]] = []

    async def _capture(event):
        seen.append(event)
        return {"status": "responded"}

    await queue_for_retry(
        business_id=BUSINESS,
        customer_id="cust-1",
        channel="facebook_messenger",
        message_text="hours?",
        error="TimeoutError",
        external_sender_id="psid-1",
        receiving_account_id="page-1",
        message_id=MESSAGE_ID,
        comment_id=None,
    )
    await process_retry_queues(_capture)

    assert len(seen) == 1
    assert seen[0]["event_type"] == "retry"
    assert seen[0]["message_id"] == MESSAGE_ID


# ── Part 3: the semantic cache must not answer a replay ──────────────────────


@pytest.mark.asyncio
async def test_a_retry_does_not_read_the_semantic_cache(
    redis, session_env, as_customer, monkeypatch
):
    from agent.session import handle_incoming_message

    as_customer()
    monkeypatch.setattr("agent.session.is_semantic_cache_enabled", lambda: True)
    cache_get = AsyncMock(return_value="a stored answer the customer already has")
    monkeypatch.setattr("agent.session.get_cached_response", cache_get)
    monkeypatch.setattr("agent.session.embed_for_cache", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr("agent.session.set_cached_response", AsyncMock())
    _invoke_returns(monkeypatch, "the freshly generated answer")

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="what services do you provide",
        receiving_account_id="pnid",
        external_message_id="wamid.RETRY",
        is_retry=True,
    )

    cache_get.assert_not_called()
    assert out["response_text"] == "the freshly generated answer"


@pytest.mark.asyncio
async def test_a_normal_message_still_uses_the_cache(
    redis, session_env, as_customer, monkeypatch
):
    from agent.session import handle_incoming_message

    as_customer()
    monkeypatch.setattr("agent.session.is_semantic_cache_enabled", lambda: True)
    monkeypatch.setattr(
        "agent.session.get_cached_response", AsyncMock(return_value="cached answer")
    )
    monkeypatch.setattr("agent.session.embed_for_cache", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr("agent.session.set_cached_response", AsyncMock())
    _invoke_returns(monkeypatch, "should not be reached")

    out = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919427030666",
        message_text="what services do you provide",
        receiving_account_id="pnid",
        external_message_id="wamid.FRESH",
    )

    assert out["response_text"] == "cached answer"


# ── Part 4: the timeout that started it all ──────────────────────────────────


def test_specialist_timeout_has_room_for_a_tool_hop():
    from agent.config import MULTI_AGENT_CONFIG

    assert int(MULTI_AGENT_CONFIG["specialist_timeout_s"]) >= 45


# ── Part 5: WhatsApp access denied is a credential problem ───────────────────


def test_whatsapp_131005_maps_to_the_auth_path():
    from mcp_servers.meta_mcp.utils import graph_client
    from mcp_servers.meta_mcp.utils.errors import MetaAuthError

    response = httpx.Response(
        status_code=403,
        json={
            "error": {
                "message": "(#131005) Access denied",
                "type": "OAuthException",
                "code": 131005,
            }
        },
        request=httpx.Request("POST", "https://graph.facebook.com/v22.0/123/messages"),
    )

    with pytest.raises(MetaAuthError) as exc_info:
        graph_client._raise_for_meta_error(response)
    assert exc_info.value.code == "TOKEN_EXPIRED"
