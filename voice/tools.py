"""Voice tools that wait on WhatsApp input during an active call."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from shared.redis_client import get_redis
from voice.cross_channel import (
    CROSS_CHANNEL_CONTENT_TIMEOUT,
    actor_identity,
    cancel_cross_channel,
    check_cross_channel_listener,
    pop_pending_cross_channel_input,
    pubsub_channel,
    register_cross_channel_listener,
)
from voice.userdata import UserData

logger = logging.getLogger(__name__)


def _default_whatsapp_timeout() -> int:
    try:
        from voice.config import get_voice_settings

        return max(1, int(get_voice_settings().voice_whatsapp_input_timeout))
    except Exception:
        return CROSS_CHANNEL_CONTENT_TIMEOUT


async def request_whatsapp_input(
    userdata: UserData,
    prompt_message: str,
    expected_content: str = "any",
    timeout_seconds: int | None = None,
    livekit_room: str = "",
) -> dict[str, Any]:
    """Register a listener, send a WhatsApp prompt, and wait for content."""
    wait_seconds = (
        max(1, int(timeout_seconds))
        if timeout_seconds is not None
        else _default_whatsapp_timeout()
    )
    customer_id = userdata.customer_id
    if not userdata.caller_phone:
        return {"status": "error", "content": None, "error": "missing_phone"}
    identity = actor_identity(
        business_id=userdata.business_id,
        is_owner=userdata.is_owner,
        customer_id=customer_id,
        sender_external_id=userdata.caller_phone,
    )
    if not identity:
        return {"status": "error", "content": None, "error": "missing_actor_identity"}
    actor_type, actor_key = identity

    pubsub = None
    channel = pubsub_channel(userdata.call_id)
    try:
        # Subscribe before sending the prompt so an immediate WhatsApp reply
        # cannot be published between registration and subscription.
        redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)

        pending = await pop_pending_cross_channel_input(
            business_id=userdata.business_id,
            is_owner=userdata.is_owner,
            customer_id=customer_id,
            sender_external_id=userdata.caller_phone,
            call_id=userdata.call_id,
            redis=redis,
        )
        if pending is not None:
            return {"status": "received", "content": pending}

        existing = await check_cross_channel_listener(
            business_id=userdata.business_id,
            is_owner=userdata.is_owner,
            customer_id=customer_id,
            sender_external_id=userdata.caller_phone,
        )
        if not existing:
            await register_cross_channel_listener(
                customer_id=customer_id,
                customer_phone=userdata.caller_phone,
                voice_session_id=userdata.call_id,
                livekit_room=livekit_room or f"wa-call-{userdata.call_id}",
                business_id=userdata.business_id,
                expected_content=expected_content or "any",
                prompt_message=prompt_message,
                is_owner=userdata.is_owner,
            )

        async def _wait() -> dict[str, Any]:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                data = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "cross_channel_content":
                    return {
                        "status": "received",
                        "content": data.get("content") or {},
                    }
            return {"status": "timeout", "content": None}

        try:
            return await asyncio.wait_for(_wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            await cancel_cross_channel(
                actor_key,
                business_id=userdata.business_id,
                status="expired",
            )
            logger.info(
                "CROSS_CHANNEL tool timeout actor_type=%s call_id=%s",
                actor_type,
                userdata.call_id,
            )
            return {"status": "timeout", "content": None}
    except Exception as exc:
        logger.exception("request_whatsapp_input failed call_id=%s", userdata.call_id)
        return {"status": "error", "content": None, "error": str(exc)[:300]}
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass


PROACTIVE_CLIP_CHARS = 1200


def _content_label(content: dict[str, Any]) -> tuple[str, str]:
    kind = str(content.get("type") or "file")
    filename = str((content.get("metadata") or {}).get("filename") or "").strip()
    if kind == "image" and not filename:
        kind = "photo"
    return kind, filename


def format_proactive_whatsapp_content(content: dict[str, Any]) -> str:
    """Describe content that arrived mid-call so the model can name it and ask about it."""
    kind, filename = _content_label(content)
    text = str(content.get("text") or "").strip()
    named = f'"{filename}"' if filename else f"a {kind}"
    if not text:
        error = (content.get("metadata") or {}).get("error")
        if error:
            return (
                f"The caller just sent {named} on WhatsApp during this call, "
                f"but it could not be read ({error})."
            )
        return (
            f"The caller just sent {named} on WhatsApp during this call, "
            "but it had no readable text."
        )
    facts = [f"kind: {kind}"]
    if filename:
        facts.append(f"filename: {filename}")
    tables = content.get("tables") or []
    if tables:
        facts.append(f"tables: {len(tables)}")
    facts.append(f"length: {len(text)} characters")
    clipped = text[:PROACTIVE_CLIP_CHARS]
    if len(text) > PROACTIVE_CLIP_CHARS:
        clipped += " ..."
    return (
        f"The caller just sent {named} on WhatsApp during this call "
        f"({'; '.join(facts)}).\nContent:\n{clipped}"
    )


def spoken_whatsapp_fallback(content: dict[str, Any]) -> str:
    """Spoken acknowledgement used when the model reply produced no audio."""
    kind, filename = _content_label(content)
    text = str(content.get("text") or "").strip()
    subject = filename or f"the {kind}"
    if not text:
        return (
            f"I received {subject} on WhatsApp, but I could not read anything from it. "
            "Would you like to tell me what it says instead?"
        )
    headline = ""
    for line in text.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if len(candidate) >= 3:
            headline = candidate[:120]
            break
    if headline:
        return (
            f"I have {subject} here, and it starts with {headline}. "
            "What would you like me to do with it?"
        )
    return f"I have {subject} here. What would you like me to do with it?"


def format_whatsapp_input_result(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "timeout":
        return (
            "The caller did not send anything on WhatsApp in time. "
            "Continue the call and offer to try again."
        )
    if status == "error":
        return "I could not wait for WhatsApp input. Continue the call without that file."
    content = result.get("content") or {}
    text = str(content.get("text") or "").strip()
    kind = content.get("type") or "content"
    if not text:
        error = (content.get("metadata") or {}).get("error")
        if error:
            return f"WhatsApp {kind} arrived but could not be read ({error}). Ask them to resend."
        return f"The caller sent a {kind} on WhatsApp but there was no readable text."
    clipped = text[:1500]
    return f"The caller sent this {kind} on WhatsApp: {clipped}"
