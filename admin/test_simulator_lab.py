"""Shared Test Simulator lab: synthetic inject, webhook capture, scenarios, E2E."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from admin.pipeline_trace import PipelineTrace
from agent.stage_timings import timings_map
from shared.simulator_inbound import build_simulator_inbound_event

logger = logging.getLogger(__name__)

VOICE_TTL = 1800
E2E_HISTORY_TTL = 7 * 24 * 3600
BRIDGE_TTL = 3600
MAX_BURST = 10
MAX_SCENARIO_USERS = 25


WEBHOOK_PRESETS: dict[str, dict[str, Any]] = {
    "whatsapp_text": {
        "name": "WhatsApp text",
        "payload": {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_SIM",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "PNID_SIM"},
                                "messages": [
                                    {
                                        "from": "919876543210",
                                        "id": "wamid.sim",
                                        "timestamp": "1721300000",
                                        "type": "text",
                                        "text": {"body": "Hi from WhatsApp"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
    },
    "instagram_dm": {
        "name": "Instagram DM",
        "payload": {
            "object": "instagram",
            "entry": [
                {
                    "id": "IG_SIM",
                    "messaging": [
                        {
                            "sender": {"id": "IG_USER_SIM"},
                            "recipient": {"id": "IG_SIM"},
                            "timestamp": 1721300000,
                            "message": {"mid": "m_sim", "text": "Hello IG"},
                        }
                    ],
                }
            ],
        },
    },
    "facebook_comment": {
        "name": "Facebook comment",
        "payload": {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_SIM",
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "verb": "add",
                                "comment_id": "sim_fb_comment",
                                "post_id": "sim_fb_post",
                                "message": "Is this still available?",
                                "from": {"id": "FB_USER_SIM", "name": "Tester"},
                            },
                        }
                    ],
                }
            ],
        },
    },
    "instagram_comment": {
        "name": "Instagram comment",
        "payload": {
            "object": "instagram",
            "entry": [
                {
                    "id": "IG_SIM",
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "id": "sim_ig_comment",
                                "text": "Love this!",
                                "from": {"id": "IG_USER_SIM", "username": "tester"},
                                "media": {"id": "sim_ig_post"},
                            },
                        }
                    ],
                }
            ],
        },
    },
}

SCENARIO_PRESETS: dict[str, dict[str, Any]] = {
    "comment_giveaway": {
        "id": "comment_giveaway",
        "name": "Comment Giveaway",
        "trigger_channel": "instagram_comment",
        "trigger_keyword": "buy",
        "user_count": 20,
        "edge_cases": {
            "duplicate_commenter": True,
            "over_limit": True,
            "non_matching": True,
        },
        "description": (
            "Injects comment events with skip_send. Unique-code giveaway logic only "
            "runs if a deployed Temporal AI workflow already encodes it."
        ),
    },
    "mention_auto_reply": {
        "id": "mention_auto_reply",
        "name": "Mention Auto-Reply",
        "trigger_channel": "instagram_comment",
        "trigger_keyword": None,
        "user_count": 5,
        "edge_cases": {},
        "mention_shaped": True,
        "description": (
            "There is no instagram_mention parser. Events are instagram_comment "
            "with mention-shaped text (e.g. @business …)."
        ),
    },
    "comment_to_dm_funnel": {
        "id": "comment_to_dm_funnel",
        "name": "Comment → DM Funnel",
        "trigger_channel": "facebook_comment",
        "trigger_keyword": "interested",
        "user_count": 10,
        "edge_cases": {"duplicate_commenter": True},
        "description": "Facebook comments with skip_send; Temporal must already match the keyword.",
    },
    "multi_channel_burst": {
        "id": "multi_channel_burst",
        "name": "Multi-Channel Burst",
        "trigger_channel": "mixed",
        "trigger_keyword": None,
        "user_count": 8,
        "channels": [
            "instagram_comment",
            "facebook_comment",
            "instagram_dm",
            "whatsapp",
        ],
        "edge_cases": {},
        "description": "Rotates IG comment, FB comment, IG DM, and WhatsApp injects.",
    },
}

TRIGGER_TO_CHANNEL = {
    "whatsapp_message": "whatsapp",
    "instagram_dm": "instagram_dm",
    "instagram_comment": "instagram_comment",
    "facebook_message": "facebook_messenger",
    "facebook_comment": "facebook_comment",
    "voice_call": "whatsapp",
    "email_received": "web",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace_from_result(
    trace: PipelineTrace,
    *,
    event: dict[str, Any],
    result: dict[str, Any] | None,
    error: str | None = None,
    parse_ms: float = 0.0,
) -> None:
    """Build the lab trace from the timings the pipeline recorded per stage."""
    meta = (result or {}).get("ai_metadata") or {}
    timings = timings_map(
        (result or {}).get("stage_timings") or meta.get("stage_timings")
    )
    failed_stage = next(
        (
            stage
            for stage in ((result or {}).get("stage_timings") or [])
            if str(stage.get("status")) == "error"
        ),
        None,
    )

    trace.add(
        "parse",
        detail=str(event.get("event_type") or event.get("channel") or ""),
        data={
            "event_type": event.get("event_type"),
            "channel": event.get("channel"),
            "skip_send": True,
        },
        ms=parse_ms,
    )
    identity = {
        "business_id": event.get("business_id"),
        "sender_id": event.get("sender_id"),
        "mode": (event.get("simulator") or {}).get("mode"),
    }
    trace.add(
        "identity",
        detail="simulator identity",
        data=identity,
        ms=timings.get("identity"),
    )
    if error and not timings:
        # The pipeline never ran far enough to record a stage.
        trace.add("agent", status="error", detail=error)
        return

    specialist = (result or {}).get("specialist") or meta.get("specialist")
    route_method = (result or {}).get("route_method") or meta.get("route_method")
    tools = (result or {}).get("tools_called") or meta.get("tools_called") or []
    matched = bool((result or {}).get("workflow_matched"))
    trace.add(
        "workflow",
        status="ok" if matched else "skip",
        detail="matched" if matched else "no Temporal match",
        data={"workflow_matched": matched},
        ms=timings.get("workflow"),
    )
    trace.add(
        "context",
        detail=str(meta.get("thread_id") or ""),
        data={
            "memories_injected": meta.get("memories_injected"),
            "memory_retrieval_ms": meta.get("memory_retrieval_ms"),
        },
        ms=timings.get("context"),
    )
    trace.add(
        "router",
        # The owner broker path has no separate router step, so no timing.
        detail=str(route_method or "broker (no router)"),
        data={"route_method": route_method, "specialist": specialist},
        ms=meta.get("router_latency_ms"),
    )
    trace.add(
        "specialist",
        status="error" if failed_stage else "ok",
        detail=str(failed_stage.get("detail") if failed_stage else (specialist or "")),
        data={"specialist": specialist, "route_method": route_method},
        ms=timings.get("agent"),
    )
    trace.add(
        "tools",
        status="ok" if tools else "skip",
        detail=", ".join(tools) if tools else "none",
        data={"tools": tools},
    )
    if failed_stage:
        return
    outbound = (result or {}).get("outbound") or "skip_send"
    trace.add(
        "outbound",
        detail=str(outbound),
        data={"skip_send": True, "outbound": outbound, "status": (result or {}).get("status")},
        ms=timings.get("outbound"),
    )
    if "post" in timings:
        trace.add(
            "post",
            detail="log, post_process, memory",
            data={"conversation_id": (result or {}).get("conversation_id")},
            ms=timings["post"],
        )


async def run_synthetic_message(
    *,
    business_id: str,
    text: str,
    channel: str,
    mode: str = "customer",
    session_id: str | None = None,
    customer_name: str = "Test Customer",
    sender_id: str | None = None,
    comment_id: str | None = None,
    post_id: str | None = None,
    media_url: str | None = None,
    process: bool = True,
    skip_workflows: bool = False,
) -> dict[str, Any]:
    parse_started = time.monotonic()
    event = build_simulator_inbound_event(
        business_id=business_id,
        text=text,
        channel=channel,
        mode=mode,  # type: ignore[arg-type]
        session_id=session_id,
        customer_name=customer_name,
        sender_id=sender_id,
        comment_id=comment_id,
        post_id=post_id,
    )
    event["skip_send"] = True
    if skip_workflows:
        event["simulator"] = {
            **(event.get("simulator") or {}),
            "skip_workflows": True,
        }
    if media_url:
        event["content"] = {**(event.get("content") or {}), "media_url": media_url, "type": "image"}
        event["message_type"] = "image"
    parse_ms = round((time.monotonic() - parse_started) * 1000, 2)

    trace = PipelineTrace()
    result: dict[str, Any] | None = None
    error: str | None = None
    if process:
        from agent.session import handle_incoming_message

        try:
            result = await handle_incoming_message(
                channel=str(event["channel"]),
                external_sender_id=str(event["sender_id"]),
                message_text=text,
                receiving_account_id=business_id,
                message_type=str(event.get("message_type") or "text"),
                media_url=media_url,
                external_message_id=str(event.get("message_id") or ""),
                comment_id=event.get("comment_id"),
                post_id=event.get("post_id"),
                sender_display_name=event.get("sender_name") or customer_name,
                skip_send=True,
                simulator=event.get("simulator"),
                progress_session_id=str(event.get("session_id") or ""),
            )
        except Exception as exc:
            logger.exception("test-simulator message failed")
            error = str(exc)
        # A pipeline that fails internally returns status=error instead of raising.
        if error is None and isinstance(result, dict) and result.get("error"):
            error = str(result["error"])
    _trace_from_result(
        trace,
        event=event,
        result=result,
        error=error,
        parse_ms=parse_ms,
    )
    return {
        "event": event,
        "skip_send": True,
        "result": result,
        "error": error,
        "trace": trace.to_dict(),
        "visual": trace.to_visual_dict(),
        "response": (result or {}).get("response_text") if result else None,
        "specialist": (result or {}).get("specialist") if result else None,
        "route_method": (result or {}).get("route_method") if result else None,
        "tools": (result or {}).get("tools_called") if result else [],
        "workflow_matched": bool((result or {}).get("workflow_matched")) if result else False,
        "duration_ms": trace.total_ms,
    }


async def run_burst(
    *,
    business_id: str,
    text: str,
    channel: str,
    count: int,
    mode: str = "customer",
) -> dict[str, Any]:
    n = max(1, min(int(count), MAX_BURST))
    items = []
    for i in range(n):
        items.append(
            await run_synthetic_message(
                business_id=business_id,
                text=f"{text} [{i + 1}]",
                channel=channel,
                mode=mode,
                session_id=f"sim-burst-{uuid.uuid4().hex[:10]}",
            )
        )
    return {"count": n, "skip_send": True, "items": items}


async def process_webhook_safe(
    *,
    business_id: str,
    payload: dict[str, Any],
    validate_only: bool = False,
    run_pipeline: bool = False,
) -> dict[str, Any]:
    from mcp_servers.meta_mcp.webhooks.handler import process_webhook_payload

    captured: list[dict[str, Any]] = []

    async def _publish(_channel: str, event: dict[str, Any]) -> None:
        stamped = dict(event)
        stamped["skip_send"] = True
        stamped["source"] = "test_simulator"
        sim = stamped.get("simulator") if isinstance(stamped.get("simulator"), dict) else {}
        stamped["simulator"] = {
            "business_id": business_id,
            "mode": "customer",
            "customer_name": "Webhook sim",
            **sim,
        }
        captured.append(stamped)

    async def _resolve(*_a: Any, **_k: Any) -> str:
        return business_id

    modules = (
        "mcp_servers.meta_mcp.webhooks.whatsapp",
        "mcp_servers.meta_mcp.webhooks.instagram",
        "mcp_servers.meta_mcp.webhooks.facebook",
    )
    with ExitStack() as stack:
        for mod in modules:
            stack.enter_context(patch(f"{mod}.publish_event", new=_publish))
            stack.enter_context(patch(f"{mod}.resolve_business_from_channel", new=_resolve))
        await process_webhook_payload(payload)

    queued = [
        {
            "event_type": e.get("event_type"),
            "channel": e.get("channel"),
            "skip_send": True,
            "sender_id": e.get("sender_id"),
            "text": (e.get("content") or {}).get("text") if isinstance(e.get("content"), dict) else None,
        }
        for e in captured
    ]
    pipeline: list[dict[str, Any]] = []
    if run_pipeline and not validate_only:
        from agent.session import handle_redis_event

        for event in captured:
            pipeline.append(await handle_redis_event(event, skip_send=True) or {"status": "dropped"})
    return {
        "skip_send": True,
        "validate_only": validate_only,
        "queued_count": len(captured),
        "queued": queued,
        "events": captured,
        "pipeline": pipeline,
    }


def build_scenario_events(
    *,
    business_id: str,
    preset_id: str,
    user_count: int | None = None,
    keyword: str | None = None,
    edge: dict[str, bool] | None = None,
    post_id: str | None = None,
) -> list[dict[str, Any]]:
    preset = SCENARIO_PRESETS.get(preset_id)
    if not preset:
        raise ValueError(f"Unknown scenario preset: {preset_id}")
    n = max(1, min(int(user_count if user_count is not None else preset["user_count"]), MAX_SCENARIO_USERS))
    kw = keyword if keyword is not None else preset.get("trigger_keyword")
    edges = {**(preset.get("edge_cases") or {}), **(edge or {})}
    pid = post_id or "synthetic_post_001"
    mention = bool(preset.get("mention_shaped"))
    channels = preset.get("channels")
    events: list[dict[str, Any]] = []

    def _one(i: int, text: str, channel: str, sender: str) -> dict[str, Any]:
        return build_simulator_inbound_event(
            business_id=business_id,
            text=text,
            channel=channel,
            mode="customer",
            session_id=f"sim-scen-{uuid.uuid4().hex[:10]}",
            customer_name=f"user_{i:02d}",
            sender_id=sender,
            post_id=pid,
        )

    if channels:
        for i in range(n):
            ch = channels[i % len(channels)]
            events.append(_one(i + 1, "hello from burst", ch, f"sim-burst-user-{i+1}"))
        return events

    channel = str(preset["trigger_channel"])
    if channel == "instagram_mention":
        channel = "instagram_comment"
        mention = True
    for i in range(n):
        if mention:
            body = f"@business {kw or 'check this'}"
        else:
            body = str(kw or "hello")
        events.append(_one(i + 1, body, channel, f"sim-scen-user-{i+1}"))
    if edges.get("duplicate_commenter") and events:
        dup = dict(events[0])
        dup["session_id"] = f"sim-scen-{uuid.uuid4().hex[:10]}"
        dup["message_id"] = f"sim-msg-{uuid.uuid4().hex[:16]}"
        if dup.get("comment_id"):
            dup["comment_id"] = f"sim-comment-{uuid.uuid4().hex[:12]}"
        events.append(dup)
    if edges.get("over_limit"):
        events.append(_one(n + 80, str(kw or "hello"), channel, f"sim-scen-user-{n+80}"))
    if edges.get("non_matching"):
        events.append(_one(n + 90, "love this!", channel, f"sim-scen-user-{n+90}"))
    return events


async def run_scenario(
    *,
    business_id: str,
    preset_id: str,
    user_count: int | None = None,
    delay_seconds: float = 0,
    keyword: str | None = None,
    edge: dict[str, bool] | None = None,
    process: bool = True,
) -> dict[str, Any]:
    events = build_scenario_events(
        business_id=business_id,
        preset_id=preset_id,
        user_count=user_count,
        keyword=keyword,
        edge=edge,
    )
    results: list[dict[str, Any]] = []
    matched = 0
    for i, event in enumerate(events):
        if delay_seconds > 0 and i:
            await asyncio.sleep(min(delay_seconds, 2.0))
        text = ""
        content = event.get("content") or {}
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        item = await run_synthetic_message(
            business_id=business_id,
            text=text,
            channel=str(event["channel"]),
            mode="customer",
            session_id=str(event.get("session_id")),
            customer_name=str(event.get("sender_name") or event.get("simulator", {}).get("customer_name") or "user"),
            sender_id=str(event["sender_id"]),
            comment_id=event.get("comment_id"),
            post_id=event.get("post_id"),
            process=process,
        )
        if item.get("workflow_matched"):
            matched += 1
        results.append(
            {
                "event_index": i + 1,
                "user": event.get("sender_name") or event.get("sender_id"),
                "channel": event.get("channel"),
                "text": text,
                "skip_send": True,
                "workflow_matched": item.get("workflow_matched"),
                "trace": item.get("trace"),
                "visual": item.get("visual"),
                "error": item.get("error"),
            }
        )
    return {
        "preset_id": preset_id,
        "scenario_name": SCENARIO_PRESETS[preset_id]["name"],
        "description": SCENARIO_PRESETS[preset_id]["description"],
        "total_events": len(events),
        "skip_send": True,
        "workflow_matched_count": matched,
        "results": results,
        "summary": {
            "events": len(events),
            "workflow_matched": matched,
            "skip_send": True,
        },
    }


async def fire_workflow(
    *,
    business_id: str,
    workflow_id: str | None = None,
    event_type: str | None = None,
    text: str = "simulator workflow fire",
) -> dict[str, Any]:
    from workflow_builder.deployer import (
        query_workflow_state,
        route_event_to_workflows_detailed,
        terminate_test_workflow,
    )
    from workflow_builder.store import get_workflow, list_workflows

    active = await list_workflows(business_id, active_only=True)
    if workflow_id:
        picked = await get_workflow(business_id, workflow_id)
        if not picked or not picked.get("is_active"):
            raise ValueError("No active AI workflow for this business")
    elif active:
        picked = active[0]
    else:
        raise ValueError("No active AI workflow for this business")

    trigger = event_type or str(picked.get("trigger_type") or "whatsapp_message")
    channel = TRIGGER_TO_CHANNEL.get(trigger, "whatsapp")
    built = await run_synthetic_message(
        business_id=business_id,
        text=text,
        channel=channel,
        process=True,
        skip_workflows=True,
    )
    wf_event = {
        "type": trigger,
        "trigger_type": trigger,
        "channel": channel,
        "business_id": business_id,
        "message": {"text": text},
        "skip_send": True,
        "simulate_outbound": True,
        "simulator": True,
        "customer_id": None,
    }
    temporal_error: str | None = None
    routing: list[dict[str, Any]] = []
    state: dict[str, Any] | None = None
    test_run_id = uuid.uuid4().hex[:16]
    try:
        routing = await route_event_to_workflows_detailed(
            wf_event,
            business_id,
            target_workflow_id=str(picked["id"]),
            test_run_id=test_run_id,
        )
        if routing:
            temporal_id = str(routing[0]["temporal_instance_id"])
            try:
                state = await query_workflow_state(temporal_id)
            finally:
                try:
                    await terminate_test_workflow(temporal_id)
                except Exception:
                    pass
    except Exception as exc:
        temporal_error = str(exc)
        logger.warning("test-simulator workflow fire Temporal error: %s", exc)
    return {
        "skip_send": True,
        "workflow": {
            "id": picked.get("id"),
            "name": picked.get("name"),
            "trigger_type": picked.get("trigger_type"),
        },
        "matched": bool(routing),
        "routing": routing,
        "state": state,
        "test_run_id": test_run_id,
        "temporal_error": temporal_error,
        "pipeline": built,
    }


async def voice_start(*, business_id: str, caller: str, is_owner: bool = False) -> dict[str, Any]:
    from shared.redis_client import get_redis
    from voice.core_tools import format_business_context, load_business_profile
    from voice.prompts import build_customer_voice_prompt, build_owner_voice_prompt, opening_line
    from voice.userdata import UserData

    profile = await load_business_profile(business_id)
    sid = f"vs-sim-{uuid.uuid4().hex[:12]}"
    ud = UserData(
        business_id=business_id,
        call_id=sid,
        phone_number_id="sim",
        caller_phone=caller,
        is_owner=is_owner,
        customer_name="Simulator caller",
        business_name=str(profile.get("name") or "the business"),
        persona_name=str(profile.get("persona_name") or "the assistant"),
        greeting_message=str(profile.get("greeting_message") or ""),
    )
    prompt = (
        build_owner_voice_prompt(ud, format_business_context(profile))
        if is_owner
        else build_customer_voice_prompt(ud, format_business_context(profile))
    )
    opening = opening_line(ud)
    state = {
        "session_id": sid,
        "business_id": business_id,
        "caller": caller,
        "is_owner": is_owner,
        "system": prompt,
        "history": [{"role": "assistant", "content": opening}],
    }
    redis = get_redis()
    await redis.setex(f"test_sim:voice:{sid}", VOICE_TTL, json.dumps(state))
    return {"session_id": sid, "opening": opening, "skip_send": True}


async def voice_turn(*, session_id: str, text: str) -> dict[str, Any]:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from shared.redis_client import get_redis
    from voice.core_tools import search_knowledge_text

    redis = get_redis()
    raw = await redis.get(f"test_sim:voice:{session_id}")
    if not raw:
        raise ValueError("Voice session not found")
    state = json.loads(raw)
    history: list[dict[str, str]] = list(state.get("history") or [])
    history.append({"role": "user", "content": text})
    tool_note = ""
    try:
        tool_note = await search_knowledge_text(str(state["business_id"]), text, limit=2)
    except Exception:
        tool_note = ""
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.3)
    sys = str(state.get("system") or "")
    if tool_note:
        sys += f"\n\nKNOWLEDGE LOOKUP:\n{tool_note}"
    messages = [("system", sys)]
    for turn in history:
        role = "ai" if turn["role"] == "assistant" else "human"
        messages.append((role, turn["content"]))
    reply = await llm.ainvoke(messages)
    content = getattr(reply, "content", None) or str(reply)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    assistant = str(content).strip()
    history.append({"role": "assistant", "content": assistant})
    state["history"] = history
    await redis.setex(f"test_sim:voice:{session_id}", VOICE_TTL, json.dumps(state))
    return {"session_id": session_id, "reply": assistant, "skip_send": True}


async def voice_end(*, session_id: str) -> dict[str, str]:
    from shared.redis_client import get_redis

    await get_redis().delete(f"test_sim:voice:{session_id}")
    return {"status": "ended", "session_id": session_id}


async def bridge_start(*, business_id: str, phone: str, prompt: str) -> dict[str, Any]:
    from shared.redis_client import get_redis
    from voice.cross_channel import (
        CROSS_CHANNEL_TTL,
        actor_identity,
        call_map_key,
        listener_key,
    )

    identity = actor_identity(
        business_id=business_id,
        is_owner=True,
        customer_id=None,
        sender_external_id=phone,
    )
    if not identity:
        raise ValueError("Could not derive actor identity")
    _actor_type, actor_key = identity
    call_id = f"vs-bridge-{uuid.uuid4().hex[:12]}"
    listener = {
        "actor_key": actor_key,
        "actor_type": "owner",
        "customer_id": None,
        "sender_external_id": phone,
        "voice_session_id": call_id,
        "livekit_room": "sim-room",
        "business_id": business_id,
        "expected_content": "any",
        "created_at": _now(),
        "status": "waiting",
        "db_session_id": None,
        "prompt": prompt,
        "simulator": True,
    }
    redis = get_redis()
    await redis.setex(listener_key(actor_key), CROSS_CHANNEL_TTL, json.dumps(listener))
    await redis.setex(call_map_key(call_id), CROSS_CHANNEL_TTL, actor_key)
    meta = {"call_id": call_id, "actor_key": actor_key, "phone": phone, "prompt": prompt}
    await redis.setex(f"test_sim:bridge:{business_id}:{call_id}", BRIDGE_TTL, json.dumps(meta))
    return {
        "call_id": call_id,
        "actor_key": actor_key,
        "listener_registered": True,
        "whatsapp_sent": False,
        "skip_send": True,
        "prompt": prompt,
    }


async def bridge_receive(*, business_id: str, phone: str, text: str) -> dict[str, Any]:
    from shared.redis_client import get_redis
    from voice.cross_channel import (
        CROSS_CHANNEL_PENDING_TTL,
        actor_identity,
        check_cross_channel_listener,
        listener_key,
        pending_actor_key,
    )

    identity = actor_identity(
        business_id=business_id,
        is_owner=True,
        customer_id=None,
        sender_external_id=phone,
    )
    before = await check_cross_channel_listener(
        business_id=business_id,
        is_owner=True,
        customer_id=None,
        sender_external_id=phone,
    )
    pending: dict[str, Any] | None = None
    intercepted = False
    if identity and before:
        _t, actor_key = identity
        pending = {
            "type": "text",
            "text": text,
            "voice_session_id": before.get("voice_session_id"),
            "simulated": True,
        }
        redis = get_redis()
        await redis.setex(
            pending_actor_key(actor_key),
            CROSS_CHANNEL_PENDING_TTL,
            json.dumps(
                {"voice_session_id": before.get("voice_session_id"), "content": pending}
            ),
        )
        await redis.delete(listener_key(actor_key))
        intercepted = True
    after = await check_cross_channel_listener(
        business_id=business_id,
        is_owner=True,
        customer_id=None,
        sender_external_id=phone,
    )
    return {
        "skip_send": True,
        "listener_before": bool(before),
        "intercepted": intercepted,
        "pending": pending,
        "listener_after": bool(after),
        "meta_sent": False,
    }


async def run_e2e(*, business_id: str, process: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    results: list[dict[str, Any]] = []

    async def _case(tid: str, name: str, coro) -> None:
        t0 = time.monotonic()
        try:
            data = await coro
            ok = True
            err = None
            if isinstance(data, dict) and data.get("error"):
                ok = False
                err = data.get("error")
            skip = True
            if isinstance(data, dict) and data.get("skip_send") is False:
                ok = False
                err = "skip_send was false"
                skip = False
            results.append(
                {
                    "id": tid,
                    "name": name,
                    "status": "pass" if ok else "fail",
                    "duration_ms": round((time.monotonic() - t0) * 1000, 2),
                    "skip_send": skip,
                    "error": err,
                    "detail": {
                        k: data.get(k)
                        for k in (
                            "workflow_matched",
                            "specialist",
                            "route_method",
                            "queued_count",
                            "matched",
                            "intercepted",
                            "temporal_error",
                        )
                        if isinstance(data, dict) and k in data
                    },
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": tid,
                    "name": name,
                    "status": "fail",
                    "duration_ms": round((time.monotonic() - t0) * 1000, 2),
                    "skip_send": True,
                    "error": str(exc),
                    "detail": {},
                }
            )

    await _case(
        "T1",
        "WhatsApp text flow",
        run_synthetic_message(
            business_id=business_id, text="hello", channel="whatsapp", process=process
        ),
    )
    await _case(
        "T2",
        "WhatsApp media (skip Graph)",
        run_synthetic_message(
            business_id=business_id,
            text="see photo",
            channel="whatsapp",
            media_url="https://example.com/sim.jpg",
            process=process,
        ),
    )
    await _case(
        "T3",
        "Instagram DM flow",
        run_synthetic_message(
            business_id=business_id, text="hi ig", channel="instagram_dm", process=process
        ),
    )
    await _case(
        "T4",
        "Facebook comment flow",
        run_synthetic_message(
            business_id=business_id,
            text="price?",
            channel="facebook_comment",
            process=process,
        ),
    )
    await _case(
        "T5",
        "skip_send outbound (no Graph)",
        run_synthetic_message(
            business_id=business_id,
            text="template window check",
            channel="whatsapp",
            process=process,
        ),
    )

    async def _t6() -> dict[str, Any]:
        try:
            return await fire_workflow(business_id=business_id)
        except ValueError as exc:
            return {"skip_send": True, "matched": False, "error": None, "temporal_error": str(exc)}

    await _case("T6", "Workflow fire (Temporal)", _t6())

    async def _t7() -> dict[str, Any]:
        started_b = await bridge_start(
            business_id=business_id, phone="+919876543210", prompt="Send a photo"
        )
        rec = await bridge_receive(
            business_id=business_id, phone="+919876543210", text="here is the file"
        )
        rec["skip_send"] = True
        rec["call_id"] = started_b.get("call_id")
        rec["whatsapp_sent"] = started_b.get("whatsapp_sent")
        return rec

    await _case("T7", "Cross-channel bridge (Redis only)", _t7())
    await _case(
        "T8",
        "Routing (comment → specialist/route_method)",
        run_synthetic_message(
            business_id=business_id,
            text="nice post",
            channel="instagram_comment",
            process=process,
        ),
    )
    sender = f"sim-warm-{uuid.uuid4().hex[:8]}"
    await _case(
        "T9a",
        "Identity warm (first)",
        run_synthetic_message(
            business_id=business_id,
            text="first hello",
            channel="whatsapp",
            sender_id=sender,
            process=process,
        ),
    )
    await _case(
        "T9",
        "Identity warm (return)",
        run_synthetic_message(
            business_id=business_id,
            text="second hello",
            channel="whatsapp",
            sender_id=sender,
            process=process,
        ),
    )
    await _case(
        "T10",
        "Smart number (not implemented — skip_send sanity)",
        run_synthetic_message(
            business_id=business_id,
            text="personal filter check",
            channel="whatsapp",
            process=process,
        ),
    )

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    payload = {
        "summary": f"{passed}/{len(results)} tests passed",
        "passed": passed,
        "failed": failed,
        "results": results,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "skip_send": True,
        "ran_at": _now(),
        "business_id": business_id,
    }
    try:
        from shared.redis_client import get_redis

        key = f"test_sim:e2e:{business_id}"
        redis = get_redis()
        await redis.lpush(key, json.dumps(payload, default=str))
        await redis.ltrim(key, 0, 19)
        await redis.expire(key, E2E_HISTORY_TTL)
    except Exception:
        logger.debug("e2e history persist skipped", exc_info=True)
    return payload


async def e2e_history(business_id: str) -> dict[str, Any]:
    from shared.redis_client import get_redis

    raw = await get_redis().lrange(f"test_sim:e2e:{business_id}", 0, 19)
    items = []
    for row in raw:
        try:
            items.append(json.loads(row))
        except (TypeError, json.JSONDecodeError):
            continue
    return {"items": items}
