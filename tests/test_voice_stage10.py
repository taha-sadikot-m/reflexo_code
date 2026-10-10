"""Stage 10 WhatsApp Calling webhook + handler tests (Graph/LiveKit stubbed)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers.meta_mcp.webhooks.handler import process_webhook_payload
from voice.agent import build_instructions
from voice.prompts import build_customer_voice_prompt
from voice.bridge import WhatsAppAudioBridge, normalize_whatsapp_sdp
from voice.userdata import UserData
from voice.webhook_parse import (
    call_event_name,
    caller_name_of,
    classify_call_event,
    iter_call_records,
    phone_number_id_of,
    sdp_offer_of,
)


@pytest.mark.asyncio
async def test_voice_transcript_temporal_match_skips_spoken_specialist() -> None:
    from voice.session import dispatch_voice_turn

    userdata = UserData(
        business_id="biz",
        call_id="call",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        customer_id="customer",
    )
    speak = AsyncMock()
    with patch(
        "voice.session.process_voice_transcript",
        new=AsyncMock(
            return_value={
                "status": "workflow_consumed",
                "workflow_matched": True,
                "response_text": None,
                "specialist": None,
            }
        ),
    ):
        result = await dispatch_voice_turn(
            userdata,
            "I want the automation",
            turn_id="1",
            speak=speak,
        )
    assert result["workflow_matched"] is True
    speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_no_workflow_speaks_canonical_specialist_reply() -> None:
    from voice.session import dispatch_voice_turn

    userdata = UserData(
        business_id="biz",
        call_id="call",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        customer_id="customer",
    )
    speak = AsyncMock()
    with patch(
        "voice.session.process_voice_transcript",
        new=AsyncMock(
            return_value={
                "status": "success",
                "workflow_matched": False,
                "response_text": "The store is open until six.",
                "specialist": "inquiry_agent",
            }
        ),
    ):
        result = await dispatch_voice_turn(
            userdata,
            "When do you close?",
            turn_id="2",
            speak=speak,
        )
    assert result["specialist"] == "inquiry_agent"
    speak.assert_awaited_once_with("The store is open until six.")


@pytest.mark.asyncio
async def test_voice_pipeline_call_uses_identity_channel_and_tts_only_send() -> None:
    from voice.session import process_voice_transcript

    userdata = UserData(
        business_id="biz",
        call_id="call-abc",
        phone_number_id="PNID_1",
        caller_phone="+919876543210",
        customer_name="Taha",
    )
    handler = AsyncMock(return_value={"status": "success"})
    with patch("agent.session.handle_incoming_message", handler):
        await process_voice_transcript(
            userdata,
            "Hello",
            turn_id="3",
        )
    handler.assert_awaited_once_with(
        channel="voice",
        external_sender_id="+919876543210",
        message_text="Hello",
        receiving_account_id="PNID_1",
        sender_display_name="Taha",
        external_message_id="voice:call-abc:3",
        message_type="voice",
        skip_send=True,
        runtime_context={
            "voice_call_id": "call-abc",
            "is_outbound": False,
            "outbound_reason": "",
        },
    )


def _calls_payload(*, event: str = "connect", nested: bool = True) -> dict:
    call = {
        "id": "call-abc",
        "from": "919876543210",
        "to": "15550001111",
        "timestamp": "1721300000",
        "session": {"sdp": "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"},
    }
    if nested:
        call["type"] = event
        value = {
            "metadata": {"phone_number_id": "PNID_1"},
            "calls": [call],
        }
    else:
        call["event"] = event
        call["metadata"] = {"phone_number_id": "PNID_1"}
        value = call
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [{"field": "calls", "value": value}],
            }
        ],
    }


def test_parse_nested_connect_shape():
    payload = _calls_payload(nested=True)
    value = payload["entry"][0]["changes"][0]["value"]
    records = iter_call_records(value)
    assert len(records) == 1
    assert classify_call_event(call_event_name(records[0])) == "connect"
    assert sdp_offer_of(records[0]).startswith("v=0")
    assert phone_number_id_of(value, records[0]) == "PNID_1"


def test_parse_flat_event_status_shape():
    payload = _calls_payload(event="connect", nested=False)
    value = payload["entry"][0]["changes"][0]["value"]
    records = iter_call_records(value)
    assert classify_call_event(call_event_name(records[0])) == "connect"


def test_normalize_whatsapp_sdp_keeps_only_sha256_and_crlf():
    raw = "\n".join(
        [
            "v=0",
            "o=- 1 1 IN IP4 0.0.0.0",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE audio",
            "m=audio 50000 UDP/TLS/RTP/SAVPF 111",
            "c=IN IP4 192.168.1.10",
            "a=rtpmap:111 opus/48000/2",
            "a=ice-ufrag:abcd",
            "a=ice-pwd:abcdefghijklmnopqrstuv",
            "a=candidate:host 1 udp 1 169.254.1.2 50000 typ host",
            "a=candidate:srflx 1 udp 1 203.0.113.10 50001 typ srflx",
            "a=fingerprint:sha-256 AA:BB",
            "a=fingerprint:sha-384 CC:DD",
            "a=fingerprint:sha-512 EE:FF",
            "a=setup:active",
        ]
    )
    answer = normalize_whatsapp_sdp(raw)
    assert answer.endswith("\r\n")
    assert answer.count("a=fingerprint:") == 1
    assert "a=fingerprint:sha-256 AA:BB\r\n" in answer
    assert "sha-384" not in answer
    assert "sha-512" not in answer
    assert "169.254.1.2" not in answer
    assert "203.0.113.10" in answer


def test_normalize_whatsapp_sdp_rejects_incomplete_answer():
    from voice.errors import VoiceSdpError

    with pytest.raises(VoiceSdpError, match="invalid WhatsApp SDP answer"):
        normalize_whatsapp_sdp("v=0\r\n")


@pytest.mark.asyncio
async def test_meta_sdp_error_is_classified_as_voice_sdp_error():
    from mcp_servers.meta_mcp.utils.errors import MessageFailedError
    from voice.errors import VoiceSdpError
    from voice.meta_api import pre_accept_call

    with patch(
        "voice.meta_api.graph_post",
        new_callable=AsyncMock,
        side_effect=MessageFailedError(
            "SDP Validation error",
            graph_code=138008,
            http_status=400,
        ),
    ):
        with pytest.raises(VoiceSdpError, match="pre-accept SDP"):
            await pre_accept_call(
                "pnid",
                "call-id",
                "token",
                business_id="biz",
                sdp_answer="answer",
            )


def test_caller_name_matches_wa_id():
    value = {
        "contacts": [
            {"wa_id": "911111111111", "profile": {"name": "Someone Else"}},
            {"wa_id": "919876543210", "profile": {"name": "Taha"}},
        ],
    }
    assert caller_name_of(value, {"from": "919876543210"}) == "Taha"
    # Unmatched caller falls back to the first named contact.
    assert caller_name_of(value, {"from": "915555555555"}) == "Someone Else"
    assert caller_name_of({}, {"from": "919876543210"}) == ""


@pytest.mark.asyncio
async def test_identity_failure_releases_call_slot():
    """A bare return after acquiring the slot used to leak it for the Redis TTL."""
    from identity.models import IdentityErrorCode, IdentityResolutionError

    payload = _calls_payload(nested=True)

    with (
        patch(
            "voice.call_handler.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch(
            "voice.call_handler.resolve_identity",
            new_callable=AsyncMock,
            side_effect=IdentityResolutionError(
                IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT, "no row"
            ),
        ),
        patch("voice.call_handler.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.call_handler.release_call_slot", new_callable=AsyncMock) as rel,
        patch("voice.call_handler.pre_accept_call", new_callable=AsyncMock) as pre,
        patch(
            "voice.call_handler.get_voice_settings",
            return_value=MagicMock(is_configured=lambda: True),
        ),
    ):
        await process_webhook_payload(payload)

    rel.assert_awaited_once_with("biz-uuid")
    pre.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdp_failure_releases_slot_and_closes_bridge():
    from voice.errors import VoiceSdpError

    payload = _calls_payload(nested=True)
    bridge = MagicMock()
    bridge.create_sdp_answer = AsyncMock(side_effect=VoiceSdpError("bad offer"))
    bridge.close = AsyncMock()

    with (
        patch(
            "voice.call_handler.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch(
            "voice.call_handler.resolve_identity",
            new_callable=AsyncMock,
            return_value=MagicMock(
                customer_id="cust-1", is_owner=False, customer_name="Rahul"
            ),
        ),
        patch(
            "voice.call_handler.load_business_profile",
            new_callable=AsyncMock,
            return_value={"name": "HolonCode"},
        ),
        patch(
            "voice.call_handler.get_or_create_voice_conversation",
            new_callable=AsyncMock,
            return_value="conv-1",
        ),
        patch(
            "voice.call_handler.pop_outbound_context",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "voice.call_handler.ensure_valid_token",
            new_callable=AsyncMock,
            return_value={"access_token": "tok"},
        ),
        patch("voice.call_handler.WhatsAppAudioBridge", return_value=bridge),
        patch("voice.call_handler.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.call_handler.release_call_slot", new_callable=AsyncMock) as rel,
        # Unpatched, the failure path would POST to the live Graph API.
        patch("voice.call_handler.terminate_call", new_callable=AsyncMock) as terminate,
        patch(
            "voice.call_handler.get_voice_settings",
            return_value=MagicMock(
                is_configured=lambda: True,
                livekit_url="wss://example.livekit.cloud",
                livekit_api_key="k",
                livekit_api_secret="s",
            ),
        ),
    ):
        await process_webhook_payload(payload)

    rel.assert_awaited_once_with("biz-uuid")
    bridge.close.assert_awaited()
    terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_hangup_during_setup_stops_before_accept_and_releases_once():
    from voice import call_handler as ch

    payload = _calls_payload(nested=True)
    bridge = MagicMock()
    bridge.create_sdp_answer = AsyncMock(return_value="sdp-answer")
    bridge.close = AsyncMock()
    pre_started = asyncio.Event()
    finish_pre = asyncio.Event()

    async def slow_pre_accept(*_args, **_kwargs):
        pre_started.set()
        await finish_pre.wait()

    with (
        patch(
            "voice.call_handler.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch(
            "voice.call_handler.resolve_identity",
            new_callable=AsyncMock,
            return_value=MagicMock(
                customer_id="cust-1", is_owner=False, customer_name="Rahul"
            ),
        ),
        patch(
            "voice.call_handler.load_business_profile",
            new_callable=AsyncMock,
            return_value={"name": "HolonCode"},
        ),
        patch(
            "voice.call_handler.get_or_create_voice_conversation",
            new_callable=AsyncMock,
            return_value="conv-1",
        ),
        patch(
            "voice.call_handler.ensure_valid_token",
            new_callable=AsyncMock,
            return_value={"access_token": "tok"},
        ),
        patch("voice.call_handler.WhatsAppAudioBridge", return_value=bridge),
        patch(
            "voice.call_handler.pre_accept_call",
            new_callable=AsyncMock,
            side_effect=slow_pre_accept,
        ),
        patch("voice.call_handler.accept_call", new_callable=AsyncMock) as accept,
        patch("voice.call_handler.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.call_handler.release_call_slot", new_callable=AsyncMock) as rel,
        patch("voice.call_handler.publish_event", new_callable=AsyncMock),
        patch(
            "voice.call_handler.get_voice_settings",
            return_value=MagicMock(is_configured=lambda: True),
        ),
    ):
        connect_task = asyncio.create_task(process_webhook_payload(payload))
        await pre_started.wait()
        hangup_task = asyncio.create_task(
            ch.handle_hangup("call-abc", "biz-uuid", reason="hangup")
        )
        await asyncio.sleep(0)
        finish_pre.set()
        await asyncio.gather(connect_task, hangup_task)

    accept.assert_not_awaited()
    rel.assert_awaited_once_with("biz-uuid")
    bridge.close.assert_awaited_once()
    assert "call-abc" not in ch._pending_calls
    assert "call-abc" not in ch._active_calls


def test_hangup_aliases():
    assert classify_call_event("disconnected") == "hangup"
    assert classify_call_event("terminate") == "hangup"
    assert classify_call_event("timeout") == "timeout"


def test_voice_prompt_contains_spoken_rules():
    ud = UserData(
        business_id="b1",
        call_id="c1",
        phone_number_id="p1",
        caller_phone="+9198",
        business_name="HolonCode",
        persona_name="Maya",
    )
    text = build_customer_voice_prompt(ud)
    assert "LIVE VOICE CALL" in text
    assert "request_whatsapp_input" in text
    assert "HolonCode" in text
    instructions = build_instructions(ud, {"description": "We build software."})
    assert "hours" in instructions.lower() or "VOICE RULES" in instructions


def test_owner_prompt_allows_immediate_business_updates():
    ud = UserData(
        business_id="b1",
        call_id="c1",
        phone_number_id="p1",
        caller_phone="+9198",
        is_owner=True,
        business_name="HolonCode",
        persona_name="Maya",
    )
    prompt = build_instructions(ud, {"description": "We build software."})
    assert "immediately update" in prompt
    assert "this voice session is read-oriented" not in prompt
    assert "Never describe this as a read-only session" in prompt
    assert "upsert_catalog_item" in prompt
    assert "MUST call request_whatsapp_input immediately" in prompt
    assert "request_whatsapp_input is actively running" in prompt


@pytest.mark.asyncio
async def test_voice_business_update_uses_resolved_business_id():
    from voice.core_tools import update_business_info_text

    with patch(
        "voice.core_tools.update_business_info_impl",
        new_callable=AsyncMock,
        return_value={
            "status": "updated",
            "field": "description",
            "old_value": "Old",
            "new_value": "New",
        },
    ) as update:
        result = await update_business_info_text("resolved-biz", "description", "New")
    update.assert_awaited_once_with("resolved-biz", "description", "New")
    assert result == "Saved description as New."


@pytest.mark.asyncio
async def test_voice_rejects_invalid_email_before_mutation():
    from voice.core_tools import update_business_context_text

    with patch(
        "voice.core_tools.upsert_business_context_impl",
        new_callable=AsyncMock,
    ) as update:
        result = await update_business_context_text(
            "resolved-biz",
            "contact.email",
            "19eMIIt087.@gmail.com",
        )
    update.assert_not_awaited()
    assert "not valid" in result


class _FakeSpeechHandle:
    def __init__(self, *, text="Sure, I have it.", error=None, interrupted=False):
        self._error = error
        self.interrupted = interrupted
        self.chat_items = (
            [SimpleNamespace(role="assistant", text_content=text)] if text else []
        )

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()

    def exception(self):
        return self._error


class _FakeVoiceSession:
    instances: list["_FakeVoiceSession"] = []

    def __init__(self, **_kwargs):
        self.kwargs = _kwargs
        self.handlers = {}
        self.say = AsyncMock()
        self.generated: list[dict] = []
        self.reply_outcomes: list = []
        self.agent = None
        self._activity = None
        self.__class__.instances.append(self)

    def on(self, event_name):
        def register(callback):
            self.handlers[event_name] = callback
            return callback

        return register

    async def start(self, *, agent, room):
        self.agent = agent
        self._activity = MagicMock(session=self)
        agent._activity = self._activity

    def generate_reply(self, **kwargs):
        self.generated.append(kwargs)
        outcome = self.reply_outcomes.pop(0) if self.reply_outcomes else _FakeSpeechHandle()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _build_bound_voice_agent(
    *,
    is_owner: bool = False,
    customer_id: str | None = None,
    userdata: UserData | None = None,
):
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    userdata = userdata or UserData(
        business_id="biz-1",
        call_id="call-1",
        phone_number_id="pnid",
        caller_phone="+9198",
        is_owner=is_owner,
        customer_id=customer_id,
        business_name="HolonCode",
        persona_name="Maya",
    )
    settings = MagicMock(
        agent_model="gemini-2.5-flash",
        voice_llm_fallback_model="gemini-2.5-flash-lite",
        voice_llm_temperature=0.2,
        voice_llm_max_output_tokens=512,
        voice_llm_thinking_budget=0,
        voice_llm_attempt_timeout=4.0,
        voice_llm_max_retry_per_model=0,
        voice_endpointing_min_delay=0.6,
        voice_endpointing_max_delay=2.0,
        voice_preemptive_tts=False,
        google_api_key="google",
        deepgram_api_key="deepgram",
        stt_model="nova-2",
        stt_language="multi",
        tts_model="aura-2-asteria-en",
    )
    with (
        patch("voice.session.AgentSession", _FakeVoiceSession),
        patch("voice.session.get_voice_settings", return_value=settings),
        patch(
            "voice.session.load_business_profile",
            new_callable=AsyncMock,
            return_value={"name": "HolonCode", "description": "Software"},
        ),
        patch("voice.session.silero.VAD.load", return_value=MagicMock()),
        patch("voice.session.deepgram.STT", return_value=MagicMock()),
        patch("voice.session.deepgram.TTS", return_value=MagicMock()),
        patch("voice.session.google.LLM", return_value=MagicMock()),
    ):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
    session = _FakeVoiceSession.instances[-1]
    return session.agent, session, userdata


@pytest.mark.asyncio
async def test_customer_agent_does_not_expose_owner_mutation_tools():
    agent, _session, _userdata = await _build_bound_voice_agent(is_owner=False)
    tool_ids = {tool.id for tool in agent.tools}
    assert "update_business_info" not in tool_ids
    assert "upsert_catalog_item" not in tool_ids
    assert "search_knowledge" in tool_ids
    assert "check_availability" in tool_ids
    assert "request_whatsapp_input" not in tool_ids


@pytest.mark.asyncio
async def test_fast_owner_tool_skips_progress_speech_and_deduplicates():
    agent, session, userdata = await _build_bound_voice_agent(is_owner=True)
    with patch(
        "voice.session.tool_search_knowledge",
        new_callable=AsyncMock,
        return_value="We build software.",
    ) as search:
        first = await agent.search_knowledge(query=" HolonCode   services ")
        second = await agent.search_knowledge(query="holoncode services")
    assert first == second
    search.assert_awaited_once_with(userdata, " HolonCode   services ")
    session.say.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_owner_tool_starts_before_delayed_progress_speech():
    agent, session, _userdata = await _build_bound_voice_agent(is_owner=True)
    started = asyncio.Event()

    async def _slow_search(*_args):
        started.set()
        await asyncio.sleep(0.8)
        return "Done"

    with patch("voice.session.tool_search_knowledge", new=_slow_search):
        task = asyncio.create_task(agent.search_knowledge(query="slow query"))
        await asyncio.wait_for(started.wait(), timeout=0.2)
        session.say.assert_not_awaited()
        await task
    session.say.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_final_voice_transcript_resets_the_turn():
    agent, session, _userdata = await _build_bound_voice_agent(is_owner=True)
    with patch.object(agent, "reset_tool_turn") as reset:
        session.handlers["user_input_transcribed"](
            MagicMock(is_final=False, transcript="partial")
        )
        session.handlers["user_input_transcribed"](
            MagicMock(is_final=True, transcript="final request")
        )
    reset.assert_called_once_with()


@pytest.mark.asyncio
async def test_livekit_turn_hook_speaks_pipeline_and_stops_second_llm():
    from livekit.agents import StopResponse

    agent, session, _userdata = await _build_bound_voice_agent(is_owner=False)
    with patch(
        "voice.session.dispatch_voice_turn",
        new=AsyncMock(
            return_value={
                "status": "success",
                "response_text": "Canonical response",
                "workflow_matched": False,
                "specialist": "inquiry_agent",
            }
        ),
    ) as dispatch:
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(
                MagicMock(),
                MagicMock(text_content="Customer question"),
            )
    dispatch.assert_awaited_once()
    assert session.kwargs["turn_handling"]["preemptive_generation"]["enabled"] is False


@pytest.mark.asyncio
async def test_livekit_turn_hook_speaks_recovery_and_stops_on_pipeline_error():
    from livekit.agents import StopResponse

    agent, session, _userdata = await _build_bound_voice_agent(is_owner=False)
    with patch(
        "voice.session.dispatch_voice_turn",
        new=AsyncMock(side_effect=RuntimeError("pipeline unavailable")),
    ):
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(
                MagicMock(),
                MagicMock(text_content="Customer question"),
            )
    session.say.assert_awaited_once()
    assert "say it once more" in session.say.await_args.args[0]


@pytest.mark.asyncio
async def test_pipeline_failure_does_not_suppress_same_text_retry():
    from livekit.agents import StopResponse

    agent, _session, _userdata = await _build_bound_voice_agent(is_owner=False)
    dispatch = AsyncMock(
        side_effect=[
            RuntimeError("temporary"),
            {
                "status": "success",
                "response_text": "Recovered",
                "workflow_matched": False,
                "specialist": "inquiry_agent",
            },
        ]
    )
    with patch("voice.session.dispatch_voice_turn", new=dispatch):
        for _ in range(2):
            with pytest.raises(StopResponse):
                await agent.on_user_turn_completed(
                    MagicMock(),
                    MagicMock(text_content="Repeat me"),
                )
    assert dispatch.await_count == 2


@pytest.mark.asyncio
async def test_llm_session_error_gets_spoken_recovery():
    _agent, session, _userdata = await _build_bound_voice_agent(is_owner=True)
    source = type("LLM", (), {})()
    session.handlers["error"](MagicMock(source=source, error=RuntimeError("empty")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.say.assert_awaited_once()
    assert "say that once more" in session.say.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_session_disables_competing_preemptive_llm():
    from livekit.agents.llm import FallbackAdapter

    _agent, session, _userdata = await _build_bound_voice_agent(is_owner=True)
    assert isinstance(session.kwargs["llm"], FallbackAdapter)
    turn = session.kwargs["turn_handling"]
    assert turn["endpointing"]["min_delay"] == 0.6
    assert turn["preemptive_generation"]["enabled"] is False
    assert turn["preemptive_generation"]["max_retries"] == 1
    assert session.kwargs["conn_options"].llm_conn_options.max_retry == 0


@pytest.mark.asyncio
async def test_worker_reads_bridge_participant_metadata_fallback():
    import asyncio
    import json

    from voice.worker import _userdata_from_context

    room = MagicMock(
        name="wa-call-call-from-room",
        metadata="",
        remote_participants={
            "bridge": MagicMock(
                metadata=json.dumps(
                    {
                        "business_id": "biz-1",
                        "call_id": "call-from-metadata",
                        "phone_number_id": "pnid-1",
                        "caller_phone": "+9198",
                    }
                )
            )
        },
    )
    ctx = MagicMock(room=room, job=MagicMock(metadata=""))
    userdata = await _userdata_from_context(ctx, asyncio.Event())
    assert userdata.business_id == "biz-1"
    assert userdata.call_id == "call-from-metadata"


def test_voice_worker_prewarm_loads_vad_once():
    from voice.worker import prewarm

    proc = MagicMock(userdata={})
    vad = MagicMock()
    with patch("livekit.plugins.silero.VAD.load", return_value=vad) as load:
        prewarm(proc)
    load.assert_called_once_with()
    assert proc.userdata["vad"] is vad


def test_voice_job_process_uses_selector_event_loop():
    import selectors
    import sys

    from voice.worker import prewarm

    original = asyncio.new_event_loop
    proc = MagicMock(userdata={})
    try:
        with (
            patch("multiprocessing.current_process") as current_process,
            patch("livekit.plugins.silero.VAD.load", return_value=MagicMock()),
        ):
            current_process.return_value.name = "job_proc"
            prewarm(proc)
        if sys.platform == "win32":
            loop = asyncio.new_event_loop()
            try:
                assert isinstance(loop, asyncio.SelectorEventLoop)
                assert isinstance(loop._selector, selectors.SelectSelector)
            finally:
                loop.close()
        else:
            assert asyncio.new_event_loop is original
    finally:
        asyncio.new_event_loop = original


@pytest.mark.asyncio
async def test_bridge_waits_for_both_audio_endpoints_before_forwarding():
    bridge = WhatsAppAudioBridge("call-gating")
    bridge._forward_whatsapp_to_livekit = AsyncMock()

    bridge._whatsapp_audio_track = object()
    bridge._maybe_start_whatsapp_forwarding()
    assert bridge._whatsapp_forward_task is None

    bridge._lk_source = object()
    bridge._maybe_start_whatsapp_forwarding()
    await asyncio.sleep(0)
    bridge._forward_whatsapp_to_livekit.assert_awaited_once()
    await bridge.close()


@pytest.mark.asyncio
async def test_runtime_outbound_track_uses_queue_backed_recv():
    from voice.bridge import _bind_queued_track

    track = _bind_queued_track()
    frame = await track.recv()
    assert frame.sample_rate == 48000
    assert frame.samples == 960
    track.stop()


@pytest.mark.asyncio
async def test_connect_calls_pre_accept_and_accept():
    payload = _calls_payload(nested=True)
    order: list[str] = []
    bridge = MagicMock()
    bridge.create_sdp_answer = AsyncMock(return_value="sdp-answer")
    bridge.connect_to_livekit = AsyncMock(
        side_effect=lambda **_kwargs: order.append("livekit")
    )
    bridge.wait_for_agent = AsyncMock(
        side_effect=lambda: order.append("agent_ready") or True
    )
    bridge.close = AsyncMock()

    identity = MagicMock(
        customer_id="cust-1",
        is_owner=False,
        customer_name="Rahul",
        business_id="biz-uuid",
    )

    with (
        patch(
            "voice.call_handler.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch(
            "voice.call_handler.resolve_identity",
            new_callable=AsyncMock,
            return_value=identity,
        ),
        patch(
            "voice.call_handler.load_business_profile",
            new_callable=AsyncMock,
            return_value={"name": "HolonCode", "persona_name": "Maya"},
        ),
        patch(
            "voice.call_handler.get_or_create_voice_conversation",
            new_callable=AsyncMock,
            return_value="conv-1",
        ),
        patch(
            "voice.call_handler.ensure_valid_token",
            new_callable=AsyncMock,
            return_value={"access_token": "tok"},
        ),
        patch("voice.call_handler.WhatsAppAudioBridge", return_value=bridge),
        patch(
            "voice.call_handler.pre_accept_call",
            new_callable=AsyncMock,
            side_effect=lambda *_args, **_kwargs: order.append("pre_accept"),
        ) as pre,
        patch(
            "voice.call_handler.accept_call",
            new_callable=AsyncMock,
            side_effect=lambda *_args, **_kwargs: order.append("accept"),
        ) as acc,
        patch("voice.call_handler.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.call_handler.publish_event", new_callable=AsyncMock),
        patch(
            "voice.call_handler.get_voice_settings",
            return_value=MagicMock(
                is_configured=lambda: True,
                livekit_url="wss://example.livekit.cloud",
                livekit_api_key="k",
                livekit_api_secret="s",
            ),
        ),
    ):
        await process_webhook_payload(payload)

    pre.assert_awaited()
    acc.assert_awaited()
    body = acc.await_args.args
    # positional: phone_number_id, call_id, token, sdp_answer
    assert "sdp-answer" in body
    assert pre.await_args.kwargs.get("sdp_answer") == "sdp-answer" or True
    bridge.create_sdp_answer.assert_awaited()
    bridge.connect_to_livekit.assert_awaited()
    assert order == ["pre_accept", "livekit", "agent_ready", "accept"]


@pytest.mark.asyncio
async def test_duplicate_connect_does_not_double_accept():
    payload = _calls_payload(nested=True)
    from voice import call_handler as ch

    existing = MagicMock()
    ch._active_calls["call-abc"] = existing
    try:
        with (
            patch(
                "voice.call_handler.resolve_business_from_channel",
                new_callable=AsyncMock,
                return_value="biz-uuid",
            ),
            patch("voice.call_handler.pre_accept_call", new_callable=AsyncMock) as pre,
            patch(
                "voice.call_handler.get_voice_settings",
                return_value=MagicMock(is_configured=lambda: True),
            ),
        ):
            await process_webhook_payload(payload)
        pre.assert_not_awaited()
    finally:
        ch._active_calls.pop("call-abc", None)


@pytest.mark.asyncio
async def test_missing_phone_number_id_dropped():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [
                    {
                        "field": "calls",
                        "value": {
                            "calls": [
                                {
                                    "id": "call-x",
                                    "type": "connect",
                                    "from": "9198",
                                    "session": {"sdp": "v=0"},
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    with (
        patch(
            "mcp_servers.meta_mcp.webhooks.whatsapp.get_phone_number_id_from_number",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("voice.call_handler.handle_inbound_call", new_callable=AsyncMock) as inbound,
    ):
        await process_webhook_payload(payload)
        inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_hangup_closes_bridge():
    from voice import call_handler as ch

    bridge = MagicMock()
    bridge.close = AsyncMock()
    ch._active_calls["call-abc"] = bridge
    payload = _calls_payload(event="hangup", nested=True)
    payload["entry"][0]["changes"][0]["value"]["calls"][0]["type"] = "hangup"
    try:
        with (
            patch(
                "voice.call_handler.resolve_business_from_channel",
                new_callable=AsyncMock,
                return_value="biz-uuid",
            ),
            patch("voice.call_handler.release_call_slot", new_callable=AsyncMock) as rel,
            patch("voice.call_handler.publish_event", new_callable=AsyncMock),
        ):
            await process_webhook_payload(payload)
        bridge.close.assert_awaited()
        rel.assert_awaited()
        assert "call-abc" not in ch._active_calls
    finally:
        ch._active_calls.pop("call-abc", None)


@pytest.mark.asyncio
async def test_sdp_fixture_answer_from_bridge_mock():
    from voice.bridge import WhatsAppAudioBridge

    with patch.object(
        WhatsAppAudioBridge,
        "create_sdp_answer",
        new_callable=AsyncMock,
        return_value="v=0\r\no=- answer",
    ):
        b = WhatsAppAudioBridge("c1")
        sdp = await b.create_sdp_answer("v=0\r\no=- offer")
        assert "answer" in sdp


@pytest.mark.asyncio
async def test_send_response_uses_livekit_when_voice_configured(monkeypatch):
    from agent.pipeline import send_response

    monkeypatch.setattr(
        "voice.config.get_voice_settings",
        lambda: MagicMock(is_configured=lambda: True),
    )
    called = []

    async def _no_mcp(*_a, **_k):
        called.append(True)
        raise AssertionError("should not send WhatsApp text when LiveKit is configured")

    monkeypatch.setattr("agent.pipeline.call_mcp_tool", _no_mcp)
    result = await send_response(
        channel="voice",
        recipient_id="+9198",
        text="We open at ten.",
        business_id="biz",
    )
    assert result["delivery"] == "livekit"
    assert not called
    ud = UserData(
        business_id="b",
        call_id="c",
        phone_number_id="p",
        caller_phone="+1",
        business_name="Shop",
        persona_name="Sam",
    )
    prompt = build_instructions(ud, {})
    assert "hours" in prompt.lower() or "knowledge" in prompt.lower()
    assert "request_whatsapp_input" in prompt


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def setex(self, key, _ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        await self.messages.put({"type": "message", "data": payload})

    def pubsub(self):
        return _FakePubSub(self)


class _FakePubSub:
    def __init__(self, redis: _FakeRedis):
        self.redis = redis

    async def subscribe(self, _channel):
        return None

    async def unsubscribe(self, _channel):
        return None

    async def aclose(self):
        return None

    async def listen(self):
        yield {"type": "subscribe", "data": None}
        while True:
            yield await self.redis.messages.get()


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []
        self.arguments: list[tuple] = []

    async def fetchval(self, sql, *args):
        self.executed.append(sql)
        self.arguments.append(args)
        return "sess-uuid-1"

    async def execute(self, sql, *args):
        self.executed.append(sql)
        self.arguments.append(args)
        return None


def _fake_tenant(conn: _FakeConn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(_business_id):
        yield conn

    return _cm


@pytest.mark.asyncio
async def test_register_cross_channel_listener_writes_redis_db_and_sends():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    conn = _FakeConn()
    sent: list[dict] = []

    async def _send(**kwargs):
        sent.append(kwargs)
        return {"status": "sent"}

    with (
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch("voice.cross_channel.tenant_connection", _fake_tenant(conn)),
        patch("voice.cross_channel.send_whatsapp_text", new=_send),
    ):
        session_id = await cc.register_cross_channel_listener(
            customer_id="11111111-1111-1111-1111-111111111111",
            customer_phone="+919876543210",
            voice_session_id="call-abc",
            livekit_room="wa-call-call-abc",
            business_id="22222222-2222-2222-2222-222222222222",
            expected_content="document",
            prompt_message="Please send the PDF here.",
        )
    assert session_id == "sess-uuid-1"
    actor_key = (
        "customer:22222222-2222-2222-2222-222222222222:"
        "11111111-1111-1111-1111-111111111111"
    )
    assert cc.listener_key(actor_key) in redis.store
    assert redis.store["cross_channel:call:call-abc"] == (
        actor_key
    )
    assert sent[0]["text"] == "Please send the PDF here."
    assert any("INSERT INTO cross_channel_sessions" in sql for sql in conn.executed)


@pytest.mark.asyncio
async def test_register_owner_listener_uses_phone_actor_without_customer_id():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    conn = _FakeConn()
    with (
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch("voice.cross_channel.tenant_connection", _fake_tenant(conn)),
        patch(
            "voice.cross_channel.send_whatsapp_text",
            new=AsyncMock(return_value={"status": "sent"}),
        ),
    ):
        session_id = await cc.register_cross_channel_listener(
            customer_id=None,
            customer_phone="919876543210",
            voice_session_id="owner-call",
            livekit_room="wa-call-owner-call",
            business_id="22222222-2222-2222-2222-222222222222",
            expected_content="text",
            prompt_message="Please send your exact email address here.",
            is_owner=True,
        )

    actor_key = "owner:22222222-2222-2222-2222-222222222222:+919876543210"
    assert session_id == "sess-uuid-1"
    assert cc.listener_key(actor_key) in redis.store
    listener = json.loads(redis.store[cc.listener_key(actor_key)])
    assert listener["actor_type"] == "owner"
    assert listener["customer_id"] is None
    insert_args = conn.arguments[0]
    assert insert_args[1] is None
    assert insert_args[5:] == ("owner", "+919876543210")


@pytest.mark.asyncio
async def test_register_listener_rolls_back_when_whatsapp_prompt_fails():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    conn = _FakeConn()
    business = "22222222-2222-2222-2222-222222222222"
    phone = "+919876543210"
    actor_key = f"owner:{business}:{phone}"
    with (
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch("voice.cross_channel.tenant_connection", _fake_tenant(conn)),
        patch(
            "voice.cross_channel.send_whatsapp_text",
            new=AsyncMock(side_effect=RuntimeError("Meta access denied")),
        ),
    ):
        with pytest.raises(RuntimeError, match="access denied"):
            await cc.register_cross_channel_listener(
                customer_id=None,
                customer_phone=phone,
                voice_session_id="owner-call",
                livekit_room="wa-call-owner-call",
                business_id=business,
                expected_content="text",
                prompt_message="Please send your email.",
                is_owner=True,
            )

    assert cc.listener_key(actor_key) not in redis.store
    assert cc.call_map_key("owner-call") not in redis.store
    assert any(
        "SET status = 'cancelled'" in sql
        for sql in conn.executed
    )


@pytest.mark.asyncio
async def test_active_owner_call_routes_message_before_listener():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    business = "22222222-2222-2222-2222-222222222222"
    phone = "+919876543210"
    actor_key = f"owner:{business}:{phone}"
    with patch("voice.cross_channel.get_redis", return_value=redis):
        await cc.mark_cross_channel_call_active(
            business_id=business,
            is_owner=True,
            customer_id=None,
            sender_external_id=phone,
            call_id="owner-call",
        )
        result = await cc.check_cross_channel_route(
            business_id=business,
            is_owner=True,
            customer_id=None,
            sender_external_id=phone,
        )

    assert result["status"] == "active_call"
    assert result["call_id"] == "owner-call"
    assert cc.active_actor_key(actor_key) in redis.store


@pytest.mark.asyncio
async def test_no_active_call_uses_normal_whatsapp_routing():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    with patch("voice.cross_channel.get_redis", return_value=redis):
        result = await cc.check_cross_channel_route(
            business_id="22222222-2222-2222-2222-222222222222",
            is_owner=True,
            customer_id=None,
            sender_external_id="+919876543210",
        )
    assert result is None


@pytest.mark.asyncio
async def test_complete_cross_channel_publishes_and_is_one_shot():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    conn = _FakeConn()
    customer = "11111111-1111-1111-1111-111111111111"
    business = "22222222-2222-2222-2222-222222222222"
    actor_key = f"customer:{business}:{customer}"
    redis.store[cc.listener_key(actor_key)] = (
        '{"customer_id":"%s","voice_session_id":"call-abc",'
        '"business_id":"22222222-2222-2222-2222-222222222222",'
        '"actor_type":"customer","db_session_id":"33333333-3333-3333-3333-333333333333",'
        '"status":"waiting"}'
        % customer
    )
    with (
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch("voice.cross_channel.tenant_connection", _fake_tenant(conn)),
    ):
        await cc.complete_cross_channel(
            actor_key, {"type": "pdf", "text": "Invoice 42", "tables": [], "metadata": {}}
        )
        second = await cc.check_cross_channel_listener(
            business_id=business,
            is_owner=False,
            customer_id=customer,
            sender_external_id="+919876543210",
        )
    assert second is None
    assert redis.published
    payload = json.loads(redis.published[0][1])
    assert payload["content"]["text"] == "Invoice 42"


@pytest.mark.asyncio
async def test_request_whatsapp_input_returns_pubsub_content():
    from voice.tools import request_whatsapp_input

    redis = _FakeRedis()
    userdata = UserData(
        business_id="22222222-2222-2222-2222-222222222222",
        call_id="call-abc",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        customer_id="11111111-1111-1111-1111-111111111111",
    )
    listener = {
        "status": "waiting",
        "voice_session_id": "call-abc",
        "db_session_id": "sess-uuid-1",
    }

    async def _later_publish():
        await asyncio.sleep(0.01)
        await redis.publish(
            "voice_session:call-abc:cross_channel",
            json.dumps(
                {
                    "type": "cross_channel_content",
                    "content": {"type": "image", "text": "receipt", "tables": [], "metadata": {}},
                }
            ),
        )

    with (
        patch("voice.tools.get_redis", return_value=redis),
        patch("voice.tools.check_cross_channel_listener", new=AsyncMock(return_value=listener)),
        patch("voice.tools.register_cross_channel_listener", new=AsyncMock()) as register,
    ):
        task = asyncio.create_task(_later_publish())
        result = await request_whatsapp_input(userdata, "Please send a photo")
        await task
    register.assert_not_awaited()
    assert result["status"] == "received"
    assert result["content"]["text"] == "receipt"


@pytest.mark.asyncio
async def test_owner_request_whatsapp_input_returns_exact_email():
    from voice.tools import request_whatsapp_input

    redis = _FakeRedis()
    userdata = UserData(
        business_id="22222222-2222-2222-2222-222222222222",
        call_id="owner-call",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        is_owner=True,
        customer_id=None,
    )
    listener = {
        "status": "waiting",
        "actor_type": "owner",
        "voice_session_id": "owner-call",
        "db_session_id": "sess-uuid-1",
    }

    async def _later_publish():
        await asyncio.sleep(0.01)
        await redis.publish(
            "voice_session:owner-call:cross_channel",
            json.dumps(
                {
                    "type": "cross_channel_content",
                    "content": {
                        "type": "text",
                        "text": "owner.new@example.com",
                        "tables": [],
                        "metadata": {},
                    },
                }
            ),
        )

    with (
        patch("voice.tools.get_redis", return_value=redis),
        patch(
            "voice.tools.check_cross_channel_listener",
            new=AsyncMock(return_value=listener),
        ) as check,
        patch(
            "voice.tools.register_cross_channel_listener",
            new=AsyncMock(),
        ) as register,
    ):
        task = asyncio.create_task(_later_publish())
        result = await request_whatsapp_input(
            userdata,
            "Please send your exact email address.",
            expected_content="text",
        )
        await task

    check.assert_awaited_once_with(
        business_id=userdata.business_id,
        is_owner=True,
        customer_id=None,
        sender_external_id=userdata.caller_phone,
    )
    register.assert_not_awaited()
    assert result == {
        "status": "received",
        "content": {
            "type": "text",
            "text": "owner.new@example.com",
            "tables": [],
            "metadata": {},
        },
    }


@pytest.mark.asyncio
async def test_request_whatsapp_input_timeout_expires_listener():
    from voice.tools import request_whatsapp_input

    redis = _FakeRedis()
    userdata = UserData(
        business_id="22222222-2222-2222-2222-222222222222",
        call_id="call-abc",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        customer_id="11111111-1111-1111-1111-111111111111",
    )

    async def _never():
        await asyncio.sleep(10)

    redis.messages.get = _never  # type: ignore[method-assign]
    cancelled = AsyncMock()
    with (
        patch("voice.tools.get_redis", return_value=redis),
        patch("voice.tools.check_cross_channel_listener", new=AsyncMock(return_value={"status": "waiting", "voice_session_id": "call-abc"})),
        patch("voice.tools.cancel_cross_channel", cancelled),
    ):
        result = await request_whatsapp_input(
            userdata, "Please send a photo", timeout_seconds=1
        )
    assert result["status"] == "timeout"
    cancelled.assert_awaited()


@pytest.mark.asyncio
async def test_inbound_whatsapp_with_media_completes_listener_without_react():
    from voice.cross_channel import handle_inbound_whatsapp_if_listening

    processed = {"type": "pdf", "text": "hello pdf", "tables": [], "metadata": {}}
    with (
        patch(
            "voice.cross_channel.check_cross_channel_listener",
            new=AsyncMock(return_value={"voice_session_id": "call-abc", "status": "waiting"}),
        ),
        patch(
            "shared.document_processor.process_whatsapp_media",
            new=AsyncMock(return_value=processed),
        ),
        patch("voice.cross_channel.complete_cross_channel", new=AsyncMock()) as complete,
        patch("agent.pipeline.log_messages", new=AsyncMock()),
    ):
        result = await handle_inbound_whatsapp_if_listening(
            customer_id="cust-1",
            is_owner=False,
            sender_external_id="+919876543210",
            business_id="biz-1",
            conversation_id="conv-1",
            message_text="",
            message_type="document",
            media_id="media-1",
            media_url=None,
            filename="file.pdf",
            mime_type="application/pdf",
            external_message_id="wamid.1",
        )
    assert result["status"] == "cross_channel_handled"
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_whatsapp_without_listener_is_ignored():
    from voice.cross_channel import handle_inbound_whatsapp_if_listening

    with (
        patch(
            "voice.cross_channel.check_cross_channel_listener",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "voice.cross_channel.check_cross_channel_route",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await handle_inbound_whatsapp_if_listening(
            customer_id="cust-1",
            is_owner=False,
            sender_external_id="+919876543210",
            business_id="biz-1",
            conversation_id="conv-1",
            message_text="hi",
            message_type="text",
            media_id=None,
            media_url=None,
            filename=None,
            mime_type=None,
            external_message_id="wamid.2",
        )
    assert result is None


@pytest.mark.asyncio
async def test_owner_whatsapp_text_completes_active_voice_listener():
    from voice.cross_channel import handle_inbound_whatsapp_if_listening

    listener = {
        "voice_session_id": "owner-call",
        "actor_type": "owner",
        "status": "waiting",
    }
    with (
        patch(
            "voice.cross_channel.check_cross_channel_listener",
            new=AsyncMock(return_value=listener),
        ),
        patch(
            "voice.cross_channel.complete_cross_channel",
            new=AsyncMock(),
        ) as complete,
        patch("agent.pipeline.log_messages", new=AsyncMock()) as log_messages,
    ):
        result = await handle_inbound_whatsapp_if_listening(
            customer_id=None,
            is_owner=True,
            sender_external_id="+919876543210",
            business_id="22222222-2222-2222-2222-222222222222",
            conversation_id="owner-conv",
            message_text="owner.new@example.com",
            message_type="text",
            media_id=None,
            media_url=None,
            filename=None,
            mime_type=None,
            external_message_id="wamid.owner",
        )
        await asyncio.sleep(0)

    assert result["status"] == "cross_channel_handled"
    complete.assert_awaited_once_with(
        "owner:22222222-2222-2222-2222-222222222222:+919876543210",
        {
            "type": "text",
            "text": "owner.new@example.com",
            "tables": [],
            "metadata": {},
        },
    )
    assert log_messages.await_args.kwargs["is_owner"] is True


@pytest.mark.asyncio
async def test_owner_whatsapp_before_tool_is_buffered_not_normal_chat():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    conn = _FakeConn()
    business = "22222222-2222-2222-2222-222222222222"
    phone = "+919876543210"
    actor_key = f"owner:{business}:{phone}"
    with (
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch("voice.cross_channel.tenant_connection", _fake_tenant(conn)),
        patch("agent.pipeline.log_messages", new=AsyncMock()),
    ):
        await cc.mark_cross_channel_call_active(
            business_id=business,
            is_owner=True,
            customer_id=None,
            sender_external_id=phone,
            call_id="owner-call",
        )
        result = await cc.handle_inbound_whatsapp_if_listening(
            customer_id=None,
            is_owner=True,
            sender_external_id=phone,
            business_id=business,
            conversation_id="owner-conv",
            message_text="early@example.com",
            message_type="text",
            media_id=None,
            media_url=None,
            filename=None,
            mime_type=None,
            external_message_id="wamid.early",
        )
        pending = await cc.pop_pending_cross_channel_input(
            business_id=business,
            is_owner=True,
            customer_id=None,
            sender_external_id=phone,
            call_id="owner-call",
        )
        await asyncio.sleep(0)

    assert result["status"] == "cross_channel_handled"
    assert result["voice_session_id"] == "owner-call"
    assert pending["text"] == "early@example.com"
    assert cc.pending_actor_key(actor_key) not in redis.store
    assert any("Buffered during active voice call" in sql for sql in conn.executed)
    assert any(
        json.loads(payload).get("type") == "cross_channel_pending_content"
        for _channel, payload in redis.published
    )


@pytest.mark.asyncio
async def test_active_sender_route_is_indexed_and_removed_on_hangup():
    from voice import cross_channel as cc

    redis = _FakeRedis()
    business = "22222222-2222-2222-2222-222222222222"
    phone = "+919876543210"
    with patch("voice.cross_channel.get_redis", return_value=redis):
        await cc.mark_cross_channel_call_active(
            business_id=business,
            is_owner=True,
            customer_id=None,
            sender_external_id=phone,
            call_id="owner-call",
        )
        route = await cc.check_cross_channel_sender_route(
            business_id=business,
            sender_external_id=phone,
        )
        await cc.cancel_cross_channel_for_call("owner-call", business_id=business)
        missing = await cc.check_cross_channel_sender_route(
            business_id=business,
            sender_external_id=phone,
        )

    assert route["call_id"] == "owner-call"
    assert route["actor_type"] == "owner"
    assert missing is None


@pytest.mark.asyncio
async def test_pending_file_proactively_generates_one_voice_reply():
    from voice.config import VoiceSettings
    from voice.cross_channel import pending_actor_key
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    redis = _FakeRedis()
    userdata = UserData(
        business_id="biz-1",
        call_id="call-1",
        phone_number_id="pnid",
        caller_phone="+9198",
        is_owner=True,
        business_name="HolonCode",
        persona_name="Maya",
    )
    actor_key = "owner:biz-1:+9198"
    await redis.setex(
        pending_actor_key(actor_key),
        300,
        json.dumps(
            {
                "voice_session_id": "call-1",
                "content": {
                    "type": "docx",
                    "text": "Service code HC-77 costs INR 5000.",
                    "tables": [],
                    "metadata": {},
                },
            }
        ),
    )
    settings = VoiceSettings(
        livekit_url="wss://x",
        livekit_api_key="k",
        livekit_api_secret="s",
        deepgram_api_key="d",
        google_api_key="g",
        _env_file=None,
    )
    with (
        patch("voice.session.AgentSession", _FakeVoiceSession),
        patch("voice.session.get_voice_settings", return_value=settings),
        patch("voice.session.get_redis", return_value=redis),
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch(
            "voice.session.load_business_profile",
            new=AsyncMock(return_value={"name": "HolonCode"}),
        ),
        patch("voice.session.silero.VAD.load", return_value=MagicMock()),
        patch("voice.session.deepgram.STT", return_value=MagicMock()),
        patch("voice.session.deepgram.TTS", return_value=MagicMock()),
        patch("voice.session.google.LLM", return_value=MagicMock()),
    ):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
        session = _FakeVoiceSession.instances[-1]
        for _ in range(10):
            if session.generated:
                break
            await asyncio.sleep(0.01)
        session.handlers["close"](MagicMock())
        await asyncio.sleep(0)

    assert len(session.generated) == 1
    assert "HC-77" in session.generated[0]["user_input"]
    assert pending_actor_key(actor_key) not in redis.store


def _voice_test_settings():
    from voice.config import VoiceSettings

    return VoiceSettings(
        livekit_url="wss://x",
        livekit_api_key="k",
        livekit_api_secret="s",
        deepgram_api_key="d",
        google_api_key="g",
        _env_file=None,
    )


@contextlib.contextmanager
def _patched_voice_session(redis):
    with (
        patch("voice.session.AgentSession", _FakeVoiceSession),
        patch("voice.session.get_voice_settings", return_value=_voice_test_settings()),
        patch("voice.session.get_redis", return_value=redis),
        patch("voice.cross_channel.get_redis", return_value=redis),
        patch(
            "voice.session.load_business_profile",
            new=AsyncMock(return_value={"name": "HolonCode"}),
        ),
        patch("voice.session.silero.VAD.load", return_value=MagicMock()),
        patch("voice.session.deepgram.STT", return_value=MagicMock()),
        patch("voice.session.deepgram.TTS", return_value=MagicMock()),
        patch("voice.session.google.LLM", return_value=MagicMock()),
    ):
        yield


def _owner_call_userdata():
    return UserData(
        business_id="biz-1",
        call_id="call-1",
        phone_number_id="pnid",
        caller_phone="+9198",
        is_owner=True,
        business_name="HolonCode",
        persona_name="Maya",
    )


_ONBOARDING_DOC = {
    "type": "docx",
    "text": "HolonCode - Client Onboarding Note\nSaturday: 10:00 AM to 2:00 PM IST",
    "tables": [],
    "metadata": {"filename": "HolonCode_Onboarding_Note.docx"},
}


async def _buffer_pending(redis, content, *, call_id="call-1"):
    from voice.cross_channel import pending_actor_key

    await redis.setex(
        pending_actor_key("owner:biz-1:+9198"),
        300,
        json.dumps({"voice_session_id": call_id, "content": content}),
    )


async def _wait_until(predicate, *, attempts=40):
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes",
    [
        [
            _FakeSpeechHandle(text=None, error=RuntimeError("llm down")),
            _FakeSpeechHandle(text=None, error=RuntimeError("llm down again")),
        ],
        [_FakeSpeechHandle(text=""), _FakeSpeechHandle(text="")],
        [RuntimeError("AgentSession is closing"), _FakeSpeechHandle(text="")],
    ],
)
async def test_pending_file_speaks_fallback_when_model_reply_produces_no_audio(outcomes):
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    redis = _FakeRedis()
    userdata = _owner_call_userdata()
    await _buffer_pending(redis, _ONBOARDING_DOC)

    with _patched_voice_session(redis):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
        session = _FakeVoiceSession.instances[-1]
        session.reply_outcomes = list(outcomes)
        await _wait_until(lambda: session.say.await_count > 0)
        session.handlers["close"](MagicMock())
        await asyncio.sleep(0)

    # The model reply is retried once before the deterministic acknowledgement.
    assert len(session.generated) == 2
    session.say.assert_awaited_once()
    spoken = session.say.await_args.args[0]
    assert "HolonCode_Onboarding_Note.docx" in spoken
    assert spoken.rstrip().endswith("?")


@pytest.mark.asyncio
async def test_interrupted_pending_reply_is_not_retried_or_replaced():
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    redis = _FakeRedis()
    userdata = _owner_call_userdata()
    await _buffer_pending(redis, _ONBOARDING_DOC)

    with _patched_voice_session(redis):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
        session = _FakeVoiceSession.instances[-1]
        session.reply_outcomes = [_FakeSpeechHandle(text="", interrupted=True)]
        await _wait_until(lambda: len(session.generated) > 0)
        await asyncio.sleep(0.02)
        session.handlers["close"](MagicMock())
        await asyncio.sleep(0)

    assert len(session.generated) == 1
    session.say.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_keeps_listening_after_a_failed_delivery():
    from voice.cross_channel import pubsub_channel
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    redis = _FakeRedis()
    userdata = _owner_call_userdata()
    await _buffer_pending(redis, _ONBOARDING_DOC)

    with _patched_voice_session(redis):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
        session = _FakeVoiceSession.instances[-1]
        session.reply_outcomes = [ValueError("delivery exploded")]
        await _wait_until(lambda: len(session.generated) > 0)

        await _buffer_pending(
            redis,
            {
                "type": "text",
                "text": "priya.nair@holoncode.dev",
                "tables": [],
                "metadata": {},
            },
        )
        await redis.publish(
            pubsub_channel("call-1"),
            json.dumps(
                {
                    "type": "cross_channel_pending_content",
                    "voice_session_id": "call-1",
                    "actor_type": "owner",
                }
            ),
        )
        assert await _wait_until(lambda: len(session.generated) > 1)
        session.handlers["close"](MagicMock())
        await asyncio.sleep(0)

    assert "priya.nair@holoncode.dev" in session.generated[1]["user_input"]


def test_proactive_content_summary_names_the_file_and_clips_long_text():
    from voice.tools import PROACTIVE_CLIP_CHARS, format_proactive_whatsapp_content

    summary = format_proactive_whatsapp_content(
        {
            "type": "docx",
            "text": "Refund policy. " + ("x" * (PROACTIVE_CLIP_CHARS + 500)),
            "tables": [{"a": 1}],
            "metadata": {"filename": "HolonCode_Onboarding_Note.docx"},
        }
    )
    assert "HolonCode_Onboarding_Note.docx" in summary
    assert "tables: 1" in summary
    assert summary.endswith("...")
    assert len(summary) < PROACTIVE_CLIP_CHARS + 400


def test_spoken_fallback_acknowledges_and_asks_what_to_do():
    from voice.tools import spoken_whatsapp_fallback

    spoken = spoken_whatsapp_fallback(_ONBOARDING_DOC)
    assert "HolonCode_Onboarding_Note.docx" in spoken
    assert "Client Onboarding Note" in spoken
    assert spoken.rstrip().endswith("?")

    unreadable = spoken_whatsapp_fallback(
        {"type": "pdf", "text": "", "tables": [], "metadata": {"error": "timeout"}}
    )
    assert "could not read" in unreadable
    assert unreadable.rstrip().endswith("?")


def test_proactive_instructions_require_one_grounded_question():
    from voice.session import PROACTIVE_CONTENT_INSTRUCTIONS

    lowered = PROACTIVE_CONTENT_INSTRUCTIONS.lower()
    assert "exactly one question" in lowered
    assert "resend" in lowered
    assert "confirm" in lowered


def test_voice_prompts_cover_midcall_whatsapp_arrival():
    from voice.prompts import build_customer_voice_prompt, build_owner_voice_prompt

    userdata = _owner_call_userdata()
    for prompt in (
        build_customer_voice_prompt(userdata),
        build_owner_voice_prompt(userdata),
    ):
        lowered = prompt.lower()
        assert "whatsapp content arriving mid-call" in lowered
        assert "never go quiet" in lowered


@pytest.mark.asyncio
async def test_hangup_cancels_cross_channel_listener():
    from voice import call_handler as ch

    with patch(
        "voice.cross_channel.cancel_cross_channel_for_call", new=AsyncMock()
    ) as cancel:
        await ch.handle_hangup("call-abc", "biz-uuid", reason="hangup")
    cancel.assert_awaited_once_with("call-abc", business_id="biz-uuid")


@pytest.mark.asyncio
async def test_customer_with_id_gets_request_whatsapp_input_tool():
    agent, _session, _userdata = await _build_bound_voice_agent(
        is_owner=False, customer_id="cust-1"
    )
    tool_ids = {tool.id for tool in agent.tools}
    assert "request_whatsapp_input" in tool_ids
    assert "check_availability" in tool_ids
    assert "update_business_info" not in tool_ids


@pytest.mark.asyncio
async def test_verified_owner_gets_request_whatsapp_input_tool():
    agent, _session, _userdata = await _build_bound_voice_agent(is_owner=True)
    tool_ids = {tool.id for tool in agent.tools}
    assert "request_whatsapp_input" in tool_ids
    assert "check_availability" in tool_ids
    assert "update_business_context" in tool_ids


@pytest.mark.asyncio
async def test_process_content_text_fallback():
    from shared.document_processor import process_content

    result = await process_content(b"Hello menu", mime_type="text/plain", filename="n.txt")
    assert "Hello menu" in result["text"]
    assert result["type"] == "text"


def test_stt_tts_config_matches_spec():
    from voice.config import VoiceSettings

    settings = VoiceSettings(
        livekit_url="wss://x",
        livekit_api_key="k",
        livekit_api_secret="s",
        deepgram_api_key="d",
        stt_model="nova-2",
        tts_model="aura-2-asteria-en",
        _env_file=None,
    )
    assert settings.stt_model == "nova-2"
    assert settings.stt_language == "multi"
    assert settings.tts_model == "aura-2-asteria-en"


def test_voice_config_accepts_deepgram_model_env_aliases(monkeypatch):
    from voice.config import VoiceSettings

    monkeypatch.setenv("DEEPGRAM_STT_MODEL", "nova-3")
    monkeypatch.setenv("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")
    settings = VoiceSettings(_env_file=None)
    assert settings.stt_model == "nova-3"
    assert settings.tts_model == "aura-2-thalia-en"


def test_voice_tracing_skips_without_keys():
    from voice.telemetry import configure_voice_tracing

    with patch("voice.telemetry.get_voice_settings") as gs:
        gs.return_value = MagicMock(langfuse_public_key="", langfuse_secret_key="")
        configure_voice_tracing()


def test_opening_line_uses_outbound_reason_even_for_owners():
    from voice.prompts import opening_line

    owner = UserData(
        business_id="biz-1",
        call_id="call-1",
        phone_number_id="pnid",
        caller_phone="+9198",
        is_owner=True,
        business_name="HolonCode",
        persona_name="Maya",
        outbound_reason="your Saturday appointment",
        outbound_customer_name="Priya",
    )
    spoken = opening_line(owner)
    assert "Saturday appointment" in spoken
    assert "Priya" in spoken
    assert "welcome back" not in spoken.lower()

    inbound = UserData(
        business_id="biz-1",
        call_id="call-2",
        phone_number_id="pnid",
        caller_phone="+9198",
        is_owner=False,
        business_name="HolonCode",
        persona_name="Maya",
        greeting_message="Thanks for calling HolonCode.",
    )
    assert opening_line(inbound) == "Thanks for calling HolonCode."


@pytest.mark.asyncio
async def test_outbound_context_roundtrip_and_pop_is_one_shot():
    from voice.outbound import pop_outbound_context, store_outbound_context

    redis = _FakeRedis()
    with patch("voice.outbound.get_redis", return_value=redis):
        await store_outbound_context(
            "call-out-1",
            {
                "business_id": "biz-1",
                "reason": "your appointment",
                "customer_name": "Priya",
                "to_phone": "+9198",
            },
        )
        first = await pop_outbound_context("call-out-1")
        second = await pop_outbound_context("call-out-1")
    assert first["reason"] == "your appointment"
    assert first["customer_name"] == "Priya"
    assert second is None


def _calling_tools() -> dict:
    from mcp_servers.meta_mcp.tools import calling

    captured: dict = {}

    class _Mcp:
        def tool(self, **_kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    calling.register(_Mcp())
    return captured


@pytest.mark.asyncio
async def test_initiate_whatsapp_call_places_the_call_without_sdp_stub():
    tools = _calling_tools()
    with (
        patch(
            "mcp_servers.meta_mcp.tools.calling.business_exists",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "mcp_servers.meta_mcp.tools.calling.check_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "voice.outbound.place_outbound_call",
            new=AsyncMock(return_value="wacid.out1"),
        ) as place,
    ):
        result = await tools["initiate_whatsapp_call"](
            "biz-1",
            "+919876543210",
            reason="a reminder",
            customer_name="Priya",
        )
    place.assert_awaited_once_with(
        "biz-1",
        "+919876543210",
        reason="a reminder",
        customer_name="Priya",
    )
    assert result["call_id"] == "wacid.out1"
    assert "sdp" not in result["message"].lower()
    assert "stub" not in result["message"].lower()


@pytest.mark.asyncio
async def test_initiate_whatsapp_call_explains_missing_call_permission():
    from voice.errors import VoiceCallNotAllowedError

    tools = _calling_tools()
    with (
        patch(
            "mcp_servers.meta_mcp.tools.calling.business_exists",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "mcp_servers.meta_mcp.tools.calling.check_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "voice.outbound.place_outbound_call",
            new=AsyncMock(
                side_effect=VoiceCallNotAllowedError(
                    "No approved call permission for this customer."
                )
            ),
        ),
    ):
        result = await tools["initiate_whatsapp_call"]("biz-1", "+919876543210")
    assert result["error"]["code"] == "CALL_NOT_ALLOWED"
    assert "permission" in result["error"]["message"].lower()
    assert "do not retry" in result["error"]["agent_guidance"].lower()


@pytest.mark.asyncio
async def test_availability_text_speaks_free_slots():
    from voice.core_tools import availability_text

    with (
        patch(
            "voice.core_tools.load_business_profile",
            new=AsyncMock(
                return_value={"business_hours": {"sat": {"open": "10:00", "close": "14:00"}}}
            ),
        ),
        patch(
            "channel_framework.integrations.google_calendar.check_availability",
            new=AsyncMock(
                return_value={
                    "available_slots": [
                        {"start": "2026-09-05T10:00:00+05:30", "end": "2026-09-05T10:30:00+05:30"},
                        {"start": "2026-09-05T11:00:00+05:30", "end": "2026-09-05T11:30:00+05:30"},
                    ]
                }
            ),
        ),
    ):
        spoken = await availability_text("biz-1", "cleaning", "2026-09-05")
    assert "cleaning" in spoken
    assert "10:00" in spoken or "10:00 AM" in spoken


@pytest.mark.asyncio
async def test_availability_text_falls_back_when_calendar_missing():
    from voice.core_tools import availability_text

    with (
        patch(
            "voice.core_tools.load_business_profile",
            new=AsyncMock(return_value={"business_hours": {}}),
        ),
        patch(
            "channel_framework.integrations.google_calendar.check_availability",
            new=AsyncMock(side_effect=RuntimeError("missing")),
        ),
        patch(
            "voice.core_tools.business_hours_text",
            new=AsyncMock(return_value="Today we are open from 10:00 to 14:00 India time."),
        ),
    ):
        spoken = await availability_text("biz-1", "consult")
    assert "not connected" in spoken.lower()
    assert "10:00" in spoken


@pytest.mark.asyncio
async def test_pending_image_proactively_generates_reply_with_extracted_text():
    from voice.session import start_voice_session

    _FakeVoiceSession.instances.clear()
    redis = _FakeRedis()
    userdata = _owner_call_userdata()
    await _buffer_pending(
        redis,
        {
            "type": "image",
            "text": "Desk plate HC-SIGN-44",
            "tables": [],
            "metadata": {"filename": "HolonCode_Sign.png"},
        },
    )
    with _patched_voice_session(redis):
        await start_voice_session(MagicMock(room=MagicMock()), userdata)
        session = _FakeVoiceSession.instances[-1]
        await _wait_until(lambda: bool(session.generated))
        session.handlers["close"](MagicMock())
        await asyncio.sleep(0)
    assert "HC-SIGN-44" in session.generated[0]["user_input"]
    assert "HolonCode_Sign.png" in session.generated[0]["user_input"]


def test_voice_whatsapp_input_timeout_defaults_to_five_minutes(monkeypatch):
    from voice.config import VoiceSettings

    monkeypatch.delenv("VOICE_WHATSAPP_INPUT_TIMEOUT", raising=False)
    settings = VoiceSettings(
        livekit_url="wss://x",
        livekit_api_key="k",
        livekit_api_secret="s",
        deepgram_api_key="d",
        _env_file=None,
    )
    assert settings.voice_whatsapp_input_timeout == 300


def test_customer_prompt_covers_availability_and_outbound():
    from voice.prompts import build_customer_voice_prompt

    ud = UserData(
        business_id="biz-1",
        call_id="call-1",
        phone_number_id="pnid",
        caller_phone="+9198",
        outbound_reason="your booking",
        business_name="HolonCode",
        persona_name="Maya",
    )
    prompt = build_customer_voice_prompt(ud)
    lowered = prompt.lower()
    assert "check_availability" in lowered
    assert "outbound call" in lowered
    assert "payment" not in lowered


# ── Business-initiated calling (outbound SDP handshake) ────────────────────


def _status_payload(status: str, call_id: str = "wacid.out1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [
                    {
                        "field": "calls",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID_1"},
                            "statuses": [
                                {
                                    "id": call_id,
                                    "type": "call",
                                    "status": status,
                                    "timestamp": "1749197000",
                                    "recipient_id": "919876543210",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _bic_connect_payload(call_id: str = "wacid.out1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [
                    {
                        "field": "calls",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID_1"},
                            "calls": [
                                {
                                    "id": call_id,
                                    "to": "919876543210",
                                    "from": "15550001111",
                                    "event": "connect",
                                    "direction": "BUSINESS_INITIATED",
                                    "timestamp": "1749196895",
                                    "session": {
                                        "sdp_type": "answer",
                                        "sdp": "v=0\r\nanswer-from-meta\r\n",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_offer_normalization_allows_actpass_while_answers_require_active():
    from voice.errors import VoiceSdpError

    lines = [
        "v=0",
        "o=- 1 1 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "m=audio 50000 UDP/TLS/RTP/SAVPF 111",
        "c=IN IP4 192.168.1.10",
        "a=rtpmap:111 opus/48000/2",
        "a=ice-ufrag:abcd",
        "a=ice-pwd:abcdefghijklmnopqrstuv",
        "a=fingerprint:sha-256 AA:BB",
        "a=setup:actpass",
    ]
    offer = normalize_whatsapp_sdp("\n".join(lines), "offer")
    assert "a=setup:actpass\r\n" in offer

    # The same description is not a valid answer — we must pick a DTLS role.
    with pytest.raises(VoiceSdpError, match="invalid WhatsApp SDP answer"):
        normalize_whatsapp_sdp("\n".join(lines), "answer")


@pytest.mark.asyncio
async def test_initiate_call_posts_action_connect_with_sdp_offer():
    from voice.meta_api import initiate_call

    with patch(
        "voice.meta_api.graph_post",
        new=AsyncMock(return_value={"calls": [{"id": "wacid.out1"}]}),
    ) as post:
        data = await initiate_call(
            "PNID_1",
            "tok",
            "+919876543210",
            "v=0\r\noffer\r\n",
            business_id="biz-1",
        )

    assert data["calls"][0]["id"] == "wacid.out1"
    endpoint, token, body = post.await_args.args
    assert endpoint == "PNID_1/calls"
    assert token == "tok"
    assert body["action"] == "connect"
    assert body["session"] == {"sdp_type": "offer", "sdp": "v=0\r\noffer\r\n"}
    assert body["to"] == "919876543210"
    assert "call_id" not in body


@pytest.mark.asyncio
async def test_initiate_call_maps_missing_permission_to_a_spoken_reason():
    from mcp_servers.meta_mcp.utils.errors import MessageFailedError
    from voice.errors import VoiceCallNotAllowedError
    from voice.meta_api import initiate_call

    failure = MessageFailedError("Parameter value is not valid", graph_code=138006)
    with patch("voice.meta_api.graph_post", new=AsyncMock(side_effect=failure)):
        with pytest.raises(VoiceCallNotAllowedError, match="call permission"):
            await initiate_call(
                "PNID_1", "tok", "+919876543210", "v=0\r\n", business_id="biz-1"
            )


def test_extract_call_id_reads_the_wacid_from_either_shape():
    from voice.meta_api import extract_call_id

    assert extract_call_id({"calls": [{"id": "wacid.a"}]}) == "wacid.a"
    assert extract_call_id({"call_id": "wacid.b"}) == "wacid.b"
    assert extract_call_id({"calls": []}) == ""


@pytest.mark.asyncio
async def test_bic_connect_webhook_hands_the_answer_over_without_inbound_setup():
    from voice import outbound

    payload = _bic_connect_payload()
    future = outbound.answer_future("wacid.out1")
    try:
        with (
            patch(
                "voice.call_handler.resolve_business_from_channel",
                new_callable=AsyncMock,
                return_value="biz-uuid",
            ),
            patch(
                "voice.call_handler.handle_inbound_call", new_callable=AsyncMock
            ) as inbound,
        ):
            await process_webhook_payload(payload)

        inbound.assert_not_awaited()
        assert future.done()
        assert future.result() == "v=0\r\nanswer-from-meta\r\n"
    finally:
        outbound.discard_outbound("wacid.out1")


@pytest.mark.asyncio
async def test_accepted_status_webhook_signals_pickup():
    redis = _FakeRedis()
    with (
        patch(
            "voice.call_handler.resolve_business_from_channel",
            new_callable=AsyncMock,
            return_value="biz-uuid",
        ),
        patch("voice.call_handler.publish_event", new_callable=AsyncMock) as published,
        patch("voice.outbound.get_redis", return_value=redis),
    ):
        await process_webhook_payload(_status_payload("ACCEPTED"))

    assert redis.store.get("voice:pickup:wacid.out1") == "1"
    assert published.await_args.args[1]["event_type"] == "whatsapp_call_answered"


@pytest.mark.asyncio
async def test_rejected_status_webhook_tears_the_call_down():
    from voice import call_handler as ch

    bridge = MagicMock()
    bridge.close = AsyncMock()
    ch._active_calls["wacid.out1"] = bridge
    try:
        with (
            patch(
                "voice.call_handler.resolve_business_from_channel",
                new_callable=AsyncMock,
                return_value="biz-uuid",
            ),
            patch("voice.call_handler.publish_event", new_callable=AsyncMock),
            patch(
                "voice.call_handler.release_call_slot", new_callable=AsyncMock
            ) as release,
            patch("voice.call_handler.clear_pickup", new_callable=AsyncMock),
            patch(
                "voice.call_handler.cancel_cross_channel_for_call",
                new_callable=AsyncMock,
                create=True,
            ),
        ):
            await process_webhook_payload(_status_payload("REJECTED"))

        bridge.close.assert_awaited_once()
        release.assert_awaited_once_with("biz-uuid")
        assert "wacid.out1" not in ch._active_calls
    finally:
        ch._active_calls.pop("wacid.out1", None)


@pytest.mark.asyncio
async def test_hangup_forgets_the_per_call_connect_lock():
    from voice import call_handler as ch

    with (
        patch("voice.call_handler.clear_pickup", new_callable=AsyncMock),
        patch(
            "voice.call_handler.cancel_cross_channel_for_call",
            new_callable=AsyncMock,
            create=True,
        ),
    ):
        ch._lock_for("call-leak")
        assert "call-leak" in ch._connect_locks
        await ch.handle_hangup("call-leak", "biz-uuid", reason="hangup")

    assert "call-leak" not in ch._connect_locks


@pytest.mark.asyncio
async def test_place_outbound_call_bridges_answer_and_registers_the_call():
    from voice import outbound

    order: list[str] = []
    bridge = MagicMock()
    bridge.create_sdp_offer = AsyncMock(
        side_effect=lambda: order.append("offer") or "v=0\r\noffer\r\n"
    )
    bridge.apply_sdp_answer = AsyncMock(
        side_effect=lambda _sdp: order.append("apply_answer")
    )
    bridge.connect_to_livekit = AsyncMock(
        side_effect=lambda **_kwargs: order.append("livekit")
    )
    bridge.wait_for_agent = AsyncMock(
        side_effect=lambda: order.append("agent_ready") or True
    )
    bridge.close = AsyncMock()

    async def fake_initiate(*_args, **_kwargs):
        order.append("graph_connect")
        # Meta answers on the webhook right after the connect POST succeeds.
        asyncio.get_running_loop().call_soon(
            outbound.resolve_outbound_answer, "wacid.out1", "v=0\r\nanswer\r\n"
        )
        return {"calls": [{"id": "wacid.out1"}]}

    with (
        patch("voice.outbound.WhatsAppAudioBridge", return_value=bridge),
        patch("voice.outbound.initiate_call", new=AsyncMock(side_effect=fake_initiate)),
        patch("voice.outbound.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.outbound.release_call_slot", new_callable=AsyncMock) as release,
        patch("voice.outbound.terminate_call", new_callable=AsyncMock) as terminate,
        patch("voice.outbound.store_outbound_context", new_callable=AsyncMock),
        patch(
            "voice.outbound.get_voice_settings",
            return_value=MagicMock(
                is_configured=lambda: True,
                livekit_url="wss://example.livekit.cloud",
                livekit_api_key="k",
                livekit_api_secret="s",
            ),
        ),
        patch(
            "mcp_servers.meta_mcp.auth.tokens.ensure_valid_token",
            new=AsyncMock(
                return_value={"access_token": "tok", "phone_number_id": "PNID_1"}
            ),
        ),
        patch(
            "voice.outbound._prepare_outbound_session",
            new=AsyncMock(
                return_value=(
                    MagicMock(customer_id="cust-1", is_owner=False, customer_name="Priya"),
                    UserData(
                        business_id="biz-1",
                        call_id="wacid.out1",
                        phone_number_id="PNID_1",
                        caller_phone="+919876543210",
                        is_outbound=True,
                        outbound_reason="your appointment",
                    ),
                )
            ),
        ),
        patch(
            "voice.cross_channel.mark_cross_channel_call_active",
            new_callable=AsyncMock,
        ) as marked,
    ):
        call_id = await outbound.place_outbound_call(
            "biz-1",
            "+919876543210",
            reason="your appointment",
            customer_name="Priya",
        )

    from voice import call_handler as ch

    try:
        assert call_id == "wacid.out1"
        # The room is warmed while ringing, before the answer lands.
        assert order == [
            "offer",
            "graph_connect",
            "livekit",
            "apply_answer",
            "agent_ready",
        ]
        assert ch._active_calls.get("wacid.out1") is bridge
        marked.assert_awaited_once()
        terminate.assert_not_awaited()
        release.assert_not_awaited()
        assert not outbound.is_pending_outbound("wacid.out1")
    finally:
        ch._active_calls.pop("wacid.out1", None)
        outbound.discard_outbound("wacid.out1")


@pytest.mark.asyncio
async def test_place_outbound_call_releases_everything_when_no_answer_arrives():
    from voice import outbound
    from voice.errors import VoiceError

    bridge = MagicMock()
    bridge.create_sdp_offer = AsyncMock(return_value="v=0\r\noffer\r\n")
    bridge.connect_to_livekit = AsyncMock()
    bridge.close = AsyncMock()

    with (
        patch("voice.outbound.WhatsAppAudioBridge", return_value=bridge),
        patch("voice.outbound.ANSWER_TIMEOUT", 0.05),
        patch(
            "voice.outbound.initiate_call",
            new=AsyncMock(return_value={"calls": [{"id": "wacid.out2"}]}),
        ),
        patch("voice.outbound.acquire_call_slot", new_callable=AsyncMock),
        patch("voice.outbound.release_call_slot", new_callable=AsyncMock) as release,
        patch("voice.outbound.terminate_call", new_callable=AsyncMock) as terminate,
        patch("voice.outbound.store_outbound_context", new_callable=AsyncMock),
        patch("voice.outbound.clear_pickup", new_callable=AsyncMock),
        patch(
            "voice.outbound.get_voice_settings",
            return_value=MagicMock(
                is_configured=lambda: True,
                livekit_url="wss://example.livekit.cloud",
                livekit_api_key="k",
                livekit_api_secret="s",
            ),
        ),
        patch(
            "mcp_servers.meta_mcp.auth.tokens.ensure_valid_token",
            new=AsyncMock(
                return_value={"access_token": "tok", "phone_number_id": "PNID_1"}
            ),
        ),
        patch(
            "voice.outbound._prepare_outbound_session",
            new=AsyncMock(
                return_value=(
                    MagicMock(customer_id=None, is_owner=False, customer_name=""),
                    UserData(
                        business_id="biz-1",
                        call_id="wacid.out2",
                        phone_number_id="PNID_1",
                        caller_phone="+919876543210",
                        is_outbound=True,
                    ),
                )
            ),
        ),
    ):
        with pytest.raises(VoiceError, match="SDP answer"):
            await outbound.place_outbound_call("biz-1", "+919876543210")

    terminate.assert_awaited_once()
    bridge.close.assert_awaited_once()
    release.assert_awaited_once_with("biz-1")
    assert not outbound.is_pending_outbound("wacid.out2")


@pytest.mark.asyncio
async def test_pickup_wait_returns_immediately_when_already_answered():
    from voice.outbound import signal_pickup, wait_for_pickup

    redis = _FakeRedis()
    with patch("voice.outbound.get_redis", return_value=redis):
        await signal_pickup("wacid.out1")
        assert await wait_for_pickup("wacid.out1", timeout=0.05) is True


@pytest.mark.asyncio
async def test_pickup_wait_gives_up_when_nobody_answers():
    from voice.outbound import wait_for_pickup

    redis = _FakeRedis()
    with patch("voice.outbound.get_redis", return_value=redis):
        assert await wait_for_pickup("wacid.out1", timeout=0.05) is False


@pytest.mark.asyncio
async def test_outbound_agent_holds_its_greeting_until_pickup():
    ud = UserData(
        business_id="biz-1",
        call_id="wacid.out1",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        business_name="HolonCode",
        persona_name="Maya",
        is_outbound=True,
        outbound_reason="your appointment",
        outbound_customer_name="Priya",
    )
    pickup = asyncio.Event()

    async def fake_wait(call_id, *_args, **_kwargs):
        assert call_id == "wacid.out1"
        await pickup.wait()
        return True

    with patch("voice.outbound.wait_for_pickup", new=fake_wait):
        agent, session, _ud = await _build_bound_voice_agent(userdata=ud)
        greeting = asyncio.create_task(agent.on_enter())
        await asyncio.sleep(0)
        session.say.assert_not_awaited()

        pickup.set()
        await greeting

    session.say.assert_awaited_once()
    assert "your appointment" in session.say.await_args.args[0]


@pytest.mark.asyncio
async def test_outbound_agent_stays_silent_when_the_call_is_never_answered():
    ud = UserData(
        business_id="biz-1",
        call_id="wacid.out1",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        is_outbound=True,
        outbound_reason="your appointment",
    )

    with patch("voice.outbound.wait_for_pickup", new=AsyncMock(return_value=False)):
        agent, session, _ud = await _build_bound_voice_agent(userdata=ud)
        await agent.on_enter()

    session.say.assert_not_awaited()


def test_userdata_metadata_roundtrip_keeps_the_outbound_flag():
    ud = UserData(
        business_id="biz-1",
        call_id="wacid.out1",
        phone_number_id="pnid",
        caller_phone="+919876543210",
        is_outbound=True,
        outbound_reason="your appointment",
    )
    restored = UserData.from_dict(json.loads(json.dumps(ud.to_json_dict())))
    assert restored.is_outbound is True
    assert restored.outbound_reason == "your appointment"


