"""Cross-channel session registry for the Voice+WhatsApp bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection
from shared.redis_client import get_redis
from voice.meta_api import send_whatsapp_text

logger = logging.getLogger(__name__)

CROSS_CHANNEL_KEY = "cross_channel:actor:{actor_key}"
CROSS_CHANNEL_CALL_KEY = "cross_channel:call:{call_id}"
CROSS_CHANNEL_ACTIVE_KEY = "cross_channel:active:{actor_key}"
CROSS_CHANNEL_ACTIVE_CALL_KEY = "cross_channel:active_call:{call_id}"
CROSS_CHANNEL_ACTIVE_SENDER_KEY = "cross_channel:active_sender:{business_id}:{sender}"
CROSS_CHANNEL_PENDING_KEY = "cross_channel:pending:{actor_key}"
CROSS_CHANNEL_TTL = 3600
CROSS_CHANNEL_ACTIVE_TTL = 14400
CROSS_CHANNEL_PENDING_TTL = 300
CROSS_CHANNEL_CONTENT_TIMEOUT = 300
CROSS_CHANNEL_DEFAULT_PROMPT = (
    "Please share the file or information here. I'll process it while we're on the call."
)
_background_tasks: set[asyncio.Task[Any]] = set()


def _run_background(coro: Any, *, label: str) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(finished: asyncio.Task[Any]) -> None:
        _background_tasks.discard(finished)
        if finished.cancelled():
            return
        try:
            finished.result()
        except Exception:
            logger.exception("CROSS_CHANNEL background task failed label=%s", label)

    task.add_done_callback(_done)


def actor_identity(
    *,
    business_id: str,
    is_owner: bool,
    customer_id: str | None,
    sender_external_id: str | None,
) -> tuple[str, str] | None:
    """Return (actor_type, opaque actor key) shared by voice and WhatsApp."""
    if is_owner and sender_external_id:
        from mcp_servers.meta_mcp.tools.helpers import normalize_e164

        phone = normalize_e164(sender_external_id)
        return "owner", f"owner:{business_id}:{phone}"
    if customer_id:
        return "customer", f"customer:{business_id}:{customer_id}"
    return None


def listener_key(actor_key: str) -> str:
    return CROSS_CHANNEL_KEY.format(actor_key=actor_key)


def call_map_key(call_id: str) -> str:
    return CROSS_CHANNEL_CALL_KEY.format(call_id=call_id)


def active_actor_key(actor_key: str) -> str:
    return CROSS_CHANNEL_ACTIVE_KEY.format(actor_key=actor_key)


def active_call_key(call_id: str) -> str:
    return CROSS_CHANNEL_ACTIVE_CALL_KEY.format(call_id=call_id)


def pending_actor_key(actor_key: str) -> str:
    return CROSS_CHANNEL_PENDING_KEY.format(actor_key=actor_key)


def active_sender_key(business_id: str, sender_external_id: str) -> str:
    from mcp_servers.meta_mcp.tools.helpers import normalize_e164

    return CROSS_CHANNEL_ACTIVE_SENDER_KEY.format(
        business_id=business_id,
        sender=normalize_e164(sender_external_id),
    )


def pubsub_channel(voice_session_id: str) -> str:
    return f"voice_session:{voice_session_id}:cross_channel"


async def check_cross_channel_listener(
    *,
    business_id: str,
    is_owner: bool,
    customer_id: str | None,
    sender_external_id: str | None,
) -> dict[str, Any] | None:
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not identity:
        logger.info(
            "CROSS_CHANNEL lookup skipped actor_identity_missing owner=%s",
            is_owner,
        )
        return None
    actor_type, actor_key = identity
    try:
        redis = get_redis()
        raw = await redis.get(listener_key(actor_key))
    except Exception:
        logger.debug("cross-channel redis lookup failed", exc_info=True)
        return None
    if not raw:
        logger.debug("CROSS_CHANNEL lookup miss actor_type=%s", actor_type)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("status") != "waiting":
        return None
    logger.info(
        "CROSS_CHANNEL lookup hit actor_type=%s call_id=%s",
        actor_type,
        data.get("voice_session_id"),
    )
    return data


async def mark_cross_channel_call_active(
    *,
    business_id: str,
    is_owner: bool,
    customer_id: str | None,
    sender_external_id: str | None,
    call_id: str,
) -> None:
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not identity or not call_id:
        return
    actor_type, actor_key = identity
    redis = get_redis()
    payload = json.dumps(
        {
            "actor_type": actor_type,
            "actor_key": actor_key,
            "business_id": business_id,
            "call_id": call_id,
            "customer_id": customer_id,
            "sender_external_id": sender_external_id,
        }
    )
    sender_key = active_sender_key(business_id, sender_external_id or "")
    await redis.setex(active_actor_key(actor_key), CROSS_CHANNEL_ACTIVE_TTL, payload)
    await redis.setex(sender_key, CROSS_CHANNEL_ACTIVE_TTL, payload)
    await redis.setex(
        active_call_key(call_id),
        CROSS_CHANNEL_ACTIVE_TTL,
        json.dumps({"actor_key": actor_key, "sender_key": sender_key}),
    )
    logger.info(
        "CROSS_CHANNEL active call actor_type=%s call_id=%s",
        actor_type,
        call_id,
    )


async def check_cross_channel_sender_route(
    *, business_id: str, sender_external_id: str
) -> dict[str, Any] | None:
    """Fast active-call lookup before the normal identity/chat pipeline."""
    if not business_id or not sender_external_id:
        return None
    try:
        raw = await get_redis().get(
            active_sender_key(business_id, sender_external_id)
        )
        route = json.loads(raw) if raw else None
    except Exception:
        logger.debug("cross-channel sender route lookup failed", exc_info=True)
        return None
    if not isinstance(route, dict) or not route.get("call_id"):
        return None
    route["status"] = "active_call"
    return route


async def check_cross_channel_route(
    *,
    business_id: str,
    is_owner: bool,
    customer_id: str | None,
    sender_external_id: str | None,
) -> dict[str, Any] | None:
    """Return an explicit listener or an active-call buffer route."""
    listener = await check_cross_channel_listener(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if listener:
        return listener
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not identity:
        return None
    _actor_type, actor_key = identity
    redis = get_redis()
    raw = await redis.get(active_actor_key(actor_key))
    if not raw:
        return None
    try:
        active = json.loads(raw)
    except json.JSONDecodeError:
        return None
    active["status"] = "active_call"
    logger.info(
        "CROSS_CHANNEL active route actor_type=%s call_id=%s",
        active.get("actor_type"),
        active.get("call_id"),
    )
    return active


async def pop_pending_cross_channel_input(
    *,
    business_id: str,
    is_owner: bool,
    customer_id: str | None,
    sender_external_id: str | None,
    call_id: str,
    redis: Any | None = None,
) -> dict[str, Any] | None:
    """Consume content buffered because it arrived before the voice tool call."""
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not identity:
        return None
    actor_type, actor_key = identity
    redis = redis or get_redis()
    key = pending_actor_key(actor_key)
    getdel = getattr(redis, "getdel", None)
    raw = await getdel(key) if callable(getdel) else await redis.get(key)
    if not raw:
        return None
    try:
        pending = json.loads(raw)
    except json.JSONDecodeError:
        if not callable(getdel):
            await redis.delete(key)
        return None
    if pending.get("voice_session_id") != call_id:
        if callable(getdel):
            await redis.setex(key, CROSS_CHANNEL_PENDING_TTL, raw)
        return None
    if not callable(getdel):
        await redis.delete(key)
    logger.info(
        "CROSS_CHANNEL pending consumed actor_type=%s call_id=%s",
        actor_type,
        call_id,
    )
    return pending.get("content") or {}


async def register_cross_channel_listener(
    customer_id: str | None,
    customer_phone: str,
    voice_session_id: str,
    livekit_room: str,
    business_id: str,
    expected_content: str,
    prompt_message: str,
    *,
    is_owner: bool = False,
) -> str:
    """Register Redis + DB listener and send the WhatsApp prompt. Returns session id."""
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=customer_phone,
    )
    if not identity:
        raise ValueError("Cross-channel actor identity is missing")
    actor_type, actor_key = identity
    normalized_sender = actor_key.rsplit(":", 1)[-1] if is_owner else customer_phone
    existing = await check_cross_channel_listener(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=customer_phone,
    )
    if existing and existing.get("voice_session_id") == voice_session_id:
        return str(existing.get("db_session_id") or "")

    redis = get_redis()
    prompt = (prompt_message or "").strip() or CROSS_CHANNEL_DEFAULT_PROMPT
    async with tenant_connection(business_id) as conn:
        session_id = await conn.fetchval(
            """
            INSERT INTO cross_channel_sessions
                (business_id, customer_id, voice_session_id, prompt_message,
                 expected_content, status, actor_type, sender_external_id)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, 'waiting', $6, $7)
            RETURNING id
            """,
            business_id,
            customer_id,
            voice_session_id,
            prompt,
            expected_content,
            actor_type,
            normalized_sender if is_owner else None,
        )

    listener = {
        "actor_key": actor_key,
        "actor_type": actor_type,
        "customer_id": customer_id,
        "sender_external_id": normalized_sender,
        "voice_session_id": voice_session_id,
        "livekit_room": livekit_room,
        "business_id": business_id,
        "expected_content": expected_content or "any",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "waiting",
        "db_session_id": str(session_id),
    }
    payload = json.dumps(listener)
    await redis.setex(listener_key(actor_key), CROSS_CHANNEL_TTL, payload)
    await redis.setex(call_map_key(voice_session_id), CROSS_CHANNEL_TTL, actor_key)

    try:
        await send_whatsapp_text(
            business_id=business_id,
            to=customer_phone,
            text=prompt,
        )
    except Exception:
        # A listener is useful only if the caller received the prompt. Roll
        # back both hot-path keys and the audit status on Graph auth/send errors.
        await redis.delete(
            listener_key(actor_key),
            call_map_key(voice_session_id),
        )
        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                UPDATE cross_channel_sessions
                SET status = 'cancelled'
                WHERE id = $1::uuid AND status = 'waiting'
                """,
                session_id,
            )
        logger.warning(
            "CROSS_CHANNEL registration rolled back actor_type=%s call_id=%s",
            actor_type,
            voice_session_id,
        )
        raise
    logger.info(
        "CROSS_CHANNEL registered actor_type=%s customer_present=%s call_id=%s session=%s",
        actor_type,
        bool(customer_id),
        voice_session_id,
        session_id,
    )
    return str(session_id)


async def complete_cross_channel(
    actor_key: str,
    processed_content: dict[str, Any],
) -> None:
    redis = get_redis()
    key = listener_key(actor_key)
    raw = await redis.get(key)
    if not raw:
        return
    listener = json.loads(raw)
    voice_session_id = str(listener.get("voice_session_id") or "")
    business_id = str(listener.get("business_id") or "")
    session_id = str(listener.get("db_session_id") or "")
    await redis.delete(key)
    if voice_session_id:
        await redis.delete(call_map_key(voice_session_id))

    if voice_session_id:
        await redis.publish(
            pubsub_channel(voice_session_id),
            json.dumps(
                {
                    "type": "cross_channel_content",
                    "content": processed_content,
                    "actor_type": listener.get("actor_type"),
                    "customer_id": listener.get("customer_id"),
                }
            ),
        )

    async def _audit_completion() -> None:
        if not business_id:
            return
        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                UPDATE cross_channel_sessions
                SET status = 'received',
                    received_content = $1::jsonb,
                    received_at = now()
                WHERE id = $2::uuid
                  AND status = 'waiting'
                """,
                json.dumps(processed_content),
                session_id,
            )

    _run_background(_audit_completion(), label="complete_audit")
    logger.info(
        "CROSS_CHANNEL completed actor_type=%s call_id=%s",
        listener.get("actor_type"),
        voice_session_id,
    )


async def _close_listener(
    actor_key: str,
    *,
    business_id: str | None,
    status: str,
) -> None:
    redis = get_redis()
    raw = await redis.get(listener_key(actor_key))
    voice_session_id = ""
    bid = business_id or ""
    session_id = ""
    actor_type = ""
    if raw:
        try:
            listener = json.loads(raw)
        except json.JSONDecodeError:
            listener = {}
        voice_session_id = str(listener.get("voice_session_id") or "")
        bid = bid or str(listener.get("business_id") or "")
        session_id = str(listener.get("db_session_id") or "")
        actor_type = str(listener.get("actor_type") or "")
    await redis.delete(listener_key(actor_key))
    if voice_session_id:
        await redis.delete(call_map_key(voice_session_id))
    if bid and session_id:
        async with tenant_connection(bid) as conn:
            await conn.execute(
                """
                UPDATE cross_channel_sessions
                SET status = $1
                WHERE id = $2::uuid
                  AND status = 'waiting'
                """,
                status,
                session_id,
            )
    logger.info(
        "CROSS_CHANNEL closed actor_type=%s call_id=%s status=%s",
        actor_type,
        voice_session_id,
        status,
    )


async def cancel_cross_channel(
    actor_key: str,
    *,
    business_id: str | None = None,
    status: str = "cancelled",
) -> None:
    if not actor_key:
        return
    try:
        await _close_listener(actor_key, business_id=business_id, status=status)
    except Exception:
        logger.debug("CROSS_CHANNEL cancel failed", exc_info=True)


async def cancel_cross_channel_for_call(call_id: str, *, business_id: str | None = None) -> None:
    if not call_id:
        return
    try:
        redis = get_redis()
        actor_key = await redis.get(call_map_key(call_id))
        if actor_key:
            await cancel_cross_channel(
                str(actor_key), business_id=business_id, status="cancelled"
            )
        await redis.delete(call_map_key(call_id))
        active_raw = await redis.get(active_call_key(call_id))
        if active_raw:
            try:
                active = json.loads(active_raw)
            except (TypeError, json.JSONDecodeError):
                active = {"actor_key": str(active_raw)}
            actor_key = str(active.get("actor_key") or "")
            sender_key = str(active.get("sender_key") or "")
            if actor_key:
                await redis.delete(active_actor_key(actor_key))
            if sender_key:
                await redis.delete(sender_key)
        await redis.delete(active_call_key(call_id))
    except Exception:
        logger.debug("CROSS_CHANNEL hangup cleanup skipped call_id=%s", call_id, exc_info=True)


async def handle_inbound_whatsapp_if_listening(
    *,
    customer_id: str | None,
    is_owner: bool,
    sender_external_id: str,
    business_id: str,
    conversation_id: str,
    message_text: str,
    message_type: str,
    media_id: str | None,
    media_url: str | None,
    filename: str | None,
    mime_type: str | None,
    external_message_id: str | None,
) -> dict[str, Any] | None:
    """If a voice session is waiting, consume this WhatsApp message and skip ReAct."""
    started = time.perf_counter()
    identity = actor_identity(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not identity:
        return None
    actor_type, actor_key = identity
    listener = await check_cross_channel_listener(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    route = listener or await check_cross_channel_route(
        business_id=business_id,
        is_owner=is_owner,
        customer_id=customer_id,
        sender_external_id=sender_external_id,
    )
    if not route:
        return None

    from shared.document_processor import process_whatsapp_media

    if media_id or media_url:
        processed = await process_whatsapp_media(
            business_id,
            media_id=media_id,
            media_url=media_url,
            message_type=message_type,
            filename=filename,
            mime_type=mime_type,
        )
    else:
        processed = {
            "type": "text",
            "text": message_text,
            "tables": [],
            "metadata": {},
        }

    voice_session_id = str(
        route.get("voice_session_id") or route.get("call_id") or ""
    )
    if listener:
        await complete_cross_channel(actor_key, processed)
    else:
        redis = get_redis()
        await redis.setex(
            pending_actor_key(actor_key),
            CROSS_CHANNEL_PENDING_TTL,
            json.dumps(
                {
                    "voice_session_id": voice_session_id,
                    "content": processed,
                }
            ),
        )
        await redis.publish(
            pubsub_channel(voice_session_id),
            json.dumps(
                {
                    "type": "cross_channel_pending_content",
                    "voice_session_id": voice_session_id,
                    "actor_type": actor_type,
                }
            ),
        )
        normalized_sender = (
            actor_key.rsplit(":", 1)[-1] if actor_type == "owner" else None
        )
        tenant_factory = tenant_connection

        async def _audit_buffer() -> None:
            async with tenant_factory(business_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO cross_channel_sessions
                        (business_id, customer_id, voice_session_id, prompt_message,
                         expected_content, status, actor_type, sender_external_id,
                         received_content, received_at)
                    VALUES
                        ($1::uuid, $2::uuid, $3, 'Buffered during active voice call',
                         'any', 'received', $4, $5, $6::jsonb, now())
                    """,
                    business_id,
                    customer_id,
                    voice_session_id,
                    actor_type,
                    normalized_sender,
                    json.dumps(processed),
                )

        _run_background(_audit_buffer(), label="buffer_audit")
        logger.info(
            "CROSS_CHANNEL buffered actor_type=%s call_id=%s",
            actor_type,
            voice_session_id,
        )
    delivery_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "CROSS_CHANNEL_LATENCY call_id=%s stage=delivered duration_ms=%d buffered=%s kind=%s",
        voice_session_id,
        delivery_ms,
        not bool(listener),
        processed.get("type"),
    )

    from agent.pipeline import log_messages

    log_fn = log_messages

    async def _audit_message() -> None:
        if not conversation_id:
            return

        await log_fn(
            business_id=business_id,
            conversation_id=conversation_id,
            channel="whatsapp",
            incoming_text=message_text or "[cross-channel content]",
            incoming_type=message_type,
            incoming_external_id=external_message_id,
            response_text="Handled on the voice call.",
            ai_metadata={
                "handled_by": "cross_channel_bridge",
                "voice_session_id": voice_session_id,
                "buffered_before_tool": not bool(listener),
            },
            is_owner=is_owner,
        )

    _run_background(_audit_message(), label="message_audit")
    logger.info(
        "CROSS_CHANNEL_LATENCY call_id=%s stage=audit_scheduled total_ms=%d",
        voice_session_id,
        round((time.perf_counter() - started) * 1000),
    )
    return {
        "status": "cross_channel_handled",
        "response_text": None,
        "thread_id": "",
        "voice_session_id": voice_session_id,
        "outbound_sent": False,
        "business_id": business_id,
    }
