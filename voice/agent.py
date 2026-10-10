"""LiveKit voice agent with Core knowledge/hours tools."""

from __future__ import annotations

import logging
from typing import Any

from voice.core_tools import (
    availability_text,
    business_hours_text,
    format_business_context,
    load_business_profile,
    search_entities_text,
    search_knowledge_text,
    update_business_context_text,
    update_business_info_text,
    upsert_catalog_item_text,
)
from voice.logging import log_voice_utterance
from voice.prompts import build_customer_voice_prompt, build_owner_voice_prompt
from voice.userdata import UserData

logger = logging.getLogger(__name__)


def _agent_base() -> Any:
    from livekit.agents.voice import Agent

    return Agent


def build_instructions(userdata: UserData, profile: dict[str, Any]) -> str:
    context = format_business_context(profile)
    if userdata.is_owner:
        return build_owner_voice_prompt(userdata, business_context=context)
    return build_customer_voice_prompt(userdata, business_context=context)


class REFLEXOVoiceAgent:
    """Factory wrapper; the real LiveKit Agent subclass is created in session.py."""

    def __init__(self, userdata: UserData, instructions: str) -> None:
        self.userdata = userdata
        self.instructions = instructions


async def log_agent_text(userdata: UserData, role: str, text: str) -> None:
    if not userdata.conversation_id or not text.strip():
        return
    inbound_role = "owner" if userdata.is_owner and role != "ai" else role
    await log_voice_utterance(
        userdata.business_id,
        userdata.conversation_id,
        role=inbound_role,
        content=text,
        call_id=userdata.call_id,
        ai_metadata={"source": "voice", "call_id": userdata.call_id},
    )


async def tool_search_knowledge(userdata: UserData, query: str) -> str:
    logger.info("VOICE_TOOL search_knowledge call_id=%s", userdata.call_id)
    return await search_knowledge_text(userdata.business_id, query)


async def tool_search_entities(userdata: UserData, query: str) -> str:
    logger.info("VOICE_TOOL search_entities call_id=%s", userdata.call_id)
    return await search_entities_text(userdata.business_id, query)


async def tool_business_hours(userdata: UserData) -> str:
    logger.info("VOICE_TOOL get_business_hours call_id=%s", userdata.call_id)
    return await business_hours_text(userdata.business_id)


async def tool_check_availability(
    userdata: UserData, service_name: str, date: str = ""
) -> str:
    logger.info(
        "VOICE_TOOL check_availability call_id=%s service=%s date=%s",
        userdata.call_id,
        service_name[:80],
        date[:20],
    )
    return await availability_text(userdata.business_id, service_name, date)


async def tool_update_business_info(
    userdata: UserData,
    field: str,
    value: Any,
) -> str:
    logger.info(
        "VOICE_TOOL update_business_info call_id=%s field=%s",
        userdata.call_id,
        field,
    )
    return await update_business_info_text(userdata.business_id, field, value)


async def tool_update_business_context(
    userdata: UserData,
    path: str,
    value: Any,
) -> str:
    logger.info(
        "VOICE_TOOL update_business_context call_id=%s path=%s",
        userdata.call_id,
        path,
    )
    return await update_business_context_text(userdata.business_id, path, value)


async def tool_upsert_catalog_item(
    userdata: UserData,
    entity_type: str,
    name: str,
    data: dict[str, Any],
    description: str | None = None,
) -> str:
    logger.info(
        "VOICE_TOOL upsert_catalog_item call_id=%s type=%s name=%s",
        userdata.call_id,
        entity_type,
        name[:80],
    )
    return await upsert_catalog_item_text(
        userdata.business_id,
        entity_type,
        name,
        data,
        description,
    )
