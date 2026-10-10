"""Run a specialist ReAct agent to completion, including handoffs."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from langgraph.errors import GraphInterrupt

from agent.config import MULTI_AGENT_CONFIG
from agent.confirmation import interrupt_payload_from_result
from agent.factory import create_specialist_agent
from agent.handoff import parse_handoff_from_tool_results, resolve_handoff_target
from agent.registry import get_agent_definition

logger = logging.getLogger(__name__)


def _hitl_return(
    *,
    payload: dict[str, Any],
    specialist: str,
    thread_id: str,
    all_tools: list[str],
    all_results: list[dict[str, Any]],
    chain: list[str],
    token_usage: dict[str, int] | None,
    initial_specialist: str,
) -> dict[str, Any]:
    hitl = dict(payload)
    hitl["specialist"] = specialist
    hitl["thread_id"] = thread_id
    return {
        "status": "confirmation_required",
        "hitl": hitl,
        "response_text": "",
        "tool_calls_made": all_tools,
        "tool_results": all_results,
        "handoff_chain": chain,
        "specialist": specialist,
        "token_usage": token_usage,
        "initial_specialist": initial_specialist,
    }


async def run_specialist(
    agent_name: str,
    message_text: str,
    *,
    business_config: dict[str, Any],
    customer_profile: dict[str, Any] | None,
    channel: str,
    is_voice: bool,
    recent_messages: list[dict[str, Any]],
    cross_channel_messages: list[dict[str, Any]],
    checkpointer: Any,
    store: Any,
    thread_id: str,
    conversation_id: str | None = None,
    customer_memory: dict[str, Any] | None = None,
    mem0_memories: list[str] | None = None,
    invoke_config: dict[str, Any] | None = None,
    allow_handoff: bool = True,
    resume: Any | None = None,
) -> dict[str, Any]:
    """
    Invoke specialist (and optional customer handoffs).

    Returns response_text, tool_calls, tool_results, handoff_chain, specialist.
    """
    from agent.session import (
        _collect_tool_metadata,
        _invoke_agent_with_llm_retries,
        _resolve_response_text,
        _slice_current_turn,
    )

    chain: list[str] = []
    current = agent_name
    hops = 0
    all_tools: list[str] = []
    all_results: list[dict[str, Any]] = []
    response_text = ""
    last_specialist = current
    token_usage: dict[str, int] | None = None

    while True:
        defn = get_agent_definition(current)
        agent = await create_specialist_agent(
            current,
            business_config,
            customer_profile,
            channel,
            is_voice,
            recent_messages,
            cross_channel_messages,
            checkpointer,
            store,
            conversation_id=conversation_id,
            customer_memory=customer_memory,
            mem0_memories=mem0_memories,
        )
        timeout = (
            float(MULTI_AGENT_CONFIG["owner_specialist_timeout_s"])
            if defn.session_type == "owner"
            else float(MULTI_AGENT_CONFIG["specialist_timeout_s"])
        )
        recursion = int(defn.max_tool_calls) * 2 + 1
        config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion,
        }
        callbacks = None
        if invoke_config and invoke_config.get("callbacks"):
            callbacks = invoke_config["callbacks"]
        biz_id = str((business_config or {}).get("id") or "")
        from agent.observability import build_langfuse_callbacks

        hop_callbacks = build_langfuse_callbacks(
            trace_name=current,
            business_id=biz_id,
            session_id=thread_id,
            user_id=None,
            metadata={
                "specialist": current,
                "channel": channel,
                "route_method": "specialist",
            },
        )
        if hop_callbacks:
            callbacks = hop_callbacks
        if callbacks:
            config["callbacks"] = callbacks

        logger.info(
            "SPECIALIST_START specialist=%s tools_bound thread=%s timeout_s=%s resume=%s",
            current,
            thread_id,
            timeout,
            resume is not None,
        )
        hop_started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _invoke_agent_with_llm_retries(
                    agent,
                    message_text,
                    config,
                    thread_id=thread_id,
                    resume=resume,
                ),
                timeout=timeout,
            )
        except GraphInterrupt as exc:
            payload = interrupt_payload_from_result(
                {"__interrupt__": exc.args[0] if exc.args else ()}
            ) or {
                "type": "confirmation_required",
                "message": "Please confirm this action.",
            }
            return _hitl_return(
                payload=payload,
                specialist=current,
                thread_id=thread_id,
                all_tools=all_tools,
                all_results=all_results,
                chain=chain,
                token_usage=token_usage,
                initial_specialist=agent_name,
            )
        except Exception as exc:
            logger.error(
                "SPECIALIST_ERROR specialist=%s error_type=%s duration_ms=%d",
                current,
                type(exc).__name__,
                int((time.monotonic() - hop_started) * 1000),
            )
            raise

        hitl = interrupt_payload_from_result(result)
        if hitl:
            return _hitl_return(
                payload=hitl,
                specialist=current,
                thread_id=thread_id,
                all_tools=all_tools,
                all_results=all_results,
                chain=chain,
                token_usage=token_usage,
                initial_specialist=agent_name,
            )

        messages = result.get("messages") or []
        turn_messages = _slice_current_turn(messages)
        tool_calls, tool_results = _collect_tool_metadata(turn_messages)
        all_tools.extend(tool_calls)
        all_results.extend(tool_results)
        logger.info(
            "SPECIALIST_END specialist=%s tool_calls=%d tool_results=%d duration_ms=%d",
            current,
            len(tool_calls),
            len(tool_results),
            int((time.monotonic() - hop_started) * 1000),
        )
        last_specialist = current
        from agent.observability import extract_token_usage, merge_token_usage

        hop_usage = extract_token_usage(turn_messages)
        token_usage = merge_token_usage(token_usage, hop_usage)

        max_hops = int(MULTI_AGENT_CONFIG["max_handoffs_per_message"])
        handoff = parse_handoff_from_tool_results(tool_results) if allow_handoff else None
        if handoff and defn.session_type == "customer" and hops < max_hops:
            target = resolve_handoff_target(
                handoff.get("target_agent"),
                current_agent=current,
                hop_index=hops,
            )
            chain.append(f"{current}->{target}:{handoff.get('reason') or ''}")
            hops += 1
            logger.info(
                "SPECIALIST_HANDOFF from=%s to=%s hop=%s",
                current,
                target,
                hops,
            )
            current = target
            resume = None
            continue

        response_text = _resolve_response_text(turn_messages, tool_results, tool_calls)
        break

    return {
        "status": "ok",
        "response_text": response_text,
        "tool_calls_made": all_tools,
        "tool_results": all_results,
        "handoff_chain": chain,
        "specialist": last_specialist,
        "token_usage": token_usage,
        "initial_specialist": agent_name,
    }


async def resume_specialist(
    agent_name: str,
    *,
    approved: bool,
    business_config: dict[str, Any],
    channel: str,
    is_voice: bool,
    recent_messages: list[dict[str, Any]],
    cross_channel_messages: list[dict[str, Any]],
    checkpointer: Any,
    store: Any,
    thread_id: str,
    conversation_id: str | None = None,
    invoke_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume a paused owner specialist after HITL approve/decline."""
    return await run_specialist(
        agent_name,
        "",
        business_config=business_config,
        customer_profile=None,
        channel=channel,
        is_voice=is_voice,
        recent_messages=recent_messages,
        cross_channel_messages=cross_channel_messages,
        checkpointer=checkpointer,
        store=store,
        thread_id=thread_id,
        conversation_id=conversation_id,
        invoke_config=invoke_config,
        allow_handoff=False,
        resume={"approved": approved},
    )
