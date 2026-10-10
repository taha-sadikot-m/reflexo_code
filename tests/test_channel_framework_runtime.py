"""Stage T11: Channel Framework runtime."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channel_framework.adapters.base import NormalizedMessage

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _gmail_fixture() -> dict:
    body = base64.urlsafe_b64encode(b"Can you help with my order?").decode()
    return {
        "gmail_message": {
            "id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Jane <JANE@EXAMPLE.COM>"},
                    {"name": "Subject", "value": "Order help"},
                ],
                "body": {"data": body},
            },
        }
    }


@pytest.mark.asyncio
async def test_gmail_fixture_reaches_canonical_temporal_agent_pipeline() -> None:
    from channel_framework.adapters.gmail import GmailAdapter
    from channel_framework.pipeline import process_normalized_message

    message = await GmailAdapter().parse_webhook(_gmail_fixture(), BUSINESS)
    assert message is not None
    assert message.sender_id == "jane@example.com"
    handler = AsyncMock(
        return_value={
            "status": "workflow_consumed",
            "workflow_matched": True,
            "response_text": None,
        }
    )
    with (
        patch("channel_framework.pipeline.insert_inbound", AsyncMock(return_value=True)),
        patch("channel_framework.pipeline.finish_inbound", AsyncMock()),
        patch("agent.session.handle_incoming_message", handler),
        patch("channel_framework.pipeline.deliver_response", AsyncMock()) as delivery,
    ):
        result = await process_normalized_message(message)
    assert result["workflow_matched"] is True
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["channel"] == "gmail"
    assert handler.await_args.kwargs["skip_send"] is True
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_pipeline_reads_nested_agent_confidence_score() -> None:
    from channel_framework.pipeline import process_normalized_message

    handler = AsyncMock(
        return_value={
            "status": "success",
            "response_text": "Answer",
            "ai_metadata": {"confidence": {"score": 0.92}},
        }
    )
    delivery = AsyncMock(return_value={"sent": True, "action": "auto_send"})
    with (
        patch("channel_framework.pipeline.insert_inbound", AsyncMock(return_value=True)),
        patch("channel_framework.pipeline.finish_inbound", AsyncMock()),
        patch("agent.session.handle_incoming_message", handler),
        patch(
            "channel_framework.pipeline.load_tokens",
            AsyncMock(return_value={"channel_config": {}}),
        ),
        patch("channel_framework.pipeline.deliver_response", delivery),
    ):
        await process_normalized_message(_message())
    assert delivery.await_args.kwargs["confidence_score"] == 0.92


def _message(channel: str = "gmail") -> NormalizedMessage:
    return NormalizedMessage(
        channel=channel,
        business_id=BUSINESS,
        sender_id="jane@example.com" if channel != "slack" else "U123",
        sender_display="Jane",
        content="Question",
        subject="Help",
        thread_id="thread-1",
        platform_message_id="in-1",
        metadata={"channel_id": "C123"},
    )


@pytest.mark.asyncio
async def test_low_confidence_queues_without_send() -> None:
    from channel_framework.outbound import deliver_response

    with (
        patch("channel_framework.outbound.send_email", AsyncMock()) as send,
        patch("channel_framework.outbound.insert_outbound", AsyncMock()) as store,
    ):
        result = await deliver_response(
            _message(),
            "Draft",
            confidence_score=0.59,
        )
    assert result["action"] == "queued"
    assert result["sent"] is False
    send.assert_not_awaited()
    assert store.await_args.kwargs["send_action"] == "queued"


@pytest.mark.asyncio
async def test_high_confidence_sends() -> None:
    from channel_framework.outbound import deliver_response

    with (
        patch(
            "channel_framework.outbound.send_email",
            AsyncMock(return_value={"status": "sent", "message_id": "out-1"}),
        ) as send,
        patch("channel_framework.outbound.insert_outbound", AsyncMock()),
    ):
        result = await deliver_response(
            _message(),
            "Answer",
            confidence_score=0.91,
        )
    assert result["action"] == "auto_send"
    assert result["sent"] is True
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_medium_confidence_sends_and_notifies_owner() -> None:
    from channel_framework.outbound import deliver_response

    notify = AsyncMock()
    with (
        patch(
            "channel_framework.outbound.send_email",
            AsyncMock(return_value={"status": "sent", "message_id": "out-2"}),
        ),
        patch("channel_framework.outbound.insert_outbound", AsyncMock()),
    ):
        result = await deliver_response(
            _message(),
            "Please review",
            confidence_score=0.70,
            notify_owner=notify,
        )
    assert result["action"] == "send_and_notify"
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_slack_adapter_ignores_bots_and_normalizes_customer_message() -> None:
    from channel_framework.adapters.slack import SlackAdapter

    adapter = SlackAdapter()
    assert await adapter.parse_webhook(
        {"event": {"type": "message", "bot_id": "B1", "text": "loop"}},
        BUSINESS,
    ) is None
    with patch(
        "channel_framework.adapters.slack._is_connected_team",
        AsyncMock(return_value=True),
    ):
        message = await adapter.parse_webhook(
            {
                "event_id": "Ev1",
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "user": "U1",
                    "channel": "C1",
                    "text": "Need help",
                    "ts": "123.4",
                },
            },
            BUSINESS,
        )
    assert message is not None
    assert message.sender_id == "U1"
    assert message.metadata["channel_id"] == "C1"


@pytest.mark.asyncio
async def test_direct_razorpay_uses_basic_auth() -> None:
    from channel_framework.integrations.razorpay import create_payment_link

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"id": "plink_1"}
    client = MagicMock()
    client.request = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    with (
        patch(
            "channel_framework.integrations.razorpay.load_tokens",
            AsyncMock(
                return_value={
                    "access_token": "key_id",
                    "refresh_token": "key_secret",
                }
            ),
        ),
        patch(
            "channel_framework.integrations.razorpay.httpx.AsyncClient",
            return_value=context,
        ) as factory,
    ):
        result = await create_payment_link(
            BUSINESS,
            amount_paise=50000,
            description="Order",
        )
    assert result["id"] == "plink_1"
    assert factory.call_args.kwargs["auth"] == ("key_id", "key_secret")


@pytest.mark.asyncio
async def test_direct_fast2sms_uses_authorization_header() -> None:
    from channel_framework.integrations.fast2sms import send_sms

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"return": True, "request_id": "sms-1"}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    with (
        patch(
            "channel_framework.integrations.fast2sms.load_tokens",
            AsyncMock(return_value={"access_token": "api-key"}),
        ),
        patch(
            "channel_framework.integrations.fast2sms.httpx.AsyncClient",
            return_value=context,
        ),
    ):
        await send_sms(BUSINESS, phone="+919000000001", message="OTP")
    assert client.post.await_args.kwargs["headers"]["authorization"] == "api-key"


@pytest.mark.asyncio
async def test_channel_mcp_exposes_required_tools_and_health() -> None:
    from channel_framework.mcp_server import health, mcp

    tools = {tool.name for tool in await mcp.list_tools()}
    assert {
        "send_email",
        "read_emails",
        "generate_connection_link",
        "get_channel_status",
    }.issubset(tools)
    response = await health(MagicMock())
    assert response.status_code == 200
    assert b"channel-framework" in response.body
