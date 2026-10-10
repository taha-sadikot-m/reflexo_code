"""Inbound WhatsApp Calling: identity, SDP, Meta accept, LiveKit bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from identity.models import Channel, IdentityResolutionError
from identity.resolver import resolve_identity
from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.webhooks.resolve import publish_event, resolve_business_from_channel
from mcp_servers.meta_mcp.redis_keys import voice_channel
from voice.bridge import WhatsAppAudioBridge
from voice.config import get_voice_settings
from voice.core_tools import load_business_profile
from voice.errors import (
    VoiceCapacityError,
    VoiceError,
    VoiceNotConfiguredError,
    VoiceSdpError,
)
from voice.events import CallEventType, emit_call_event
from voice.limits import acquire_call_slot, release_call_slot
from voice.logging import get_or_create_voice_conversation
from voice.meta_api import accept_call, pre_accept_call, terminate_call
from voice.outbound import (
    clear_pickup,
    discard_outbound,
    is_pending_outbound,
    pop_outbound_context,
    resolve_outbound_answer,
    signal_pickup,
)
from voice.userdata import UserData
from voice.webhook_parse import (
    call_event_name,
    call_id_of,
    caller_name_of,
    classify_call_event,
    is_business_initiated,
    iter_call_records,
    phone_number_id_of,
    sdp_offer_of,
)

logger = logging.getLogger(__name__)

_EVENT_TYPES = {
    "connect": CallEventType.CONNECT,
    "connected": CallEventType.CONNECTED,
    "ringing": CallEventType.CONNECT,
    "accepted": CallEventType.CONNECTED,
    "timeout": CallEventType.TIMEOUT,
    "error": CallEventType.ERROR,
}

_active_calls: dict[str, WhatsAppAudioBridge] = {}
_pending_calls: dict[str, asyncio.Event] = {}
_connect_locks: dict[str, asyncio.Lock] = {}


class _CallEndedDuringSetup(VoiceError):
    pass


def _lock_for(call_id: str) -> asyncio.Lock:
    lock = _connect_locks.get(call_id)
    if lock is None:
        lock = asyncio.Lock()
        _connect_locks[call_id] = lock
    return lock


def _drop_lock_if_idle(call_id: str) -> None:
    """Locks are per call_id, so forget them once the call is gone."""
    if call_id in _pending_calls or call_id in _active_calls:
        return
    lock = _connect_locks.get(call_id)
    if lock is not None and not lock.locked():
        _connect_locks.pop(call_id, None)


def register_active_call(call_id: str, bridge: WhatsAppAudioBridge) -> None:
    """Hand a fully bridged call to the shared registry that handles hangup."""
    _active_calls[call_id] = bridge


def _ensure_call_pending(ended: asyncio.Event) -> None:
    if ended.is_set():
        raise _CallEndedDuringSetup("caller ended during setup")


async def _wait_for_agent_or_end(
    bridge: WhatsAppAudioBridge,
    ended: asyncio.Event,
) -> bool:
    agent_task = asyncio.create_task(bridge.wait_for_agent())
    ended_task = asyncio.create_task(ended.wait())
    try:
        done, _ = await asyncio.wait(
            {agent_task, ended_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ended_task in done and ended.is_set():
            raise _CallEndedDuringSetup("caller ended while agent was joining")
        return bool(await agent_task)
    finally:
        for task in (agent_task, ended_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(agent_task, ended_task, return_exceptions=True)


async def process_call_payload(waba_id: str, value: dict[str, Any]) -> None:
    for call in iter_call_records(value):
        await _process_one_call(waba_id, value, call)


async def _process_one_call(waba_id: str, value: dict[str, Any], call: dict[str, Any]) -> None:
    event_name = classify_call_event(call_event_name(call))
    call_id = call_id_of(call, value.get("call_id"))
    phone_number_id = phone_number_id_of(value, call)
    if not phone_number_id:
        to_number = str(call.get("to") or "")
        if to_number:
            from mcp_servers.meta_mcp.webhooks.whatsapp import get_phone_number_id_from_number

            phone_number_id = await get_phone_number_id_from_number(to_number) or ""
    if not phone_number_id:
        logger.warning("VOICE_CALL dropped missing phone_number_id waba=%s", waba_id)
        return

    business_id = await resolve_business_from_channel("whatsapp", phone_number_id)
    if not business_id:
        logger.warning("VOICE_CALL no business phone_number_id=%s", phone_number_id)
        return

    emit_call_event(
        _EVENT_TYPES.get(event_name, CallEventType.HANGUP),
        call_id=call_id or "-",
        business_id=business_id,
        phone_number_id=phone_number_id,
        extra={"event": event_name},
    )

    if event_name == "connect":
        if is_business_initiated(call) or is_pending_outbound(call_id):
            # We placed this call, so the session carries Meta's SDP answer.
            resolve_outbound_answer(call_id, sdp_offer_of(call))
            return
        await handle_inbound_call(
            call_id=call_id,
            caller=str(call.get("from") or ""),
            business_number=str(call.get("to") or ""),
            phone_number_id=phone_number_id,
            business_id=business_id,
            sdp_offer=sdp_offer_of(call),
            timestamp=str(call.get("timestamp") or value.get("timestamp") or ""),
            caller_name=caller_name_of(value, call),
        )
        return

    if event_name == "ringing":
        logger.info("VOICE_CALL outbound ringing call_id=%s", call_id)
        return

    if event_name == "accepted":
        await signal_pickup(call_id)
        await publish_event(
            voice_channel(business_id),
            {
                "event_type": "whatsapp_call_answered",
                "business_id": business_id,
                "call_id": call_id,
                "timestamp": call.get("timestamp") or value.get("timestamp"),
            },
        )
        return

    if event_name in {"hangup", "timeout", "error", "connected"}:
        if event_name != "connected":
            await handle_hangup(call_id, business_id, reason=event_name)
        await publish_event(
            voice_channel(business_id),
            {
                "event_type": (
                    "whatsapp_call_connected"
                    if event_name == "connected"
                    else "whatsapp_call_ended"
                ),
                "business_id": business_id,
                "call_id": call_id,
                "reason": event_name,
                "timestamp": call.get("timestamp") or value.get("timestamp"),
            },
        )


async def handle_inbound_call(
    *,
    call_id: str,
    caller: str,
    business_number: str,
    phone_number_id: str,
    business_id: str,
    sdp_offer: str,
    timestamp: str,
    caller_name: str = "",
) -> None:
    if not call_id:
        logger.warning("VOICE_CALL connect missing call_id")
        return

    async with _lock_for(call_id):
        if call_id in _active_calls or call_id in _pending_calls:
            logger.info("VOICE_CALL duplicate connect ignored call_id=%s", call_id)
            return
        ended = asyncio.Event()
        _pending_calls[call_id] = ended

        settings = get_voice_settings()
        if not settings.is_configured():
            logger.error(
                "VOICE_CALL skipped — LiveKit/Deepgram not configured call_id=%s",
                call_id,
            )
            _pending_calls.pop(call_id, None)
            return

        slot_held = False
        registered = False
        bridge: WhatsAppAudioBridge | None = None
        token: str | None = None
        setup_started = time.perf_counter()
        last_stage = setup_started

        def _stage(name: str) -> None:
            nonlocal last_stage
            now = time.perf_counter()
            logger.info(
                "VOICE_SETUP_LATENCY call_id=%s stage=%s duration_ms=%d total_ms=%d",
                call_id,
                name,
                round((now - last_stage) * 1000),
                round((now - setup_started) * 1000),
            )
            last_stage = now

        try:
            await acquire_call_slot(business_id)
            _stage("capacity")
            slot_held = True
            _ensure_call_pending(ended)

            try:
                identity = await resolve_identity(
                    channel=Channel.VOICE.value,
                    sender_external_id=caller,
                    receiving_account_id=phone_number_id,
                    sender_display_name=caller_name or None,
                )
                _stage("identity")
            except IdentityResolutionError:
                logger.exception("VOICE_CALL identity failed call_id=%s", call_id)
                return
            _ensure_call_pending(ended)

            profile = await load_business_profile(business_id)
            _stage("profile")
            conversation_id = await get_or_create_voice_conversation(
                business_id,
                customer_id=identity.customer_id,
                call_id=call_id,
            )
            _stage("conversation")
            outbound = await pop_outbound_context(call_id)
            outbound_reason = str((outbound or {}).get("reason") or "")
            outbound_customer_name = str(
                (outbound or {}).get("customer_name") or ""
            )
            userdata = UserData(
                business_id=business_id,
                call_id=call_id,
                phone_number_id=phone_number_id,
                caller_phone=caller,
                is_owner=identity.is_owner,
                customer_id=identity.customer_id,
                customer_name=(
                    outbound_customer_name
                    or identity.customer_name
                    or caller_name
                ),
                business_name=str(profile.get("name") or "the business"),
                persona_name=str(profile.get("persona_name") or "the assistant"),
                greeting_message=str(profile.get("greeting_message") or ""),
                conversation_id=conversation_id,
                language_preference=str(profile.get("persona_language") or "en"),
                outbound_reason=outbound_reason,
                outbound_customer_name=outbound_customer_name,
                extra={"business_profile": profile},
            )

            creds = await ensure_valid_token(business_id, "whatsapp")
            token = creds["access_token"]
            _stage("token")
            _ensure_call_pending(ended)

            bridge = WhatsAppAudioBridge(call_id=call_id)
            sdp_answer = await bridge.create_sdp_answer(sdp_offer)
            _stage("sdp")
            _ensure_call_pending(ended)
            await pre_accept_call(
                phone_number_id,
                call_id,
                token,
                business_id=business_id,
                sdp_answer=sdp_answer,
            )
            _stage("pre_accept")
            _ensure_call_pending(ended)

            room_name = f"wa-call-{call_id}"
            await bridge.connect_to_livekit(
                room_name=room_name,
                livekit_url=settings.livekit_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
                metadata=userdata.to_json_dict(),
            )
            _stage("livekit_connect")
            if not await _wait_for_agent_or_end(bridge, ended):
                raise VoiceError("LiveKit voice agent did not join before timeout")
            _stage("agent_audio_ready")
            _ensure_call_pending(ended)
            await accept_call(
                phone_number_id,
                call_id,
                token,
                sdp_answer,
                business_id=business_id,
            )
            _stage("accept")
            _ensure_call_pending(ended)
            emit_call_event(
                CallEventType.SDP_ACCEPTED,
                call_id=call_id,
                business_id=business_id,
                phone_number_id=phone_number_id,
            )
            _active_calls[call_id] = bridge
            registered = True
            try:
                from voice.cross_channel import mark_cross_channel_call_active

                await mark_cross_channel_call_active(
                    business_id=business_id,
                    is_owner=identity.is_owner,
                    customer_id=identity.customer_id,
                    sender_external_id=caller,
                    call_id=call_id,
                )
            except Exception:
                logger.warning(
                    "VOICE_CALL active actor marker failed call_id=%s",
                    call_id,
                    exc_info=True,
                )
            emit_call_event(
                CallEventType.BRIDGE_READY,
                call_id=call_id,
                business_id=business_id,
                phone_number_id=phone_number_id,
            )
            await publish_event(
                voice_channel(business_id),
                {
                    "event_type": "whatsapp_call_started",
                    "business_id": business_id,
                    "call_id": call_id,
                    "caller": caller,
                    "business_number": business_number,
                    "phone_number_id": phone_number_id,
                    "timestamp": timestamp,
                    "room_name": room_name,
                },
            )
            logger.info(
                "VOICE_CALL accepted call_id=%s business=%s owner=%s",
                call_id,
                business_id,
                identity.is_owner,
            )
        except _CallEndedDuringSetup:
            logger.info("VOICE_CALL setup cancelled by hangup call_id=%s", call_id)
        except (VoiceSdpError, VoiceCapacityError, VoiceNotConfiguredError, VoiceError):
            logger.exception("VOICE_CALL failed call_id=%s", call_id)
            if token and not ended.is_set():
                try:
                    await terminate_call(
                        phone_number_id,
                        call_id,
                        token,
                        business_id=business_id,
                    )
                except Exception:
                    logger.warning(
                        "VOICE_CALL best-effort terminate failed call_id=%s",
                        call_id,
                        exc_info=True,
                    )
        except Exception:
            logger.exception("VOICE_CALL unexpected failure call_id=%s", call_id)
            if token and not ended.is_set():
                try:
                    await terminate_call(
                        phone_number_id,
                        call_id,
                        token,
                        business_id=business_id,
                    )
                except Exception:
                    logger.warning(
                        "VOICE_CALL best-effort terminate failed call_id=%s",
                        call_id,
                        exc_info=True,
                    )
        finally:
            if _pending_calls.get(call_id) is ended:
                _pending_calls.pop(call_id, None)
            # Once the bridge is registered, handle_hangup owns the teardown.
            if not registered:
                if bridge is not None:
                    await bridge.close()
                if slot_held:
                    await release_call_slot(business_id)
    _drop_lock_if_idle(call_id)


async def handle_hangup(call_id: str, business_id: str, *, reason: str) -> None:
    if not call_id:
        return
    pending = _pending_calls.get(call_id)
    if pending is not None:
        pending.set()
    async with _lock_for(call_id):
        bridge = _active_calls.pop(call_id, None)
        if bridge is not None:
            await bridge.close()
            await release_call_slot(business_id)
    discard_outbound(call_id)
    await clear_pickup(call_id)
    _drop_lock_if_idle(call_id)
    try:
        from voice.cross_channel import cancel_cross_channel_for_call

        await cancel_cross_channel_for_call(call_id, business_id=business_id)
    except Exception:
        logger.debug("VOICE_CALL cross-channel hangup cleanup skipped", exc_info=True)
    emit_call_event(
        CallEventType.CLEANUP,
        call_id=call_id,
        business_id=business_id,
        extra={"reason": reason},
    )
    logger.info("VOICE_CALL hangup call_id=%s reason=%s", call_id, reason)


def active_call_ids() -> list[str]:
    return list(_active_calls.keys())
