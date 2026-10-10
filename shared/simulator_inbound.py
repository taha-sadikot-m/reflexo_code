"""Build inbound Redis events for the admin/API simulator.

Shapes match Meta webhook parsers so routing (social_agent, comment tools)
sees the same fields as production. Outbound is always skip_send.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

SimulatorMode = Literal["customer", "owner"]

MESSAGE_CHANNELS = frozenset({"whatsapp", "web"})
DM_CHANNELS = {
    "facebook_messenger": "facebook_message",
    "instagram_dm": "instagram_dm",
}
COMMENT_CHANNELS = frozenset({"facebook_comment", "instagram_comment"})
ALLOWED_CHANNELS = MESSAGE_CHANNELS | frozenset(DM_CHANNELS) | COMMENT_CHANNELS

# Legacy UI value — treat as Instagram DM.
CHANNEL_ALIASES = {"instagram": "instagram_dm"}


def normalize_simulator_channel(channel: str) -> str:
    raw = (channel or "whatsapp").strip() or "whatsapp"
    mapped = CHANNEL_ALIASES.get(raw, raw)
    if mapped not in ALLOWED_CHANNELS:
        raise ValueError(f"Unsupported simulator channel: {channel}")
    return mapped


def build_simulator_inbound_event(
    *,
    business_id: str,
    text: str,
    channel: str,
    mode: SimulatorMode = "customer",
    session_id: str | None = None,
    customer_name: str = "Test Customer",
    sender_id: str | None = None,
    comment_id: str | None = None,
    post_id: str | None = None,
) -> dict[str, Any]:
    """Queue-ready inbound event. skip_send is always True."""
    channel = normalize_simulator_channel(channel)
    session_id = session_id or f"sim-{uuid.uuid4().hex[:12]}"
    sender_id = sender_id or f"sim-{mode}-{session_id}"
    message_id = f"sim-msg-{uuid.uuid4().hex[:16]}"
    name = (customer_name or "Test Customer").strip() or "Test Customer"

    event: dict[str, Any] = {
        "business_id": business_id,
        "sender_id": sender_id,
        "message_type": "text",
        "skip_send": True,
        "session_id": session_id,
        "receiving_account_id": business_id,
        "simulator": {
            "business_id": business_id,
            "mode": mode,
            "customer_name": name,
        },
        "content": {"text": text, "type": "text"},
        "channel": channel,
    }

    if channel in COMMENT_CHANNELS:
        stamp = uuid.uuid4().hex[:12]
        cid = (comment_id or "").strip() or f"sim-comment-{stamp}"
        pid = (post_id or "").strip() or f"sim-post-{stamp}"
        event["event_type"] = channel
        event["comment_id"] = cid
        event["post_id"] = pid
        event["sender_name"] = name
        event["message_id"] = cid
        if channel == "facebook_comment":
            event["page_id"] = business_id
        else:
            event["ig_account_id"] = business_id
        return event

    if channel in DM_CHANNELS:
        event["event_type"] = DM_CHANNELS[channel]
        event["message_id"] = message_id
        if channel == "facebook_messenger":
            event["page_id"] = business_id
        else:
            event["ig_account_id"] = business_id
        return event

    event["event_type"] = "message"
    event["message_id"] = message_id
    return event
