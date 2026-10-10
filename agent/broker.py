"""Owner Orchestrator Broker — three meta-tools only (MULTI_AGENT_SPEC §4)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langchain_core.tools import StructuredTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from agent.config import MULTI_AGENT_CONFIG, broker_budget_s
from agent.broker_prompts import broker_delegate_names, build_broker_system_prompt
from agent.confirmation import HITL_MARKER, hitl_from_specialist_result
from agent.progress import publish_progress
from agent.settings import get_agent_settings
from agent.specialist_runner import run_specialist

logger = logging.getLogger(__name__)

BROKER_STATUS_MESSAGE = "Looking that up..."


class DelegateArgs(BaseModel):
    agent_name: str = Field(description="Specialist name")
    task: str = Field(description="Clear task with all needed context")


class ParallelTask(BaseModel):
    agent_name: str
    task: str


class DelegateParallelArgs(BaseModel):
    tasks: list[ParallelTask] = Field(max_length=4)


class ReplyArgs(BaseModel):
    message: str


async def run_broker_path(
    message_text: str,
    *,
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
    recipient_id: str | None = None,
    skip_send: bool = False,
    progress_session_id: str | None = None,
) -> dict[str, Any]:
    delegates: list[dict[str, Any]] = []
    all_tools: list[str] = []
    all_results: list[dict[str, Any]] = []
    max_delegations = int(MULTI_AGENT_CONFIG["broker_max_delegations"])
    delegate_token_usage: dict[str, int] | None = None
    hitl_state: dict[str, Any] | None = None
    status_sent = False
    biz_id = str((business_config or {}).get("id") or "")
    has_custom_mcp = False
    if biz_id:
        try:
            from agent.mcp_connector import has_mcp_connections

            has_custom_mcp = await has_mcp_connections(biz_id)
        except Exception:
            logger.debug("broker MCP connection check failed", exc_info=True)
    allowed_agents = broker_delegate_names(has_custom_mcp=has_custom_mcp)

    def _validate_agent(agent_name: str) -> str:
        if agent_name not in allowed_agents:
            raise ValueError(
                f"Unknown agent {agent_name}. Choose one of: {', '.join(allowed_agents)}"
            )
        return agent_name

    async def _send_broker_status() -> None:
        nonlocal status_sent
        if status_sent:
            return
        status_sent = True
        if progress_session_id:
            await publish_progress(
                biz_id,
                progress_session_id,
                "broker_status",
                text=BROKER_STATUS_MESSAGE,
            )
        if skip_send or not recipient_id or not biz_id:
            return
        try:
            from agent.pipeline import send_response

            await send_response(
                channel=channel,
                recipient_id=recipient_id,
                text=BROKER_STATUS_MESSAGE,
                business_id=biz_id,
            )
        except Exception as exc:
            logger.warning("Broker status send failed: %s", exc)

    async def _run_one(
        agent_name: str,
        task: str,
        *,
        thread_suffix: str | None = None,
    ) -> str:
        nonlocal delegate_token_usage, hitl_state
        await _send_broker_status()
        if len(delegates) >= max_delegations:
            return json.dumps({"error": "max delegations reached"})
        name = _validate_agent(agent_name)
        spec_thread = f"{thread_id}:spec:{name}"
        if thread_suffix:
            spec_thread = f"{spec_thread}:{thread_suffix}"
        result = await run_specialist(
            name,
            task,
            business_config=business_config,
            customer_profile=None,
            channel=channel,
            is_voice=is_voice,
            recent_messages=recent_messages,
            cross_channel_messages=cross_channel_messages,
            checkpointer=checkpointer,
            store=store,
            thread_id=spec_thread,
            conversation_id=conversation_id,
            customer_memory=None,
            invoke_config=invoke_config,
            allow_handoff=False,
        )
        hitl = hitl_from_specialist_result(result)
        if hitl:
            hitl_state = hitl
            delegates.append(
                {
                    "agent": name,
                    "task": task,
                    "preview": (hitl.get("preview") or "")[:200],
                    "hitl": True,
                }
            )
            return json.dumps({HITL_MARKER: True, **hitl})
        delegates.append(
            {
                "agent": name,
                "task": task,
                "preview": (result.get("response_text") or "")[:200],
            }
        )
        all_tools.extend(result.get("tool_calls_made") or [])
        all_results.extend(result.get("tool_results") or [])
        from agent.observability import merge_token_usage

        delegate_token_usage = merge_token_usage(
            delegate_token_usage, result.get("token_usage")
        )
        return result.get("response_text") or ""

    async def delegate_to_agent(agent_name: str, task: str) -> str:
        return await _run_one(agent_name, task)

    async def delegate_parallel(tasks: list[dict[str, Any]] | list[ParallelTask]) -> str:
        normalized: list[tuple[str, str]] = []
        for item in tasks:
            if isinstance(item, ParallelTask):
                normalized.append((item.agent_name, item.task))
            elif isinstance(item, dict):
                normalized.append((str(item.get("agent_name")), str(item.get("task"))))
            else:
                normalized.append((str(getattr(item, "agent_name", "")), str(getattr(item, "task", ""))))
        if len(normalized) > 4:
            normalized = normalized[:4]
        parallel_run_id = uuid.uuid4().hex[:12]
        gathered = await asyncio.gather(
            *[
                _run_one(a, t, thread_suffix=f"parallel:{parallel_run_id}:{index}")
                for index, (a, t) in enumerate(normalized)
            ],
            return_exceptions=True,
        )
        parts = []
        for (agent_name, _), out in zip(normalized, gathered):
            if isinstance(out, Exception):
                parts.append(f"{agent_name}: ERROR {out}")
            else:
                parts.append(f"{agent_name}: {out}")
        return "\n\n".join(parts)

    async def reply_to_owner(message: str) -> str:
        return message

    tools = [
        StructuredTool.from_function(
            coroutine=delegate_to_agent,
            name="delegate_to_agent",
            description=(
                "Send a task to ONE specialist and get the result. "
                f"agent_name must be one of: {', '.join(allowed_agents)}"
            ),
            args_schema=DelegateArgs,
        ),
        StructuredTool.from_function(
            coroutine=delegate_parallel,
            name="delegate_parallel",
            description=(
                "Run multiple specialists simultaneously for independent tasks (max 4)."
            ),
            args_schema=DelegateParallelArgs,
        ),
        StructuredTool.from_function(
            coroutine=reply_to_owner,
            name="reply_to_owner",
            description="Send a direct message to the owner when no specialist is needed.",
            args_schema=ReplyArgs,
        ),
    ]

    settings = get_agent_settings()
    llm = ChatGoogleGenerativeAI(
        model=MULTI_AGENT_CONFIG["broker_model"],
        google_api_key=settings.google_api_key,
        temperature=float(MULTI_AGENT_CONFIG["broker_temperature"]),
        max_output_tokens=int(MULTI_AGENT_CONFIG["broker_max_tokens"]),
    )
    prompt = build_broker_system_prompt(business_config, has_custom_mcp=has_custom_mcp)
    broker = create_react_agent(
        model=llm,
        tools=tools,
        prompt=prompt,
        checkpointer=checkpointer,
        store=store,
    )
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"{thread_id}:broker"},
        "recursion_limit": 8,
    }
    from agent.observability import build_langfuse_callbacks, extract_token_usage, merge_token_usage

    biz_id = str((business_config or {}).get("id") or "")
    broker_callbacks = build_langfuse_callbacks(
        trace_name="broker",
        business_id=biz_id,
        session_id=thread_id,
        user_id="owner",
        metadata={
            "specialist": "broker",
            "route_method": "broker",
            "channel": channel,
        },
    )
    if broker_callbacks:
        config["callbacks"] = broker_callbacks
    elif invoke_config and invoke_config.get("callbacks"):
        config["callbacks"] = invoke_config["callbacks"]

    timeout = broker_budget_s()
    from agent.session import (
        _collect_tool_metadata,
        _invoke_agent_with_llm_retries,
        _resolve_response_text,
        _slice_current_turn,
    )
    from langgraph.errors import GraphInterrupt
    from agent.confirmation import interrupt_payload_from_result

    try:
        result = await asyncio.wait_for(
            _invoke_agent_with_llm_retries(
                broker, message_text, config, thread_id=thread_id
            ),
            timeout=timeout,
        )
    except GraphInterrupt as exc:
        payload = interrupt_payload_from_result(
            {"__interrupt__": exc.args[0] if exc.args else ()}
        )
        if payload and not hitl_state:
            hitl_state = payload
        result = {}

    if isinstance(result, dict) and not hitl_state:
        nested = interrupt_payload_from_result(result)
        if nested:
            hitl_state = nested

    messages = result.get("messages") or [] if isinstance(result, dict) else []
    turn = _slice_current_turn(messages)
    broker_calls, broker_results = _collect_tool_metadata(turn)
    all_tools = list(broker_calls) + all_tools
    all_results = list(broker_results) + all_results
    response_text = _resolve_response_text(turn, broker_results, broker_calls)
    for entry in reversed(broker_results):
        if entry.get("tool_name") == "reply_to_owner":
            raw = entry.get("raw") or entry.get("message")
            if isinstance(raw, str) and raw.strip():
                response_text = raw
                break

    out: dict[str, Any] = {
        "status": "confirmation_required" if hitl_state else "ok",
        "response_text": response_text,
        "tool_calls_made": all_tools,
        "tool_results": all_results,
        "delegates": delegates,
        "specialist": "broker",
        "token_usage": merge_token_usage(
            extract_token_usage(turn), delegate_token_usage
        ),
    }
    if hitl_state:
        out["hitl"] = hitl_state
    return out
