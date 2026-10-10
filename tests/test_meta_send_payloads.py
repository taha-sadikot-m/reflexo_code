"""Meta payloads that Meta itself rejects, locked down by tests.

Two live failures motivated these. Facebook Messenger sends paired
`messaging_type: RESPONSE` with `tag: HUMAN_AGENT`, which Meta rejects with
"(#100) Messaging type should be MESSAGE_TAG for tagged messages", so no reply
ever reached a customer. Separately, the Instagram webhook subscription asked
for a `comments` field that is not valid on a Page, so Meta rejected the whole
call and left even `messages` unsubscribed, silently.

Both are payload-shape bugs that unit tests catch and integration tests miss.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_servers.meta_mcp.tools import facebook
from onboarding import integrations


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def send_fb(monkeypatch: pytest.MonkeyPatch):
    """Register send_facebook_message with the Graph call captured."""
    sent: dict[str, Any] = {}

    async def business_exists(business_id: str) -> bool:
        return True

    async def check_rate_limit(business_id: str, bucket: str) -> None:
        return None

    async def ensure_valid_token(business_id: str, channel: str) -> dict[str, Any]:
        return {"access_token": "page-token", "channel_account_id": "PAGE1"}

    async def graph_post(path, token, body, **kwargs):
        sent["path"] = path
        sent["body"] = body
        sent["kwargs"] = kwargs
        return {"message_id": "m_1"}

    monkeypatch.setattr(facebook, "business_exists", business_exists)
    monkeypatch.setattr(facebook, "check_rate_limit", check_rate_limit)
    monkeypatch.setattr(facebook, "ensure_valid_token", ensure_valid_token)
    monkeypatch.setattr(facebook, "graph_post", graph_post)

    mcp = _FakeMcp()
    facebook.register(mcp)
    return mcp.tools["send_facebook_message"], sent


# ── Facebook Messenger send payload ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_send_uses_response_without_a_tag(send_fb) -> None:
    fn, sent = send_fb

    result = await fn(business_id="biz-1", to_psid="PSID1", text="Hello")

    assert result["status"] == "sent"
    body = sent["body"]
    assert body["messaging_type"] == "RESPONSE"
    assert "tag" not in body, "a tag with RESPONSE is rejected by Meta as error #100"
    assert body["recipient"] == {"id": "PSID1"}
    assert body["message"] == {"text": "Hello"}
    assert sent["path"] == "PAGE1/messages"


@pytest.mark.asyncio
async def test_human_agent_switches_to_message_tag(send_fb) -> None:
    fn, sent = send_fb

    await fn(business_id="biz-1", to_psid="PSID1", text="Hi", human_agent=True)

    body = sent["body"]
    assert body["messaging_type"] == "MESSAGE_TAG"
    assert body["tag"] == "HUMAN_AGENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("human_agent", [False, True])
async def test_tag_never_accompanies_response(send_fb, human_agent: bool) -> None:
    """The invariant behind error #100, checked on both branches."""
    fn, sent = send_fb

    await fn(business_id="biz-1", to_psid="PSID1", text="Hi", human_agent=human_agent)

    body = sent["body"]
    assert not (body.get("messaging_type") == "RESPONSE" and "tag" in body)
    if "tag" in body:
        assert body["messaging_type"] == "MESSAGE_TAG"


@pytest.mark.asyncio
async def test_send_passes_channel_context_for_token_retry(send_fb) -> None:
    fn, sent = send_fb

    await fn(business_id="biz-1", to_psid="PSID1", text="Hi")

    assert sent["kwargs"]["business_id"] == "biz-1"
    assert sent["kwargs"]["channel"] == "facebook_messenger"


# ── Webhook subscribed_fields ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fields",
    [integrations.INSTAGRAM_WEBHOOK_FIELDS, integrations.FACEBOOK_WEBHOOK_FIELDS],
)
def test_subscribed_fields_are_valid_on_a_page(fields: str) -> None:
    """Both sets are POSTed to {page-id}/subscribed_apps.

    One invalid entry makes Meta reject the request outright, so nothing gets
    subscribed at all -- not even the valid fields alongside it.
    """
    requested = [f.strip() for f in fields.split(",") if f.strip()]
    assert requested, "an empty field list subscribes nothing"

    invalid = set(requested) - integrations.PAGE_VALID_SUBSCRIBED_FIELDS
    assert not invalid, f"not valid on a Page object: {sorted(invalid)}"


def test_instagram_fields_subscribe_messages_and_drop_comments() -> None:
    requested = integrations.INSTAGRAM_WEBHOOK_FIELDS.split(",")
    assert "messages" in requested, "without this, Instagram DMs never arrive"
    assert "comments" not in requested, (
        "comments is an instagram-object field, not a Page field; including it "
        "made Meta 400 the whole subscription"
    )


def test_facebook_fields_include_feed_for_page_comments() -> None:
    requested = integrations.FACEBOOK_WEBHOOK_FIELDS.split(",")
    assert "messages" in requested
    assert "feed" in requested, "Page post comments arrive on the feed field"


# ── Token must not leak through the subscribe helper ─────────────────────────


@pytest.mark.asyncio
async def test_subscribe_sends_token_in_body_not_query(monkeypatch) -> None:
    """Existing fields are retained and the token never enters a request URL."""
    captured: dict[str, Any] = {"get_urls": []}

    class _Response:
        status_code = 200

        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data
            self.text = json.dumps(data)

        def json(self) -> dict[str, Any]:
            return self.data

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url, **kwargs):
            captured["get_urls"].append(url)
            captured["get_headers"] = kwargs.get("headers")
            fields = ["feed"] if len(captured["get_urls"]) == 1 else ["feed", "messages"]
            return _Response(
                {
                    "data": [
                        {
                            "id": integrations._app_id(),
                            "subscribed_fields": fields,
                        }
                    ]
                }
            )

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            captured["data"] = kwargs.get("data")
            return _Response({"success": True})

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _Client)

    await integrations.subscribe_page_webhooks("PAGE1", "secret-token", "messages")

    assert all("secret-token" not in url for url in captured["get_urls"])
    assert captured["get_headers"] == {"Authorization": "Bearer secret-token"}
    assert "secret-token" not in captured["url"]
    assert not (captured["params"] or {}).get("access_token")
    assert captured["data"]["access_token"] == "secret-token"
    assert captured["data"]["subscribed_fields"] == "feed,messages"


@pytest.mark.asyncio
async def test_subscribe_error_message_excludes_the_token(monkeypatch) -> None:
    class _Response:
        status_code = 400
        text = '{"error":{"message":"(#100) Invalid field"}}'

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "(#100) Invalid field"}}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url, **kwargs):
            return type(
                "ReadResponse",
                (),
                {
                    "status_code": 200,
                    "text": "{}",
                    "json": lambda self: {"data": []},
                },
            )()

        async def post(self, url, **kwargs):
            return _Response()

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _Client)

    with pytest.raises(RuntimeError) as excinfo:
        await integrations.subscribe_page_webhooks("PAGE1", "secret-token", "messages")

    message = str(excinfo.value)
    assert "secret-token" not in message, "raised errors get logged verbatim"
    assert "(#100) Invalid field" in message, "the actionable reason must survive"


# ── A failed send must never be reported as a delivered reply ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "recipient"),
    [("facebook_messenger", "PSID1"), ("instagram_dm", "IGSID1"), ("whatsapp", "+919876543210")],
)
async def test_send_response_raises_on_graph_error(
    monkeypatch, channel: str, recipient: str
) -> None:
    """This is what stops a turn being logged as responded when nothing sent.

    session.py converts SendFailedError into send_status="send_failed" and a
    retry-queue entry; if send_response swallowed the error dict instead, every
    delivery failure would look like a success.
    """
    from agent import pipeline
    from mcp_servers.meta_mcp.utils.errors import tool_error

    async def call_mcp_tool(server, name, args):
        return tool_error("MESSAGE_FAILED", "(#100) Messaging type should be MESSAGE_TAG")

    monkeypatch.setattr(pipeline, "call_mcp_tool", call_mcp_tool)

    with pytest.raises(pipeline.SendFailedError) as excinfo:
        await pipeline.send_response(
            channel=channel,
            recipient_id=recipient,
            text="Hello",
            business_id="biz-1",
        )

    assert "MESSAGE_TAG" in str(excinfo.value), "the Meta reason must reach the log"


@pytest.mark.asyncio
async def test_send_response_returns_result_on_success(monkeypatch) -> None:
    from agent import pipeline

    async def call_mcp_tool(server, name, args):
        return {"message_id": "m_1", "status": "sent"}

    monkeypatch.setattr(pipeline, "call_mcp_tool", call_mcp_tool)

    result = await pipeline.send_response(
        channel="facebook_messenger",
        recipient_id="PSID1",
        text="Hello",
        business_id="biz-1",
    )

    assert result["status"] == "sent"


def test_failed_send_tool_result_does_not_suppress_pipeline_fallback() -> None:
    """A failed LLM-initiated send must not be mistaken for a delivered reply."""
    from agent.pipeline import llm_already_sent_outbound

    failed = [
        {
            "tool_name": "send_facebook_message",
            "error": {"code": "MESSAGE_FAILED", "message": "(#100)"},
        }
    ]
    assert llm_already_sent_outbound(failed) is False

    succeeded = [{"tool_name": "send_facebook_message", "status": "sent", "message_id": "m_1"}]
    assert llm_already_sent_outbound(succeeded) is True
