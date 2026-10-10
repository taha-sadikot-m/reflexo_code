"""Business-initiated WhatsApp calls: SDP offer, Meta connect, pickup gating.

Outbound is the mirror of the inbound flow. We create the SDP offer, Meta
returns its answer on the `calls` webhook, and the customer's phone only starts
ringing afterwards — so the agent joins LiveKit during the ring and holds its
opening line until pickup is signalled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from shared.redis_client import get_redis
from voice.bridge import WhatsAppAudioBridge
from voice.config import get_voice_settings
from voice.errors import VoiceError, VoiceNotConfiguredError
from voice.events import CallEventType, emit_call_event
from voice.limits import acquire_call_slot, release_call_slot
from voice.meta_api import extract_call_id, initiate_call, terminate_call

logger = logging.getLogger(__name__)

OUTBOUND_CONTEXT_KEY = "outbound:{call_id}"
OUTBOUND_CONTEXT_TTL = 120
PICKUP_KEY = "voice:pickup:{call_id}"
PICKUP_TTL = 300

# Meta posts the answer webhook within a second or two of a successful connect.
ANSWER_TIMEOUT = 20.0
# Ring time we allow before giving up on the customer answering.
PICKUP_TIMEOUT = 45.0

# call_id -> Future[str] holding the SDP answer from the connect webhook.
_outbound_answers: dict[str, asyncio.Future] = {}
# call_id -> business_id for calls that are ringing but not yet bridged.
_pending_outbound: dict[str, str] = {}


def outbound_context_key(call_id: str) -> str:
    return OUTBOUND_CONTEXT_KEY.format(call_id=call_id)


def pickup_key(call_id: str) -> str:
    return PICKUP_KEY.format(call_id=call_id)


# ── SDP answer handoff (webhook -> initiator) ──────────────────────────────


def answer_future(call_id: str) -> asyncio.Future:
    """Future for this call's SDP answer, created by whichever side is first."""
    future = _outbound_answers.get(call_id)
    if future is None or future.cancelled():
        future = asyncio.get_running_loop().create_future()
        _outbound_answers[call_id] = future
    return future


def resolve_outbound_answer(call_id: str, sdp_answer: str) -> None:
    """Hand the webhook's SDP answer to the waiting initiator."""
    future = answer_future(call_id)
    if not future.done():
        future.set_result(sdp_answer)
        logger.info("VOICE_OUTBOUND answer received call_id=%s", call_id)


def is_pending_outbound(call_id: str) -> bool:
    return bool(call_id) and call_id in _pending_outbound


def discard_outbound(call_id: str) -> None:
    """Drop per-call outbound state so nothing leaks after the call ends."""
    if not call_id:
        return
    _pending_outbound.pop(call_id, None)
    future = _outbound_answers.pop(call_id, None)
    if future is not None and not future.done():
        future.cancel()


# ── Pickup signalling (webhook/media -> voice worker) ──────────────────────


async def signal_pickup(call_id: str) -> None:
    """Tell the agent process the customer is on the line."""
    if not call_id:
        return
    key = pickup_key(call_id)
    try:
        redis = get_redis()
        await redis.setex(key, PICKUP_TTL, "1")
        await redis.publish(key, "1")
    except Exception:
        logger.warning(
            "VOICE_OUTBOUND pickup signal failed call_id=%s", call_id, exc_info=True
        )
        return
    logger.info("VOICE_OUTBOUND pickup signalled call_id=%s", call_id)


async def wait_for_pickup(call_id: str, timeout: float = PICKUP_TIMEOUT) -> bool:
    """Block in the agent process until the callee answers (or we give up)."""
    if not call_id:
        return False
    key = pickup_key(call_id)
    try:
        redis = get_redis()
    except Exception:
        logger.warning("VOICE_OUTBOUND pickup wait skipped — no redis", exc_info=True)
        return True

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(key)
        # Covers a pickup that landed before we subscribed.
        if await redis.get(key):
            return True

        async def _listen() -> bool:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    return True
            return False

        try:
            return await asyncio.wait_for(_listen(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("VOICE_OUTBOUND pickup wait timed out call_id=%s", call_id)
            return False
    finally:
        try:
            await pubsub.unsubscribe(key)
            await pubsub.aclose()
        except Exception:
            pass


async def clear_pickup(call_id: str) -> None:
    if not call_id:
        return
    try:
        redis = get_redis()
        await redis.delete(pickup_key(call_id))
    except Exception:
        logger.debug("VOICE_OUTBOUND pickup clear skipped", exc_info=True)


# ── Greeting context ───────────────────────────────────────────────────────


async def store_outbound_context(call_id: str, context: dict[str, Any]) -> None:
    """Remember why this outbound call was placed until the callee answers."""
    if not call_id:
        return
    try:
        redis = get_redis()
    except Exception:
        logger.debug("VOICE_OUTBOUND store skipped — redis unavailable", exc_info=True)
        return
    payload = {
        "business_id": str(context.get("business_id") or ""),
        "reason": str(context.get("reason") or ""),
        "customer_name": str(context.get("customer_name") or ""),
        "to_phone": str(context.get("to_phone") or ""),
    }
    await redis.setex(
        outbound_context_key(call_id),
        OUTBOUND_CONTEXT_TTL,
        json.dumps(payload),
    )
    logger.info(
        "VOICE_OUTBOUND context stored call_id=%s reason=%s",
        call_id,
        payload["reason"][:80],
    )


async def pop_outbound_context(call_id: str) -> dict[str, Any] | None:
    """Consume outbound context when the connect webhook arrives."""
    if not call_id:
        return None
    try:
        redis = get_redis()
    except Exception:
        logger.debug("VOICE_OUTBOUND pop skipped — redis unavailable", exc_info=True)
        return None
    key = outbound_context_key(call_id)
    getdel = getattr(redis, "getdel", None)
    raw = await getdel(key) if callable(getdel) else await redis.get(key)
    if not raw:
        return None
    if not callable(getdel):
        await redis.delete(key)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    logger.info("VOICE_OUTBOUND context consumed call_id=%s", call_id)
    return data


# ── Placing the call ───────────────────────────────────────────────────────


async def _prepare_outbound_session(
    *,
    business_id: str,
    call_id: str,
    phone_number_id: str,
    to_phone: str,
    reason: str,
    customer_name: str,
) -> Any:
    from identity.models import Channel
    from identity.resolver import resolve_identity
    from voice.core_tools import load_business_profile
    from voice.logging import get_or_create_voice_conversation
    from voice.userdata import UserData

    identity = await resolve_identity(
        channel=Channel.VOICE.value,
        sender_external_id=to_phone,
        receiving_account_id=phone_number_id,
        sender_display_name=customer_name or None,
    )
    profile = await load_business_profile(business_id)
    conversation_id = await get_or_create_voice_conversation(
        business_id,
        customer_id=identity.customer_id,
        call_id=call_id,
    )
    return identity, UserData(
        business_id=business_id,
        call_id=call_id,
        phone_number_id=phone_number_id,
        caller_phone=to_phone,
        is_owner=identity.is_owner,
        customer_id=identity.customer_id,
        customer_name=customer_name or identity.customer_name or "",
        business_name=str(profile.get("name") or "the business"),
        persona_name=str(profile.get("persona_name") or "the assistant"),
        greeting_message=str(profile.get("greeting_message") or ""),
        conversation_id=conversation_id,
        language_preference=str(profile.get("persona_language") or "en"),
        is_outbound=True,
        outbound_reason=reason,
        outbound_customer_name=customer_name,
        extra={"business_profile": profile},
    )


async def place_outbound_call(
    business_id: str,
    to_phone: str,
    *,
    reason: str = "",
    customer_name: str = "",
) -> str:
    """Ring a customer on WhatsApp with the voice agent already on the line.

    Must run in the process that owns the `calls` webhook — the peer connection
    created here is what the SDP answer gets applied to.
    """
    from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
    from mcp_servers.meta_mcp.tools.helpers import validate_e164
    from voice.call_handler import register_active_call

    settings = get_voice_settings()
    if not settings.is_configured():
        raise VoiceNotConfiguredError("LiveKit/Deepgram are not configured")

    to_e164 = validate_e164(to_phone)
    creds = await ensure_valid_token(business_id, "whatsapp")
    phone_number_id = str(creds["phone_number_id"])
    token = str(creds["access_token"])

    await acquire_call_slot(business_id)
    bridge = WhatsAppAudioBridge(call_id="pending")
    livekit_task: asyncio.Task | None = None
    call_id = ""
    registered = False
    started = time.perf_counter()
    last_stage = started

    def _stage(name: str) -> None:
        nonlocal last_stage
        now = time.perf_counter()
        logger.info(
            "VOICE_OUTBOUND_LATENCY call_id=%s stage=%s duration_ms=%d total_ms=%d",
            call_id or "pending",
            name,
            round((now - last_stage) * 1000),
            round((now - started) * 1000),
        )
        last_stage = now

    try:
        sdp_offer = await bridge.create_sdp_offer()
        _stage("sdp_offer")
        response = await initiate_call(
            phone_number_id,
            token,
            to_e164,
            sdp_offer,
            business_id=business_id,
        )
        call_id = extract_call_id(response)
        if not call_id:
            raise VoiceError("Meta did not return a call_id for the outbound call")
        _stage("graph_connect")

        bridge.call_id = call_id
        _pending_outbound[call_id] = business_id
        answer = answer_future(call_id)
        bridge.on_first_audio = lambda: signal_pickup(call_id)
        emit_call_event(
            CallEventType.CONNECT,
            call_id=call_id,
            business_id=business_id,
            phone_number_id=phone_number_id,
            extra={"direction": "outbound"},
        )
        await store_outbound_context(
            call_id,
            {
                "business_id": business_id,
                "reason": reason,
                "customer_name": customer_name,
                "to_phone": to_e164,
            },
        )

        identity, userdata = await _prepare_outbound_session(
            business_id=business_id,
            call_id=call_id,
            phone_number_id=phone_number_id,
            to_phone=to_e164,
            reason=reason,
            customer_name=customer_name,
        )
        _stage("identity_profile")

        # Warm the room and agent while the customer's phone is still ringing.
        livekit_task = asyncio.create_task(
            bridge.connect_to_livekit(
                room_name=f"wa-call-{call_id}",
                livekit_url=settings.livekit_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
                metadata=userdata.to_json_dict(),
            )
        )

        try:
            sdp_answer = await asyncio.wait_for(
                asyncio.shield(answer), timeout=ANSWER_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise VoiceError(
                "WhatsApp did not return an SDP answer for the outbound call"
            ) from exc
        await bridge.apply_sdp_answer(sdp_answer)
        _stage("sdp_answer")

        await livekit_task
        livekit_task = None
        _stage("livekit_connect")
        if not await bridge.wait_for_agent():
            raise VoiceError("LiveKit voice agent did not join before timeout")
        _stage("agent_audio_ready")

        register_active_call(call_id, bridge)
        registered = True
        discard_outbound(call_id)
        try:
            from voice.cross_channel import mark_cross_channel_call_active

            await mark_cross_channel_call_active(
                business_id=business_id,
                is_owner=identity.is_owner,
                customer_id=identity.customer_id,
                sender_external_id=to_e164,
                call_id=call_id,
            )
        except Exception:
            logger.warning(
                "VOICE_OUTBOUND active actor marker failed call_id=%s",
                call_id,
                exc_info=True,
            )
        emit_call_event(
            CallEventType.BRIDGE_READY,
            call_id=call_id,
            business_id=business_id,
            phone_number_id=phone_number_id,
        )
        logger.info(
            "VOICE_OUTBOUND bridged call_id=%s business=%s to=%s",
            call_id,
            business_id,
            to_e164,
        )
        return call_id
    finally:
        if not registered:
            if livekit_task is not None:
                livekit_task.cancel()
                await asyncio.gather(livekit_task, return_exceptions=True)
            if call_id:
                try:
                    await terminate_call(
                        phone_number_id,
                        call_id,
                        token,
                        business_id=business_id,
                    )
                except Exception:
                    logger.warning(
                        "VOICE_OUTBOUND best-effort terminate failed call_id=%s",
                        call_id,
                        exc_info=True,
                    )
                discard_outbound(call_id)
                await clear_pickup(call_id)
            await bridge.close()
            await release_call_slot(business_id)


async def initiate_outbound_call(
    business_id: str,
    to_phone: str,
    call_context: dict[str, Any] | None = None,
) -> str:
    """Back-compat wrapper around `place_outbound_call`."""
    context = dict(call_context or {})
    return await place_outbound_call(
        business_id,
        to_phone,
        reason=str(context.get("reason") or ""),
        customer_name=str(context.get("customer_name") or ""),
    )
