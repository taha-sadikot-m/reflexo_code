"""Create a LiveKit AgentSession for a WhatsApp call room."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from livekit.agents import APIConnectOptions, AgentSession, StopResponse, function_tool
from livekit.agents.llm import FallbackAdapter
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice import Agent
from livekit.plugins import deepgram, google, silero

from shared.redis_client import get_redis
from voice.agent import (
    build_instructions,
    tool_business_hours,
    tool_check_availability,
    tool_search_entities,
    tool_search_knowledge,
    tool_update_business_context,
    tool_update_business_info,
    tool_upsert_catalog_item,
)
from voice.config import get_voice_settings
from voice.core_tools import load_business_profile
from voice.cross_channel import pop_pending_cross_channel_input, pubsub_channel
from voice.tools import (
    format_proactive_whatsapp_content,
    format_whatsapp_input_result,
    request_whatsapp_input,
    spoken_whatsapp_fallback,
)
from voice.userdata import UserData

logger = logging.getLogger(__name__)

PROACTIVE_CONTENT_INSTRUCTIONS = (
    "WhatsApp content just arrived during this call and you can already read it. "
    "Speak now, in this order: acknowledge that you have it and name the file or "
    "message, give a one-sentence summary of what it actually contains, then ask "
    "exactly one question that moves the caller's current task forward, either "
    "confirming what they want done with it or clarifying a missing detail. "
    "Never ask them to resend it, never say you cannot see it, and never change a "
    "business record until they confirm."
)


async def process_voice_transcript(
    userdata: UserData,
    text: str,
    *,
    turn_id: str,
) -> dict[str, Any]:
    """Run one final STT transcript through the canonical agent pipeline."""
    from agent.session import handle_incoming_message

    return await handle_incoming_message(
        channel="voice",
        external_sender_id=userdata.caller_phone,
        message_text=text,
        receiving_account_id=userdata.phone_number_id,
        sender_display_name=userdata.customer_name or None,
        external_message_id=f"voice:{userdata.call_id}:{turn_id}",
        message_type="voice",
        skip_send=True,
        runtime_context={
            "voice_call_id": userdata.call_id,
            "is_outbound": userdata.is_outbound,
            "outbound_reason": userdata.outbound_reason,
        },
    )


async def dispatch_voice_turn(
    userdata: UserData,
    text: str,
    *,
    turn_id: str,
    speak: Any,
) -> dict[str, Any]:
    """Route a transcript and speak only the canonical pipeline response."""
    result = await process_voice_transcript(
        userdata,
        text,
        turn_id=turn_id,
    )
    response = str(result.get("response_text") or "").strip()
    if response:
        await speak(response)
    return result


def _build_fingerprint() -> str:
    """Identify the code build serving a call, so stale workers are obvious."""
    try:
        mtime = int(os.path.getmtime(__file__))
    except OSError:
        mtime = 0
    return f"{mtime}-{os.getpid()}"


async def start_voice_session(
    ctx: Any,
    userdata: UserData,
    *,
    prewarmed_vad: Any | None = None,
) -> None:
    """Attach STT/LLM/TTS + tools to an already-connected JobContext room."""
    session_started_at = time.time()
    first_tts_reported = False
    pending_content_ready_at: float | None = None
    settings = get_voice_settings()
    cached_profile = userdata.extra.get("business_profile")
    profile = (
        dict(cached_profile)
        if isinstance(cached_profile, dict) and cached_profile
        else await load_business_profile(userdata.business_id)
    )
    if not userdata.business_name:
        userdata.business_name = str(profile.get("name") or "the business")
    if not userdata.persona_name:
        userdata.persona_name = str(profile.get("persona_name") or "the assistant")
    if not userdata.greeting_message:
        userdata.greeting_message = str(profile.get("greeting_message") or "")

    instructions = build_instructions(userdata, profile)

    class BoundAgent(Agent):
        def __init__(self) -> None:
            super().__init__(instructions=instructions)
            self._ud = userdata
            hidden: set[str] = set()
            if not userdata.is_owner:
                hidden.update(
                    {
                        "update_business_info",
                        "update_business_hours",
                        "update_business_address",
                        "update_business_context",
                        "upsert_catalog_item",
                    }
                )
            has_whatsapp_actor = bool(
                userdata.customer_id
                or (userdata.is_owner and userdata.caller_phone)
            )
            if not has_whatsapp_actor:
                hidden.add("request_whatsapp_input")
            if hidden:
                self._tools = [
                    tool
                    for tool in self._tools
                    if getattr(tool, "id", "") not in hidden
                ]
            logger.info(
                "VOICE_TOOLSET call_id=%s owner=%s whatsapp_input=%s",
                userdata.call_id,
                userdata.is_owner,
                has_whatsapp_actor,
            )
            self._tool_announced = False
            self._tool_cache: dict[str, str] = {}
            self._last_pipeline_turn: tuple[str, float] | None = None

        def reset_tool_turn(self) -> None:
            self._tool_announced = False
            self._tool_cache.clear()

        @staticmethod
        def _tool_key(name: str, arguments: Any) -> str:
            def normalize(value: Any) -> Any:
                if isinstance(value, str):
                    return " ".join(value.casefold().split())
                if isinstance(value, dict):
                    return {
                        str(key): normalize(item)
                        for key, item in sorted(value.items())
                    }
                if isinstance(value, list):
                    return [normalize(item) for item in value]
                return value

            return name + ":" + json.dumps(
                normalize(arguments),
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )

        async def _run_tool_once(
            self,
            name: str,
            arguments: Any,
            operation: Any,
            announcement: str = "One moment while I handle that.",
        ) -> str:
            key = self._tool_key(name, arguments)
            if key in self._tool_cache:
                logger.info(
                    "VOICE_TOOL deduplicated call_id=%s tool=%s",
                    self._ud.call_id,
                    name,
                )
                return self._tool_cache[key]
            started = time.perf_counter()
            logger.info(
                "VOICE_LATENCY call_id=%s stage=tool_start tool=%s",
                self._ud.call_id,
                name,
            )
            announcement_started = False

            async def _announce_if_slow() -> None:
                nonlocal announcement_started
                await asyncio.sleep(0.75)
                announcement_started = True
                try:
                    await self.session.say(announcement, allow_interruptions=True)
                except Exception:
                    logger.debug(
                        "VOICE_TOOL progress speech skipped call_id=%s tool=%s",
                        self._ud.call_id,
                        name,
                        exc_info=True,
                    )

            progress_task = None
            if not self._tool_announced:
                self._tool_announced = True
                progress_task = asyncio.create_task(_announce_if_slow())
            try:
                result = await operation()
            finally:
                if progress_task is not None and not announcement_started:
                    progress_task.cancel()
                logger.info(
                    "VOICE_LATENCY call_id=%s stage=tool_end tool=%s duration_ms=%d",
                    self._ud.call_id,
                    name,
                    round((time.perf_counter() - started) * 1000),
                )
            self._tool_cache[key] = result
            return result

        def _owner_denied(self) -> str | None:
            if self._ud.is_owner:
                return None
            logger.warning(
                "VOICE_TOOL owner mutation denied call_id=%s",
                self._ud.call_id,
            )
            return "This update is available only to the verified business owner."

        async def on_enter(self) -> None:
            from voice.prompts import opening_line

            if self._ud.is_outbound:
                # We joined while the customer's phone was still ringing.
                from voice.outbound import wait_for_pickup

                if not await wait_for_pickup(self._ud.call_id):
                    logger.warning(
                        "VOICE_SESSION outbound never answered call_id=%s",
                        self._ud.call_id,
                    )
                    return
                logger.info(
                    "VOICE_SESSION outbound pickup call_id=%s", self._ud.call_id
                )
            await self.session.say(opening_line(self._ud), allow_interruptions=True)

        async def on_user_turn_completed(
            self,
            turn_ctx: Any,
            new_message: Any,
        ) -> None:
            del turn_ctx
            text = str(
                getattr(new_message, "text_content", None)
                or getattr(new_message, "raw_text_content", None)
                or ""
            ).strip()
            if not text:
                raise StopResponse()
            now = time.monotonic()
            if (
                self._last_pipeline_turn
                and self._last_pipeline_turn[0] == text
                and now - self._last_pipeline_turn[1] < 2.0
            ):
                logger.warning(
                    "VOICE_PIPELINE duplicate final suppressed call_id=%s",
                    self._ud.call_id,
                )
                raise StopResponse()
            try:
                result = await dispatch_voice_turn(
                    self._ud,
                    text,
                    turn_id=str(time.time_ns()),
                    speak=lambda response: self.session.say(
                        response,
                        allow_interruptions=True,
                    ),
                )
            except Exception:
                logger.exception(
                    "VOICE_PIPELINE failed call_id=%s",
                    self._ud.call_id,
                )
                await self.session.say(
                    "Sorry, I could not process that. Please say it once more.",
                    allow_interruptions=True,
                )
                raise StopResponse()
            self._last_pipeline_turn = (text, time.monotonic())
            logger.info(
                "VOICE_PIPELINE call_id=%s status=%s workflow=%s specialist=%s",
                self._ud.call_id,
                result.get("status"),
                result.get("workflow_matched"),
                result.get("specialist"),
            )
            # The canonical pipeline already selected/generated the response.
            # Prevent LiveKit's separate LLM from producing a duplicate.
            raise StopResponse()

        @function_tool
        async def search_knowledge(self, query: str) -> str:
            """Search the business FAQ and knowledge base."""
            return await self._run_tool_once(
                "search_knowledge",
                {"query": query},
                lambda: tool_search_knowledge(self._ud, query),
            )

        @function_tool
        async def search_entities(self, query: str) -> str:
            """Search products or services in the catalog."""
            return await self._run_tool_once(
                "search_entities",
                {"query": query},
                lambda: tool_search_entities(self._ud, query),
            )

        @function_tool
        async def get_business_hours(self) -> str:
            """Get today's opening hours."""
            return await self._run_tool_once(
                "get_business_hours",
                {},
                lambda: tool_business_hours(self._ud),
            )

        @function_tool
        async def check_availability(self, service_name: str, date: str = "") -> str:
            """Check free appointment slots for a service on a date (YYYY-MM-DD, IST)."""
            return await self._run_tool_once(
                "check_availability",
                {"service_name": service_name, "date": date},
                lambda: tool_check_availability(self._ud, service_name, date),
            )

        @function_tool
        async def update_business_info(self, field: str, value: str) -> str:
            """Owner only: update a simple business field such as description, greeting_message, website, persona_name, persona_tone, or custom_instructions."""
            if denied := self._owner_denied():
                return denied
            return await self._run_tool_once(
                "update_business_info",
                {"field": field, "value": value},
                lambda: tool_update_business_info(self._ud, field, value),
                "I'll save that change now.",
            )

        @function_tool
        async def update_business_hours(
            self,
            hours: dict[str, dict[str, str]],
        ) -> str:
            """Owner only: replace weekly hours. Keys are mon through sun and values contain open and close in 24-hour HH:MM format."""
            if denied := self._owner_denied():
                return denied
            return await self._run_tool_once(
                "update_business_hours",
                {"hours": hours},
                lambda: tool_update_business_info(
                    self._ud,
                    "business_hours",
                    hours,
                ),
                "I'll update the business hours now.",
            )

        @function_tool
        async def update_business_address(self, address: str) -> str:
            """Owner only: update the business's formatted street address."""
            if denied := self._owner_denied():
                return denied
            value = {"formatted": address}
            return await self._run_tool_once(
                "update_business_address",
                value,
                lambda: tool_update_business_info(self._ud, "address", value),
                "I'll update the address now.",
            )

        @function_tool
        async def update_business_context(self, path: str, value: str) -> str:
            """Owner only: save an extra business fact using a dotted path, for example contact.email or contact.phone."""
            if denied := self._owner_denied():
                return denied
            return await self._run_tool_once(
                "update_business_context",
                {"path": path, "value": value},
                lambda: tool_update_business_context(self._ud, path, value),
                "I'll save that business detail now.",
            )

        @function_tool
        async def upsert_catalog_item(
            self,
            entity_type: str,
            name: str,
            price: str = "",
            description: str = "",
            details: str = "",
        ) -> str:
            """Owner only: create or update a product, service, offering, location, policy, or other catalog item."""
            if denied := self._owner_denied():
                return denied
            data = {
                key: value
                for key, value in {"price": price, "details": details}.items()
                if value.strip()
            }
            return await self._run_tool_once(
                "upsert_catalog_item",
                {
                    "entity_type": entity_type,
                    "name": name,
                    "price": price,
                    "description": description,
                    "details": details,
                },
                lambda: tool_upsert_catalog_item(
                    self._ud,
                    entity_type,
                    name,
                    data,
                    description or None,
                ),
                "I'll update the catalog now.",
            )

        @function_tool
        async def request_whatsapp_input(
            self,
            prompt_message: str,
            expected_content: str = "any",
        ) -> str:
            """MANDATORY when the caller asks to send, or says they sent, anything on WhatsApp. Activates WhatsApp receipt for this call; never merely tell them to send it."""
            if not (
                self._ud.customer_id
                or (self._ud.is_owner and self._ud.caller_phone)
            ):
                return "WhatsApp file sharing is not available for this caller."
            room_name = str(getattr(getattr(ctx, "room", None), "name", "") or "")
            return await self._run_tool_once(
                "request_whatsapp_input",
                {"prompt_message": prompt_message, "expected_content": expected_content},
                lambda: _await_whatsapp_input(
                    self._ud, prompt_message, expected_content, room_name
                ),
                "I'll send you a WhatsApp message now. Please share it there.",
            )

        @function_tool
        async def escalate_to_human(self, reason: str) -> str:
            """Escalate when the caller wants a person."""
            await self.session.say(
                "Let me connect you with someone who can help.",
                allow_interruptions=True,
            )
            logger.info(
                "VOICE_ESCALATE call_id=%s reason=%s",
                self._ud.call_id,
                reason[:200],
            )
            return "The owner has been notified."

    llm_kwargs: dict = {
        "temperature": settings.voice_llm_temperature,
        "max_output_tokens": settings.voice_llm_max_output_tokens,
        "thinking_config": {"thinking_budget": settings.voice_llm_thinking_budget},
    }
    if settings.google_api_key:
        llm_kwargs["api_key"] = settings.google_api_key
    llm_models = [
        google.LLM(model=settings.agent_model, **llm_kwargs),
    ]
    if (
        settings.voice_llm_fallback_model
        and settings.voice_llm_fallback_model != settings.agent_model
    ):
        llm_models.append(
            google.LLM(model=settings.voice_llm_fallback_model, **llm_kwargs)
        )
    voice_llm: Any = (
        FallbackAdapter(
            llm_models,
            attempt_timeout=settings.voice_llm_attempt_timeout,
            max_retry_per_llm=settings.voice_llm_max_retry_per_model,
        )
        if len(llm_models) > 1
        else llm_models[0]
    )
    session = AgentSession(
        vad=prewarmed_vad or silero.VAD.load(),
        userdata=userdata,
        stt=deepgram.STT(
            model=settings.stt_model,
            language=settings.stt_language,
            smart_format=True,
            endpointing_ms=400,
            utterance_end_ms=1000,
            filler_words=True,
            api_key=settings.deepgram_api_key,
        ),
        llm=voice_llm,
        tts=deepgram.TTS(model=settings.tts_model, api_key=settings.deepgram_api_key),
        turn_handling={
            "endpointing": {
                "mode": "dynamic",
                "min_delay": settings.voice_endpointing_min_delay,
                "max_delay": settings.voice_endpointing_max_delay,
            },
            "preemptive_generation": {
                "enabled": False,
                "preemptive_tts": False,
                "max_retries": 1,
            },
        },
        transcription_timeout=2.0,
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                max_retry=0,
                timeout=settings.voice_llm_attempt_timeout,
            ),
            max_unrecoverable_errors=3,
        ),
    )

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev: Any) -> None:
        if getattr(ev, "is_final", True) is False:
            return
        text = getattr(ev, "transcript", None) or getattr(ev, "text", "") or ""
        if not text:
            return
        bound_agent.reset_tool_turn()
        logger.info("VOICE_STT call_id=%s chars=%s", userdata.call_id, len(text))
        # Canonical pipeline logging runs from on_user_turn_completed.

    @session.on("conversation_item_added")
    def _on_conversation_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        if (
            getattr(item, "type", None) != "message"
            or getattr(item, "role", None) != "assistant"
        ):
            return
        text = str(getattr(item, "raw_text_content", "") or "").strip()
        # Canonical pipeline logging owns assistant responses.

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:
        nonlocal first_tts_reported, pending_content_ready_at
        metric = getattr(ev, "metrics", None)
        if metric is None:
            return
        metric_type = str(getattr(metric, "type", type(metric).__name__))
        if metric_type not in {"llm_metrics", "tts_metrics", "stt_metrics", "eou_metrics"}:
            return
        fields = {
            "duration_ms": round(float(getattr(metric, "duration", 0) or 0) * 1000),
            "ttft_ms": round(float(getattr(metric, "ttft", 0) or 0) * 1000),
            "ttfb_ms": round(float(getattr(metric, "ttfb", 0) or 0) * 1000),
            "eou_ms": round(
                float(getattr(metric, "end_of_utterance_delay", 0) or 0) * 1000
            ),
            "transcription_ms": round(
                float(getattr(metric, "transcription_delay", 0) or 0) * 1000
            ),
        }
        if metric_type == "tts_metrics":
            first_audio_at = float(getattr(metric, "timestamp", 0) or 0) + float(
                getattr(metric, "ttfb", 0) or 0
            )
            if not first_tts_reported:
                first_tts_reported = True
                fields["session_to_first_audio_ms"] = round(
                    max(0.0, first_audio_at - session_started_at) * 1000
                )
            if pending_content_ready_at is not None:
                fields["file_to_spoken_ack_ms"] = round(
                    max(0.0, first_audio_at - pending_content_ready_at) * 1000
                )
                pending_content_ready_at = None
        logger.info(
            "VOICE_LATENCY call_id=%s stage=metric type=%s %s",
            userdata.call_id,
            metric_type,
            " ".join(f"{key}={value}" for key, value in fields.items() if value),
        )

    @session.on("function_tools_executed")
    def _on_tools_executed(ev: Any) -> None:
        calls = getattr(ev, "function_calls", None) or []
        logger.info(
            "VOICE_LATENCY call_id=%s stage=tools_executed tools=%s",
            userdata.call_id,
            ",".join(str(getattr(call, "name", "")) for call in calls),
        )

    recovery_active = False

    @session.on("error")
    def _on_session_error(ev: Any) -> None:
        nonlocal recovery_active
        logger.error(
            "VOICE_SESSION_ERROR call_id=%s source=%s error=%s",
            userdata.call_id,
            type(getattr(ev, "source", None)).__name__,
            str(getattr(ev, "error", "unknown"))[:500],
        )
        source_name = type(getattr(ev, "source", None)).__name__
        if source_name not in {"LLM", "FallbackAdapter"} or recovery_active:
            return
        recovery_active = True

        async def _speak_recovery() -> None:
            nonlocal recovery_active
            try:
                handle = session.say(
                    "Sorry, I had trouble forming that reply. Please say that once more.",
                    allow_interruptions=True,
                )
                await handle
            except Exception:
                logger.exception(
                    "VOICE_SESSION recovery speech failed call_id=%s",
                    userdata.call_id,
                )
            finally:
                recovery_active = False

        import asyncio

        asyncio.create_task(_speak_recovery())

    cross_channel_task: asyncio.Task[None] | None = None
    cross_channel_pubsub: Any = None

    def _session_running() -> bool:
        return getattr(session, "_activity", None) is not None

    def _handle_spoke(handle: Any) -> bool:
        for item in getattr(handle, "chat_items", None) or []:
            if getattr(item, "role", None) != "assistant":
                continue
            text = getattr(item, "text_content", None) or getattr(
                item, "raw_text_content", ""
            )
            if str(text or "").strip():
                return True
        return False

    async def _speak_pending_reply(summary: str) -> tuple[bool, str]:
        """Generate the spoken acknowledgement. Returns (spoke, failure reason)."""
        try:
            handle = session.generate_reply(
                user_input=summary,
                instructions=PROACTIVE_CONTENT_INSTRUCTIONS,
                tools=[],
                allow_interruptions=True,
            )
        except RuntimeError as exc:
            return False, f"rejected:{str(exc)[:120]}"
        await handle
        error = handle.exception()
        if error is not None:
            return False, f"error:{str(error)[:120]}"
        if _handle_spoke(handle):
            return True, ""
        return False, "interrupted" if handle.interrupted else "empty_reply"

    async def _deliver_pending_whatsapp() -> None:
        nonlocal pending_content_ready_at, recovery_active
        if not _session_running():
            logger.info(
                "CROSS_CHANNEL proactive skipped call_id=%s reason=session_not_running",
                userdata.call_id,
            )
            return
        pending = await pop_pending_cross_channel_input(
            business_id=userdata.business_id,
            is_owner=userdata.is_owner,
            customer_id=userdata.customer_id,
            sender_external_id=userdata.caller_phone,
            call_id=userdata.call_id,
        )
        if pending is None:
            return
        pending_content_ready_at = time.time()
        logger.info(
            "CROSS_CHANNEL proactive consume call_id=%s kind=%s",
            userdata.call_id,
            pending.get("type"),
        )
        summary = format_proactive_whatsapp_content(pending)
        # The content-specific fallback below replaces the generic LLM recovery line.
        recovery_active = True
        try:
            spoke, reason = await _speak_pending_reply(summary)
            if not spoke and reason != "interrupted":
                spoke, retry_reason = await _speak_pending_reply(summary)
                if not spoke:
                    reason = f"{reason}|{retry_reason}"
            if spoke:
                return
            if reason.endswith("interrupted"):
                logger.info(
                    "CROSS_CHANNEL proactive reply interrupted call_id=%s",
                    userdata.call_id,
                )
                return
            logger.error(
                "CROSS_CHANNEL proactive fallback call_id=%s reason=%s",
                userdata.call_id,
                reason,
            )
            try:
                await session.say(
                    spoken_whatsapp_fallback(pending),
                    allow_interruptions=True,
                )
            except Exception:
                logger.exception(
                    "CROSS_CHANNEL proactive fallback speech failed call_id=%s",
                    userdata.call_id,
                )
        finally:
            recovery_active = False

    async def _deliver_pending_safely() -> None:
        try:
            await _deliver_pending_whatsapp()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "CROSS_CHANNEL proactive delivery failed call_id=%s",
                userdata.call_id,
            )

    async def _cross_channel_consumer() -> None:
        nonlocal cross_channel_pubsub
        channel = pubsub_channel(userdata.call_id)
        try:
            redis = get_redis()
            cross_channel_pubsub = redis.pubsub()
            await cross_channel_pubsub.subscribe(channel)
            # Covers content buffered between call activation and subscription.
            await _deliver_pending_safely()
            async for message in cross_channel_pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if (
                    data.get("type") == "cross_channel_pending_content"
                    and data.get("voice_session_id") == userdata.call_id
                ):
                    await _deliver_pending_safely()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "CROSS_CHANNEL proactive consumer failed call_id=%s",
                userdata.call_id,
            )
        finally:
            if cross_channel_pubsub is not None:
                try:
                    await cross_channel_pubsub.unsubscribe(channel)
                    await cross_channel_pubsub.aclose()
                except Exception:
                    pass

    @session.on("close")
    def _on_session_close(_ev: Any) -> None:
        if cross_channel_task is not None:
            cross_channel_task.cancel()

    logger.info(
        "VOICE_SESSION start call_id=%s room=%s build=%s",
        userdata.call_id,
        getattr(getattr(ctx, "room", None), "name", ""),
        _build_fingerprint(),
    )
    try:
        from voice.telemetry import tag_voice_trace

        tag_voice_trace(call_id=userdata.call_id, business_id=userdata.business_id)
    except Exception:
        logger.debug("VOICE_OTEL session tag skipped", exc_info=True)
    bound_agent = BoundAgent()
    await session.start(agent=bound_agent, room=ctx.room)
    cross_channel_task = asyncio.create_task(_cross_channel_consumer())


async def _await_whatsapp_input(
    userdata: UserData,
    prompt_message: str,
    expected_content: str,
    livekit_room: str,
) -> str:
    result = await request_whatsapp_input(
        userdata,
        prompt_message,
        expected_content=expected_content,
        livekit_room=livekit_room,
    )
    return format_whatsapp_input_result(result)
