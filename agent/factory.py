"""Create LangGraph ReAct agents per session."""

from __future__ import annotations

import logging
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore

from agent.config import AGENT_CONFIG, MULTI_AGENT_CONFIG
from agent.prompts import build_customer_system_prompt, build_owner_system_prompt
from agent.registry import get_agent_definition, planned_tool_names
from agent.settings import get_agent_settings
from agent.specialist_prompts import build_specialist_prompt
from agent.tools import get_customer_tools, get_owner_tools, tools_for_names
from onboarding.prompts import build_onboarding_system_prompt
from onboarding.tools import get_onboarding_tools

logger = logging.getLogger(__name__)


async def create_agent_for_session(
    session_type: str,
    business_config: dict[str, Any],
    customer_profile: dict[str, Any] | None,
    channel: str,
    is_voice: bool,
    recent_messages: list[dict[str, Any]],
    cross_channel_messages: list[dict[str, Any]],
    checkpointer: AsyncPostgresSaver,
    store: BaseStore,
    *,
    conversation_id: str | None = None,
    customer_memory: dict[str, Any] | None = None,
    mem0_memories: list[str] | None = None,
    onboarding_progress: Any | None = None,
) -> Any:
    """
    Build a ReAct agent for onboarding, owner, or customer mode.

    Tools are bound with business_id pre-injected so the LLM never sees it.
    """
    settings = get_agent_settings()
    model_name = settings.agent_model or AGENT_CONFIG["model"]

    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=settings.google_api_key,
        temperature=settings.agent_temperature,
        max_output_tokens=settings.agent_max_tokens,
    )

    business_id = str(business_config["id"])
    if session_type == "onboarding":
        tools = await get_onboarding_tools(business_id, conversation_id=conversation_id)
        system_prompt = build_onboarding_system_prompt(
            business_config=business_config,
            onboarding_progress=onboarding_progress,
            channel=channel,
            is_voice=is_voice,
            cross_channel_messages=cross_channel_messages,
        )
    elif session_type == "owner":
        tools = await get_owner_tools(business_id, conversation_id=conversation_id)
        system_prompt = build_owner_system_prompt(
            business_config=business_config,
            channel=channel,
            is_voice=is_voice,
            cross_channel_messages=cross_channel_messages,
        )
    else:
        tools = await get_customer_tools(business_id, conversation_id=conversation_id)
        system_prompt = build_customer_system_prompt(
            business_config=business_config,
            customer_profile=customer_profile,
            channel=channel,
            is_voice=is_voice,
            recent_messages=recent_messages,
            cross_channel_messages=cross_channel_messages,
            customer_memory=customer_memory,
            mem0_memories=mem0_memories,
        )

    logger.info(
        "Creating %s agent for business=%s channel=%s tools=%d",
        session_type,
        business_id,
        channel,
        len(tools),
    )

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
        checkpointer=checkpointer,
        store=store,
    )
    return agent


async def create_specialist_agent(
    agent_name: str,
    business_config: dict[str, Any],
    customer_profile: dict[str, Any] | None,
    channel: str,
    is_voice: bool,
    recent_messages: list[dict[str, Any]],
    cross_channel_messages: list[dict[str, Any]],
    checkpointer: AsyncPostgresSaver,
    store: BaseStore,
    *,
    conversation_id: str | None = None,
    customer_memory: dict[str, Any] | None = None,
    mem0_memories: list[str] | None = None,
) -> Any:
    """ReAct specialist with registry-filtered tools + channel send tools."""
    from agent.handoff import build_handoff_tool
    from agent.tools import ensure_all_mcp_sessions
    from workflow_builder.tools import BUILDER_TOOL_NAMES

    defn = get_agent_definition(agent_name)
    settings = get_agent_settings()
    model_name = defn.model or settings.agent_model or MULTI_AGENT_CONFIG["specialist_default_model"]
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=settings.google_api_key,
        temperature=defn.temperature,
        max_output_tokens=int(MULTI_AGENT_CONFIG["specialist_max_tokens"]),
    )
    business_id = str(business_config["id"])
    await ensure_all_mcp_sessions()
    names = planned_tool_names(agent_name, channel)
    mcp_names = [
        n for n in names if n != "handoff_to_agent" and n not in BUILDER_TOOL_NAMES
    ]
    tools = await tools_for_names(
        mcp_names,
        business_id,
        conversation_id=conversation_id,
        require_confirmation=defn.session_type == "owner",
    )
    if "handoff_to_agent" in names:
        tools.append(build_handoff_tool())

    if agent_name == "workflow_agent":
        from workflow_builder.tools import bind_builder_tools

        tools.extend(
            bind_builder_tools(
                business_id,
                owner_conversation_id=conversation_id,
            )
        )

    if agent_name == "custom_tools_agent":
        from agent.mcp_connector import get_business_mcp_tools

        tools.extend(await get_business_mcp_tools(business_id))

    prompt_kwargs: dict[str, Any] = {
        "business_config": business_config,
        "customer_profile": customer_profile,
        "channel": channel,
        "is_voice": is_voice,
        "recent_messages": recent_messages,
        "cross_channel_messages": cross_channel_messages,
        "customer_memory": customer_memory,
    }
    if defn.session_type == "customer":
        prompt_kwargs["mem0_memories"] = mem0_memories
    system_prompt = build_specialist_prompt(defn.system_prompt_builder, **prompt_kwargs)
    logger.info(
        "Creating specialist=%s business=%s channel=%s tools=%d names=%s",
        agent_name,
        business_id,
        channel,
        len(tools),
        [t.name for t in tools],
    )
    return create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
        checkpointer=checkpointer,
        store=store,
    )
