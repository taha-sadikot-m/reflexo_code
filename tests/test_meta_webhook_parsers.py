"""Unit tests for Meta webhook content extraction and payload routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers.meta_mcp.webhooks.handler import process_webhook_payload
from mcp_servers.meta_mcp.webhooks.whatsapp import extract_whatsapp_content


def test_extract_whatsapp_text():
    content = extract_whatsapp_content(
        {"type": "text", "text": {"body": "Hello"}, "context": {"id": "wamid.1"}}
    )
    assert content["text"] == "Hello"
    assert content["context"] == {"id": "wamid.1"}


def test_extract_whatsapp_image():
    content = extract_whatsapp_content(
        {
            "type": "image",
            "image": {"id": "media_1", "mime_type": "image/jpeg", "caption": "Pic"},
        }
    )
    assert content["media_id"] == "media_1"
    assert content["text"] == "Pic"
    assert content["media_type"] == "image/jpeg"


def test_extract_whatsapp_interactive_button():
    content = extract_whatsapp_content(
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "btn_yes", "title": "Yes"},
            },
        }
    )
    assert content["text"] == "Yes"
    assert content["interactive"]["reply"]["id"] == "btn_yes"


@pytest.mark.asyncio
async def test_process_whatsapp_message_publishes():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID_1"},
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.abc",
                                    "timestamp": "1721300000",
                                    "type": "text",
                                    "text": {"body": "Hi"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    with (
        patch(
            "mcp_servers.meta_mcp.webhooks.whatsapp.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch(
            "mcp_servers.meta_mcp.webhooks.whatsapp.publish_event",
            new_callable=AsyncMock,
        ) as publish,
    ):
        await process_webhook_payload(payload)
        publish.assert_awaited()
        channel, event = publish.await_args.args
        assert channel == "incoming:biz-uuid"
        assert event["event_type"] == "whatsapp_message"
        assert event["sender_id"] == "+919876543210"
        assert event["content"]["text"] == "Hi"


@pytest.mark.asyncio
async def test_process_facebook_comment_filters_non_comments():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE_1",
                "changes": [
                    {
                        "field": "feed",
                        "value": {"item": "post", "verb": "add", "message": "ignored"},
                    }
                ],
            }
        ],
    }

    with patch(
        "mcp_servers.meta_mcp.webhooks.facebook.publish_event",
        new_callable=AsyncMock,
    ) as publish:
        await process_webhook_payload(payload)
        publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_instagram_entry_messaging():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "IG_1",
                "messaging": [
                    {
                        "sender": {"id": "USER_1"},
                        "recipient": {"id": "IG_1"},
                        "timestamp": 1721300000,
                        "message": {"mid": "m_1", "text": "Hello IG"},
                    }
                ],
            }
        ],
    }

    with (
        patch(
            "mcp_servers.meta_mcp.webhooks.instagram.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-ig",
        ),
        patch(
            "mcp_servers.meta_mcp.webhooks.instagram.publish_event",
            new_callable=AsyncMock,
        ) as publish,
    ):
        await process_webhook_payload(payload)
        publish.assert_awaited()
        _, event = publish.await_args.args
        assert event["event_type"] == "instagram_dm"
        assert event["content"]["text"] == "Hello IG"
