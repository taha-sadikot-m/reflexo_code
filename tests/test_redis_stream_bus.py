from __future__ import annotations

import asyncio

import pytest

from agent.redis_bus import run_incoming_subscriber
from mcp_servers.meta_mcp.webhooks.resolve import publish_event
from shared.redis_keys import INBOUND_EVENTS_GROUP, INBOUND_EVENTS_STREAM


@pytest.mark.asyncio
async def test_publish_event_persists_before_pubsub(monkeypatch):
    calls: list[tuple[str, object]] = []

    class FakeRedis:
        async def xadd(self, stream, fields, **kwargs):
            calls.append(("xadd", (stream, fields, kwargs)))
            return "1-0"

        async def publish(self, channel, payload):
            calls.append(("publish", (channel, payload)))
            return 1

    monkeypatch.setattr(
        "mcp_servers.meta_mcp.webhooks.resolve.get_redis",
        lambda: FakeRedis(),
    )
    await publish_event(
        "incoming:biz",
        {"event_type": "instagram_dm", "message_id": "mid.1"},
    )

    assert [name for name, _ in calls] == ["xadd", "publish"]
    stream, fields, options = calls[0][1]
    assert stream == INBOUND_EVENTS_STREAM
    assert '"message_id":"mid.1"' in fields["payload"]
    assert options["maxlen"] == 100_000


@pytest.mark.asyncio
async def test_failed_stream_entry_is_recovered_and_acked(monkeypatch, caplog):
    event = {
        "event_type": "facebook_comment",
        "business_id": "biz",
        "sender_id": "user",
        "channel": "facebook_comment",
        "message_id": "comment.1",
    }
    import json

    class FakeRedis:
        def __init__(self):
            self.acked: list[str] = []

        async def xgroup_create(self, *_args, **_kwargs):
            return None

        async def xautoclaim(self, stream, group, consumer, idle, **kwargs):
            return ["0-0", [("7-0", {"payload": json.dumps(event)})]]

        async def xreadgroup(self, *_args, **_kwargs):
            await asyncio.sleep(0.01)
            return []

        async def xack(self, stream, group, stream_id):
            assert stream == INBOUND_EVENTS_STREAM
            assert group == INBOUND_EVENTS_GROUP
            self.acked.append(stream_id)
            return 1

    redis = FakeRedis()
    monkeypatch.setattr("agent.redis_bus.get_redis", lambda: redis)
    caplog.set_level("INFO")

    first_stop = asyncio.Event()

    async def fail_once(_event):
        first_stop.set()
        raise RuntimeError("worker stopped mid-turn")

    await run_incoming_subscriber(
        fail_once,
        stop_event=first_stop,
        claim_idle_ms=0,
    )
    assert redis.acked == []

    second_stop = asyncio.Event()
    handled: list[str] = []

    async def succeed(recovered):
        handled.append(recovered["message_id"])
        second_stop.set()

    await run_incoming_subscriber(
        succeed,
        stop_event=second_stop,
        claim_idle_ms=0,
    )
    assert handled == ["comment.1"]
    assert redis.acked == ["7-0"]
    messages = "\n".join(caplog.messages)
    assert messages.index("EVENT_DISPATCH") < messages.index("EVENT_ACK")
