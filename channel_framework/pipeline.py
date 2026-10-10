"""Universal non-Meta inbound pipeline."""

from __future__ import annotations

from typing import Any

from channel_framework.adapters.base import NormalizedMessage
from channel_framework.credentials import load_tokens
from channel_framework.outbound import deliver_response
from channel_framework.storage import finish_inbound, insert_inbound


async def process_normalized_message(
    message: NormalizedMessage,
    *,
    confidence_score: float | None = None,
) -> dict[str, Any]:
    if not await insert_inbound(message):
        return {"status": "duplicate", "platform_message_id": message.platform_message_id}

    try:
        from agent.session import handle_incoming_message

        result = await handle_incoming_message(
            channel=message.channel,
            external_sender_id=message.sender_id,
            message_text=message.content,
            receiving_account_id=message.business_id,
            message_type="text",
            external_message_id=message.platform_message_id,
            sender_display_name=message.sender_display,
            skip_send=True,
            runtime_context={
                "subject": message.subject,
                "thread_id": message.thread_id,
                "channel_metadata": message.metadata,
            },
        )
        response = str(result.get("response_text") or "").strip()
        if not response:
            await finish_inbound(message)
            return result
        ai_metadata = result.get("ai_metadata") or {}
        confidence_meta = ai_metadata.get("confidence") or {}
        score = (
            float(confidence_score)
            if confidence_score is not None
            else float(confidence_meta.get("score") or 0.5)
        )
        credentials = await load_tokens(message.business_id, message.channel)
        config = (credentials or {}).get("channel_config") or {}

        async def notify_owner(
            inbound: NormalizedMessage,
            draft: str,
        ) -> None:
            from onboarding.integrations import notify_owner_text

            await notify_owner_text(
                inbound.business_id,
                f"Channel review ({inbound.channel}) from "
                f"{inbound.sender_display or inbound.sender_id}: {draft[:500]}",
            )

        delivery = await deliver_response(
            message,
            response,
            confidence_score=score,
            channel_config=config,
            notify_owner=notify_owner,
        )
        await finish_inbound(message)
        return {**result, "channel_delivery": delivery}
    except Exception as exc:
        await finish_inbound(message, error=str(exc) or type(exc).__name__)
        raise
