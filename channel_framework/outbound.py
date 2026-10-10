"""Confidence-gated outbound delivery for non-Meta channels."""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from channel_framework.adapters.base import NormalizedMessage
from channel_framework.confidence import decide_confidence
from channel_framework.senders.email import send_email
from channel_framework.senders.slack import send_slack_message
from channel_framework.storage import insert_outbound

OwnerNotifier = Callable[[NormalizedMessage, str], Awaitable[None]]


async def deliver_response(
    message: NormalizedMessage,
    response_text: str,
    *,
    confidence_score: float,
    channel_config: dict[str, Any] | None = None,
    notify_owner: OwnerNotifier | None = None,
) -> dict[str, Any]:
    decision = decide_confidence(confidence_score, channel_config)
    result: dict[str, Any] = {"status": "queued"}
    if decision.should_send:
        if message.channel in {"gmail", "outlook"}:
            subject = message.subject or "Your message"
            if not subject.casefold().startswith("re:"):
                subject = f"Re: {subject}"
            result = await send_email(
                business_id=message.business_id,
                platform=message.channel,
                to=message.sender_id,
                subject=subject,
                body=response_text,
                thread_id=message.thread_id,
            )
        elif message.channel == "slack":
            result = await send_slack_message(
                business_id=message.business_id,
                channel_id=str(message.metadata.get("channel_id") or ""),
                text=response_text,
                thread_ts=message.thread_id,
            )
        else:
            raise ValueError(f"Unsupported channel: {message.channel}")
    platform_id = str(
        result.get("message_id") or f"{decision.action}:{uuid.uuid4()}"
    )
    await insert_outbound(
        message=message,
        content=response_text,
        confidence_score=confidence_score,
        send_action=decision.action,
        platform_message_id=platform_id,
    )
    if decision.notify_owner and notify_owner:
        await notify_owner(message, response_text)
    return {
        **result,
        "action": decision.action,
        "sent": decision.should_send,
    }
