"""Deterministic parser → stream → worker routing coverage for Meta channels."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest

from agent.pipeline import claim_inbound_message, send_response
from agent.redis_bus import run_incoming_subscriber
from mcp_servers.meta_mcp.auth.signature import verify_signature
from mcp_servers.meta_mcp.tools import facebook, instagram
from mcp_servers.meta_mcp.webhooks.handler import process_webhook_payload
from shared.redis_keys import INBOUND_EVENTS_STREAM


class MemoryRedis:
    def __init__(self) -> None:
        self.stream: list[tuple[str, dict[str, str]]] = []
        self.sent = False
        self.acked: list[str] = []
        self.keys: dict[str, str] = {}

    async def xadd(self, stream, fields, **_kwargs):
        assert stream == INBOUND_EVENTS_STREAM
        stream_id = f"{len(self.stream) + 1}-0"
        self.stream.append((stream_id, fields))
        return stream_id

    async def publish(self, *_args):
        return 1

    async def xgroup_create(self, *_args, **_kwargs):
        return None

    async def xautoclaim(self, *_args, **_kwargs):
        return ["0-0", []]

    async def xreadgroup(self, *_args, **_kwargs):
        if not self.sent:
            self.sent = True
            return [(INBOUND_EVENTS_STREAM, list(self.stream))]
        await asyncio.sleep(0.01)
        return []

    async def xack(self, _stream, _group, stream_id):
        self.acked.append(stream_id)
        return 1

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True


CASES = [
    (
        "instagram_dm",
        {
            "object": "instagram",
            "entry": [
                {
                    "id": "IG_ACCOUNT",
                    "messaging": [
                        {
                            "sender": {"id": "IG_USER"},
                            "recipient": {"id": "IG_ACCOUNT"},
                            "timestamp": 1721300000,
                            "message": {"mid": "ig-mid-1", "text": "DM"},
                        }
                    ],
                }
            ],
        },
        "send_instagram_dm",
        "IG_USER",
        None,
    ),
    (
        "instagram_comment",
        {
            "object": "instagram",
            "entry": [
                {
                    "id": "IG_ACCOUNT",
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "id": "ig-comment-1",
                                "text": "Price?",
                                "from": {"id": "IG_COMMENTER", "username": "buyer"},
                                "media": {"id": "ig-post-1"},
                            },
                        }
                    ],
                }
            ],
        },
        "reply_instagram_comment",
        "IG_COMMENTER",
        "ig-comment-1",
    ),
    (
        "facebook_messenger",
        {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_1",
                    "messaging": [
                        {
                            "sender": {"id": "PSID_1"},
                            "recipient": {"id": "PAGE_1"},
                            "timestamp": 1721300000,
                            "message": {"mid": "fb-mid-1", "text": "DM"},
                        }
                    ],
                }
            ],
        },
        "send_facebook_message",
        "PSID_1",
        None,
    ),
    (
        "facebook_comment",
        {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_1",
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "verb": "add",
                                "comment_id": "fb-comment-1",
                                "post_id": "fb-post-1",
                                "parent_id": "fb-parent-1",
                                "message": "Available?",
                                "from": {"id": "FB_COMMENTER", "name": "Buyer"},
                            },
                        }
                    ],
                }
            ],
        },
        "reply_facebook_comment",
        "FB_COMMENTER",
        "fb-comment-1",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel,payload,expected_tool,sender,comment_id",
    CASES,
    ids=[case[0] for case in CASES],
)
async def test_signed_webhook_to_worker_outbound_selection(
    monkeypatch, channel, payload, expected_tool, sender, comment_id
):
    redis = MemoryRedis()
    monkeypatch.setattr(
        "mcp_servers.meta_mcp.webhooks.resolve.get_redis", lambda: redis
    )
    monkeypatch.setattr("agent.redis_bus.get_redis", lambda: redis)
    monkeypatch.setattr("shared.redis_client.get_redis", lambda: redis)

    async def resolve(_channel, _account):
        return "00000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(
        "mcp_servers.meta_mcp.webhooks.instagram.resolve_business_from_channel",
        resolve,
    )
    monkeypatch.setattr(
        "mcp_servers.meta_mcp.webhooks.facebook.resolve_business_from_channel",
        resolve,
    )

    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "fixture-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, secret)
    await process_webhook_payload(payload)
    assert len(redis.stream) == 1
    parsed = json.loads(redis.stream[0][1]["payload"])
    assert parsed["sender_id"] == sender
    if comment_id:
        assert parsed["comment_id"] == comment_id
        assert parsed["post_id"]
    if channel == "facebook_comment":
        assert parsed["parent_id"] == "fb-parent-1"

    # Simulate replay of the same durable record. The inbound claim allows only
    # one customer-visible response while both records can be acknowledged.
    redis.stream.append(("2-0", dict(redis.stream[0][1])))
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_mcp_tool(_server, tool_name, args):
        calls.append((tool_name, args))
        return {"status": "sent", "message_id": "out-1"}

    monkeypatch.setattr("agent.pipeline.call_mcp_tool", call_mcp_tool)
    stop = asyncio.Event()
    processed = 0

    async def worker(event):
        nonlocal processed
        event_id = event.get("message_id") or event.get("comment_id")
        if await claim_inbound_message(event["channel"], event_id):
            await send_response(
                event["channel"],
                event["sender_id"],
                "Reply",
                event["business_id"],
                comment_id=event.get("comment_id"),
            )
        processed += 1
        if processed == 2:
            stop.set()

    task = asyncio.create_task(
        run_incoming_subscriber(worker, stop_event=stop, claim_idle_ms=0)
    )
    await asyncio.wait_for(task, timeout=2)

    assert len(calls) == 1
    assert calls[0][0] == expected_tool
    if comment_id:
        assert calls[0][1]["comment_id"] == comment_id
    else:
        assert sender in calls[0][1].values()
    assert set(redis.acked) == {"1-0", "2-0"}


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,tool_name,kwargs,expected_path",
    [
        (
            instagram,
            "send_instagram_dm",
            {"to_ig_user_id": "IG_USER", "text": "Reply"},
            "IG_ACCOUNT/messages",
        ),
        (
            instagram,
            "reply_instagram_comment",
            {"comment_id": "ig-comment-1", "text": "Reply"},
            "ig-comment-1/replies",
        ),
        (
            facebook,
            "send_facebook_message",
            {"to_psid": "PSID_1", "text": "Reply"},
            "PAGE_1/messages",
        ),
        (
            facebook,
            "reply_facebook_comment",
            {"comment_id": "fb-comment-1", "text": "Reply"},
            "fb-comment-1/comments",
        ),
    ],
)
async def test_each_channel_targets_exact_graph_operation(
    monkeypatch, module, tool_name, kwargs, expected_path
):
    async def yes(_business_id):
        return True

    async def no_limit(*_args):
        return None

    async def creds(_business_id, channel):
        if channel == "instagram_dm":
            return {
                "access_token": "not-logged",
                "channel_account_id": "IG_ACCOUNT",
                "ig_account_id": "IG_ACCOUNT",
            }
        return {
            "access_token": "not-logged",
            "channel_account_id": "PAGE_1",
            "page_id": "PAGE_1",
        }

    paths: list[str] = []

    async def graph_post(path, _token, _body, **_context):
        paths.append(path)
        return {"id": "out-1", "message_id": "out-1"}

    monkeypatch.setattr(module, "business_exists", yes)
    monkeypatch.setattr(module, "check_rate_limit", no_limit)
    monkeypatch.setattr(module, "ensure_valid_token", creds)
    monkeypatch.setattr(module, "graph_post", graph_post)
    mcp = _FakeMcp()
    module.register(mcp)
    result = await mcp.tools[tool_name](
        business_id="00000000-0000-0000-0000-000000000001",
        **kwargs,
    )
    assert result["status"] == "sent"
    assert paths == [expected_path]
