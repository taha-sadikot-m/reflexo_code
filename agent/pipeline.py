"""Message processing pipeline utilities."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from identity.bot_detection import BotDetector
from identity.models import IdentityResult
from identity.resolver import resolve_identity as resolve_identity_core
from mcp_servers.reflexo_core.tools.config import get_business_config_impl
from shared.db import tenant_connection
from shared.redis_client import get_redis
from shared.redis_keys import business_config_key, usage_key

from agent.error_handling import LIMIT_REACHED_MESSAGE, notify_owner_of_limit
from agent.memory import load_cross_channel_context, load_recent_messages, store_customer_memory
from agent.tools import call_mcp_tool, is_mcp_error_result, mcp_error_message
from agent.config import AGENT_CONFIG
from agent.escalation import (
    flag_conversation_for_daily_summary,
    should_flag_for_daily_summary,
)

logger = logging.getLogger(__name__)


class SendFailedError(RuntimeError):
    """Outbound channel delivery failed (e.g. Meta Graph 403)."""

    def __init__(self, message: str, *, result: Any = None):
        super().__init__(message)
        self.result = result



async def resolve_identity(
    channel: str,
    external_sender_id: str,
    receiving_account_id: str,
    sender_display_name: str | None = None,
) -> IdentityResult:
    """Delegate to the shared identity package."""
    return await resolve_identity_core(
        channel=channel,
        sender_external_id=external_sender_id,
        receiving_account_id=receiving_account_id,
        sender_display_name=sender_display_name,
    )


async def load_business_config(business_id: str) -> dict[str, Any]:
    """Load business config (Redis-cached via Core implementation)."""
    config = await get_business_config_impl(business_id)
    if config is None:
        raise ValueError(f"Business not found: {business_id}")
    return config


async def check_message_limit(business_id: str) -> bool:
    """Return True if the business is still within its message quota."""
    config = await load_business_config(business_id)
    limit = int(config.get("message_limit") or 100)
    used = int(config.get("messages_used") or 0)

    # Prefer Redis counter when present (hot path); fall back to DB field
    billing_start = str(config.get("billing_cycle_start") or "")[:10]
    if not billing_start:
        billing_start = date.today().replace(day=1).isoformat()
    try:
        redis = get_redis()
        raw = await redis.get(usage_key(business_id, billing_start))
        if raw is not None:
            used = max(used, int(raw))
    except Exception as exc:
        logger.debug("usage redis read failed: %s", exc)

    return used < limit


async def check_bot_detection(
    business_id: str,
    external_sender_id: str,
    channel: str,
    *,
    already_suspected: bool = False,
) -> bool:
    """
    Return True if the message should be dropped as bot traffic.

    Identity resolution already runs BotDetector; this re-checks velocity/depth
    for defense in depth when identity did not flag.
    """
    if already_suspected:
        return True
    detector = BotDetector()
    from identity.models import Channel

    try:
        channel_enum = Channel(channel)
    except ValueError:
        return False
    suspected, _layers = await detector.check(business_id, external_sender_id, channel_enum)
    return suspected


async def get_or_create_conversation(
    business_id: str,
    customer_id: str | None,
    channel: str,
    external_thread_id: str | None = None,
) -> dict[str, Any]:
    """Create or reuse an active conversation; attach recent messages."""
    # Map voice channel for DB enum
    db_channel = "voice" if channel in ("whatsapp_voice", "voice") else channel

    async with tenant_connection(business_id) as conn:
        existing = None
        if external_thread_id:
            existing = await conn.fetchrow(
                """
                SELECT id FROM conversations
                WHERE business_id = $1::uuid AND channel = $2::channel_type
                  AND external_thread_id = $3 AND is_active = true
                LIMIT 1
                """,
                business_id,
                db_channel,
                external_thread_id,
            )
        if existing is None and customer_id:
            existing = await conn.fetchrow(
                """
                SELECT id FROM conversations
                WHERE business_id = $1::uuid AND customer_id = $2::uuid
                  AND channel = $3::channel_type AND is_active = true
                ORDER BY last_message_at DESC NULLS LAST
                LIMIT 1
                """,
                business_id,
                customer_id,
                db_channel,
            )
        if existing is None and customer_id is None:
            # Owner session — one active owner thread per channel
            existing = await conn.fetchrow(
                """
                SELECT id FROM conversations
                WHERE business_id = $1::uuid
                  AND customer_id IS NULL
                  AND channel = $2::channel_type
                  AND is_active = true
                ORDER BY last_message_at DESC NULLS LAST
                LIMIT 1
                """,
                business_id,
                db_channel,
            )

        if existing:
            conversation_id = str(existing["id"])
            created = False
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations (business_id, customer_id, channel, external_thread_id)
                VALUES ($1::uuid, $2::uuid, $3::channel_type, $4)
                RETURNING id
                """,
                business_id,
                customer_id,
                db_channel,
                external_thread_id,
            )
            conversation_id = str(row["id"])
            created = True

    recent = await load_recent_messages(business_id, conversation_id)
    return {
        "conversation_id": conversation_id,
        "created": created,
        "recent_messages": recent,
    }


async def load_customer_profile(business_id: str, customer_id: str) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, phone, email, primary_channel, preferences, tags,
                   total_orders, total_spent, last_seen_at, contact_type
            FROM customers
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            customer_id,
            business_id,
        )
    if not row:
        return None

    prefs = row["preferences"]
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except json.JSONDecodeError:
            prefs = {}

    return {
        "id": str(row["id"]),
        "name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "primary_channel": row["primary_channel"],
        "preferences": prefs or {},
        "tags": list(row["tags"] or []),
        "total_orders": row["total_orders"],
        "total_spent": float(row["total_spent"] or 0),
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "contact_type": row.get("contact_type") or "unknown",
    }


async def send_response(
    channel: str,
    recipient_id: str,
    text: str,
    business_id: str,
    *,
    comment_id: str | None = None,
) -> Any:
    """Deliver the final agent text via Meta MCP tools.

    Raises SendFailedError when Meta/Core returns a structured error dict
    (e.g. Graph HTTP 403) so callers do not claim success silently.
    """
    if not text or not str(text).strip():
        logger.warning("send_response skipped — empty text business=%s", business_id)
        return None

    text = enforce_response_length(text, channel)
    result: Any = None

    if channel == "whatsapp":
        result = await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {"business_id": business_id, "to": recipient_id, "text": text},
        )
    elif channel in ("whatsapp_voice", "voice"):
        try:
            from voice.config import get_voice_settings

            voice_ready = get_voice_settings().is_configured()
        except Exception:
            voice_ready = False
        if voice_ready:
            logger.info(
                "Voice reply is spoken by LiveKit TTS business=%s chars=%d",
                business_id,
                len(text),
            )
            return {"status": "voice_tts", "text": text, "delivery": "livekit"}
        logger.info(
            "Voice TTS unconfigured — WhatsApp text fallback business=%s chars=%d",
            business_id,
            len(text),
        )
        result = await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {"business_id": business_id, "to": recipient_id, "text": text},
        )
        if is_mcp_error_result(result):
            msg = mcp_error_message(result)
            logger.error("Voice→WhatsApp send failed: %s", msg)
            raise SendFailedError(msg, result=result)
        return {"status": "voice_text_ready", "text": text, "delivery": "whatsapp_text_fallback"}
    elif channel == "instagram_dm":
        result = await call_mcp_tool(
            "meta",
            "send_instagram_dm",
            {"business_id": business_id, "to_ig_user_id": recipient_id, "text": text},
        )
    elif channel == "instagram_comment" and comment_id:
        result = await call_mcp_tool(
            "meta",
            "reply_instagram_comment",
            {"business_id": business_id, "comment_id": comment_id, "text": text},
        )
    elif channel == "facebook_messenger":
        result = await call_mcp_tool(
            "meta",
            "send_facebook_message",
            {"business_id": business_id, "to_psid": recipient_id, "text": text},
        )
    elif channel == "facebook_comment" and comment_id:
        result = await call_mcp_tool(
            "meta",
            "reply_facebook_comment",
            {"business_id": business_id, "comment_id": comment_id, "text": text},
        )
    else:
        logger.warning("No send mapping for channel=%s — response not delivered", channel)
        return None

    if is_mcp_error_result(result):
        operation = {
            "whatsapp": "send_whatsapp_message",
            "instagram_dm": "send_instagram_dm",
            "instagram_comment": "reply_instagram_comment",
            "facebook_messenger": "send_facebook_message",
            "facebook_comment": "reply_facebook_comment",
        }.get(channel, "send_response")
        error = result.get("error") if isinstance(result, dict) else {}
        error = error if isinstance(error, dict) else {}
        graph_code = error.get("graph_code")
        retryable = bool(error.get("retryable"))
        if graph_code == 3 and channel in {
            "instagram_dm",
            "instagram_comment",
            "facebook_messenger",
            "facebook_comment",
        }:
            try:
                from integrations.capabilities import inspect_business_capabilities

                capabilities = await inspect_business_capabilities(business_id)
                feature = next(
                    (
                        feature
                        for account in capabilities.get("items", [])
                        for feature in account.get("features", [])
                        if feature.get("id") == channel
                    ),
                    None,
                )
                if feature and feature.get("unapproved_app_permissions"):
                    error["code"] = "APP_PERMISSION_UNAPPROVED"
                    error["unapproved_app_permissions"] = feature[
                        "unapproved_app_permissions"
                    ]
            except Exception as exc:
                logger.warning(
                    "Meta capability confirmation failed channel=%s operation=%s: %s",
                    channel,
                    operation,
                    type(exc).__name__,
                )
        msg = mcp_error_message(result)
        logger.error(
            "Meta operation failed channel=%s operation=%s graph_code=%s "
            "retryable=%s business=%s error_code=%s message=%s",
            channel,
            operation,
            graph_code,
            retryable,
            business_id,
            error.get("code"),
            msg,
        )
        raise SendFailedError(msg, result=result)
    return result


def llm_already_sent_outbound(tool_results: list[dict[str, Any]]) -> bool:
    """True if this turn's tool results show a successful channel send/reply.

    Name-only checks are unsafe: checkpoint history can contain older send_*
    calls, and a failed send_* must not suppress the pipeline fallback.
    """
    send_tools = {
        "send_whatsapp_message",
        "send_whatsapp_interactive",
        "send_whatsapp_media",
        "send_instagram_dm",
        "reply_instagram_comment",
        "send_facebook_message",
        "reply_facebook_comment",
    }
    for entry in tool_results:
        name = entry.get("tool_name")
        if name not in send_tools:
            continue
        if entry.get("error") or entry.get("error_type"):
            continue
        if entry.get("status") == "sent" or entry.get("message_id"):
            return True
    return False


async def claim_inbound_message(channel: str, message_id: str | None) -> bool:
    """
    Idempotency gate for inbound webhooks.

    Returns True if this message_id is new and should be processed.
    Missing message_id always processes (best-effort).
    """
    if not message_id:
        return True
    from shared.redis_client import get_redis
    from shared.redis_keys import processed_message_key

    key = processed_message_key(channel, str(message_id))
    try:
        redis = get_redis()
        claimed = await redis.set(key, "1", nx=True, ex=86400 * 2)
        return bool(claimed)
    except Exception as exc:
        logger.warning("Inbound dedup check failed (processing anyway): %s", exc)
        return True


async def release_inbound_message(channel: str, message_id: str | None) -> None:
    """Drop a claim so Meta retries can reprocess after a failed/dropped turn."""
    if not message_id:
        return
    from shared.redis_client import get_redis
    from shared.redis_keys import processed_message_key

    key = processed_message_key(channel, str(message_id))
    try:
        redis = get_redis()
        await redis.delete(key)
    except Exception as exc:
        logger.warning("Inbound claim release failed for %s: %s", message_id, exc)


async def message_already_answered(channel: str, message_id: str | None) -> bool:
    """True when a customer-visible reply already went out for this message.

    Guards the retry path: a failed turn releases its inbound claim so it can be
    reprocessed, and without this a retry would send a second reply.
    """
    if not message_id:
        return False
    from shared.redis_client import get_redis
    from shared.redis_keys import answered_message_key

    try:
        redis = get_redis()
        return bool(await redis.exists(answered_message_key(channel, str(message_id))))
    except Exception as exc:
        logger.warning("Answered check failed for %s (sending anyway): %s", message_id, exc)
        return False


async def mark_message_answered(channel: str, message_id: str | None) -> None:
    """Record that this inbound message has had its one customer reply."""
    if not message_id:
        return
    from shared.redis_client import get_redis
    from shared.redis_keys import answered_message_key

    try:
        redis = get_redis()
        await redis.set(answered_message_key(channel, str(message_id)), "1", ex=86400 * 2)
    except Exception as exc:
        logger.warning("Answered marker write failed for %s: %s", message_id, exc)


def _strip_dangling_list_markers(text: str) -> str:
    """Remove trailing incomplete numbered-list markers like bare '1.' or '2. '."""
    import re

    cleaned = text.rstrip()
    # Drop trailing lines that are only "N." / "N. "
    while True:
        next_cleaned = re.sub(r"(?:\r?\n)+\s*\d+\.\s*$", "", cleaned)
        next_cleaned = re.sub(r"^\d+\.\s*$", "", next_cleaned).rstrip()
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned
    # Also strip a dangling "N." glued to the end of the last sentence
    cleaned = re.sub(r"(?<!\n)\s*\d+\.\s*$", "", cleaned).rstrip()
    return cleaned


def enforce_response_length(text: str, channel: str) -> str:
    """Soft-enforce voice/text sentence budgets from AGENT_CONFIG.

    Numbered-list markers (``1. ``, ``2. ``) must not count as sentence ends —
    otherwise WhatsApp replies truncate to a bare ``1.``.
    """
    import re

    is_voice = channel in ("whatsapp_voice", "voice")
    max_sentences = int(
        AGENT_CONFIG["voice_max_sentences"] if is_voice else AGENT_CONFIG["text_max_sentences"]
    )
    stripped = text.strip()
    if not stripped:
        return stripped

    # Split on . ! ? only when not part of an ordinal list marker (digit before .).
    # Nested lookbehind: the punctuation itself must not be preceded by a digit
    # (a bare (?<!\\d) before (?<=[.!?]) only sees the punctuation char).
    parts = re.split(r"(?<=(?<!\d)[.!?])\s+", stripped)
    parts = [p for p in parts if p]
    if len(parts) <= max_sentences:
        return _strip_dangling_list_markers(stripped)

    truncated = " ".join(parts[:max_sentences]).strip()
    logger.debug(
        "Truncated response from %s to %s sentences (channel=%s)",
        len(parts),
        max_sentences,
        channel,
    )
    return _strip_dangling_list_markers(truncated)


async def log_inbound_message(
    business_id: str,
    conversation_id: str,
    channel: str,
    incoming_text: str,
    incoming_type: str,
    incoming_external_id: str | None,
    *,
    is_owner: bool = False,
) -> None:
    """Log only the inbound side of a turn.

    Temporal workflows consume matching customer events before the ReAct agent
    runs, so those turns have no immediate AI response to log.
    """
    db_channel = "voice" if channel in ("whatsapp_voice", "voice") else channel
    inbound_role = "owner" if is_owner else "customer"
    try:
        await call_mcp_tool(
            "core",
            "log_message",
            {
                "business_id": business_id,
                "conversation_id": conversation_id,
                "role": inbound_role,
                "content": incoming_text,
                "channel": db_channel,
                "content_type": incoming_type or "text",
                "external_message_id": incoming_external_id,
            },
        )
    except Exception as exc:
        logger.error("Failed to log inbound message: %s", exc)


async def log_messages(
    business_id: str,
    conversation_id: str,
    channel: str,
    incoming_text: str,
    incoming_type: str,
    incoming_external_id: str | None,
    response_text: str,
    ai_metadata: dict[str, Any],
    *,
    is_owner: bool = False,
) -> None:
    """Log inbound + outbound messages via Core MCP log_message."""
    db_channel = "voice" if channel in ("whatsapp_voice", "voice") else channel
    await log_inbound_message(
        business_id,
        conversation_id,
        channel,
        incoming_text,
        incoming_type,
        incoming_external_id,
        is_owner=is_owner,
    )

    try:
        await call_mcp_tool(
            "core",
            "log_message",
            {
                "business_id": business_id,
                "conversation_id": conversation_id,
                "role": "ai",
                "content": response_text,
                "channel": db_channel,
                "content_type": "text",
                "ai_metadata": ai_metadata,
            },
        )
    except Exception as exc:
        logger.error("Failed to log AI message: %s", exc)


async def increment_usage(business_id: str, config: dict[str, Any] | None = None) -> None:
    config = config or await load_business_config(business_id)
    billing_start = str(config.get("billing_cycle_start") or "")[:10]
    if not billing_start:
        billing_start = date.today().replace(day=1).isoformat()
    try:
        redis = get_redis()
        key = usage_key(business_id, billing_start)
        await redis.incr(key)
        await redis.expire(key, 86400 * 45)
    except Exception as exc:
        logger.debug("usage incr failed: %s", exc)


async def invalidate_business_config_cache(business_id: str) -> None:
    try:
        await get_redis().delete(business_config_key(business_id))
    except Exception:
        pass


async def post_process(
    business_id: str,
    customer_id: str | None,
    ai_metadata: dict[str, Any],
    tool_calls_made: list[str],
    *,
    conversation_id: str | None = None,
    external_sender_id: str | None = None,
    channel: str | None = None,
    message_text: str | None = None,
    response_text: str | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    store: Any = None,
) -> None:
    """Post steps after a successful reply (§8.1 Step 11)."""
    if customer_id:
        try:
            async with tenant_connection(business_id) as conn:
                await conn.execute(
                    "UPDATE customers SET last_seen_at = now() WHERE id = $1::uuid",
                    customer_id,
                )
        except Exception as exc:
            logger.debug("last_seen update failed: %s", exc)

    if "update_business_info" in tool_calls_made or "upsert_business_context" in tool_calls_made:
        await invalidate_business_config_cache(business_id)

    if conversation_id and should_flag_for_daily_summary(ai_metadata):
        await flag_conversation_for_daily_summary(business_id, conversation_id)
        ai_metadata["flagged_for_daily_summary"] = True

    # Recurring gap (≥3) without escalate this turn → escalate once
    if (
        conversation_id
        and "log_gap" in tool_calls_made
        and "escalate_to_owner" not in tool_calls_made
        and not ai_metadata.get("escalated")
    ):
        from agent.confidence import extract_gap_occurrence_count

        occurrence = extract_gap_occurrence_count(tool_results or [])
        if occurrence is not None and occurrence >= 3:
            try:
                await call_mcp_tool(
                    "core",
                    "escalate_to_owner",
                    {
                        "business_id": business_id,
                        "reason": (
                            f"Recurring question ({occurrence} times): "
                            f"{(message_text or '')[:200]}"
                        ),
                        "customer_id": customer_id,
                        "conversation_id": conversation_id,
                        "priority": "normal",
                        "channel": channel or "whatsapp",
                        "context": "Auto-escalated after gap occurrence_count >= 3",
                    },
                )
                ai_metadata["escalated"] = True
                tools = list(ai_metadata.get("tools_called") or tool_calls_made)
                if "escalate_to_owner" not in tools:
                    tools.append("escalate_to_owner")
                ai_metadata["tools_called"] = tools
            except Exception as exc:
                logger.warning("Auto-escalate on recurring gap failed: %s", exc)

    if external_sender_id and not ai_metadata.get("is_owner"):
        try:
            from identity.bot_detection import increment_conversation_depth

            await increment_conversation_depth(business_id, external_sender_id)
        except Exception as exc:
            logger.debug("conversation depth incr failed: %s", exc)

    if store is not None and customer_id and not ai_metadata.get("is_owner"):
        try:
            from agent.memory import load_customer_memory, merge_customer_memory_profile

            prior = await load_customer_memory(store, business_id, customer_id, "profile")
            profile = merge_customer_memory_profile(
                prior,
                message_text=message_text,
                response_text=response_text,
                channel=channel,
                tool_calls_made=tool_calls_made,
                ai_metadata=ai_metadata,
            )
            await store_customer_memory(
                store,
                business_id,
                customer_id,
                "profile",
                profile,
            )
        except Exception as exc:
            logger.debug("store_customer_memory failed: %s", exc)


async def handle_limit_reached(
    business_id: str,
    *,
    channel: str,
    recipient_id: str,
    comment_id: str | None = None,
    skip_send: bool = False,
) -> None:
    """Notify customer and owner when message quota is exhausted."""
    if not skip_send:
        await send_response(
            channel=channel,
            recipient_id=recipient_id,
            text=LIMIT_REACHED_MESSAGE,
            business_id=business_id,
            comment_id=comment_id,
        )
    await notify_owner_of_limit(business_id)


def extract_message_text(content: Any) -> str:
    """Normalize webhook content payloads to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("text"):
            return str(content["text"])
        # WhatsApp structured content helpers may nest body
        body = content.get("body")
        if isinstance(body, dict) and body.get("text"):
            return str(body["text"])
        if isinstance(body, str):
            return body
        caption = content.get("caption")
        if caption:
            return str(caption)
        return json.dumps(content, default=str)[:2000]
    return str(content)


def receiving_account_from_event(event: dict[str, Any]) -> str | None:
    return (
        event.get("phone_number_id")
        or event.get("ig_account_id")
        or event.get("page_id")
        or event.get("receiving_account_id")
    )


def limit_reached_customer_text() -> str:
    return LIMIT_REACHED_MESSAGE


async def emit_ai_workflow_event(
    *,
    channel: str,
    business_id: str,
    customer_id: str | None,
    conversation_id: str | None,
    message_text: str,
    is_owner: bool,
    extra: dict[str, Any] | None = None,
    wait: bool = False,
) -> bool:
    """Route an inbound event to Temporal and report whether it was consumed.

    The result controls whether the ReAct agent may run, so routing must finish
    before the caller continues. ``wait`` remains for call-site compatibility.
    """
    if is_owner or not business_id:
        return False

    async def _run() -> bool:
        try:
            from workflow_builder.deployer import route_event_to_workflows
            from workflow_builder.models import CHANNEL_TO_TRIGGER

            trigger = CHANNEL_TO_TRIGGER.get(channel, channel)
            extra_data = dict(extra or {})
            event = {
                "type": trigger,
                "trigger_type": trigger,
                "channel": channel,
                "business_id": business_id,
                "customer_id": customer_id,
                "conversation_id": conversation_id,
                "message": {"text": message_text},
                "customer": {"id": customer_id},
                **extra_data,
            }
            return bool(await route_event_to_workflows(event, business_id))
        except Exception:
            logger.warning("WF_ROUTE inbound failed business=%s", business_id, exc_info=True)
            return False

    return await _run()

