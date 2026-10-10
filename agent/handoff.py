"""Inter-agent handoff protocol (MULTI_AGENT_SPEC §10)."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent.config import MULTI_AGENT_CONFIG
from agent.registry import CUSTOMER_SPECIALISTS, get_agent_definition

logger = logging.getLogger(__name__)

HANDOFF_TARGETS = ("inquiry_agent", "sales_agent", "support_agent")


class HandoffArgs(BaseModel):
    target_agent: str = Field(description="Specialist to transfer to")
    reason: str = Field(description="Brief reason for the handoff")


def build_handoff_tool() -> StructuredTool:
    async def _handoff(target_agent: str, reason: str) -> str:
        payload = {
            "__handoff__": True,
            "target_agent": target_agent,
            "reason": reason,
        }
        return json.dumps(payload)

    return StructuredTool.from_function(
        coroutine=_handoff,
        name="handoff_to_agent",
        description=(
            "Transfer this conversation to another specialist with matching "
            "expertise. Available targets: inquiry_agent, sales_agent, "
            "support_agent. Include a brief reason for the handoff."
        ),
        args_schema=HandoffArgs,
    )


def parse_handoff_from_tool_results(tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(tool_results or []):
        if str(entry.get("tool_name") or "") != "handoff_to_agent":
            continue
        if entry.get("__handoff__"):
            return {
                "target_agent": entry.get("target_agent"),
                "reason": entry.get("reason") or "",
            }
        raw = entry.get("raw")
        if isinstance(raw, str) and "__handoff__" in raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if parsed.get("__handoff__"):
                return {
                    "target_agent": parsed.get("target_agent"),
                    "reason": parsed.get("reason") or "",
                }
        if isinstance(entry.get("target_agent"), str):
            return {
                "target_agent": entry["target_agent"],
                "reason": str(entry.get("reason") or ""),
            }
    return None


def resolve_handoff_target(
    requested: str | None,
    *,
    current_agent: str,
    hop_index: int,
) -> str:
    """
    hop_index is the number of handoffs already completed (0 = first request).
    Max 2 handoffs per message; extra hops go to support_agent.
    """
    fallback = str(MULTI_AGENT_CONFIG["handoff_fallback_agent"])
    max_hops = int(MULTI_AGENT_CONFIG["max_handoffs_per_message"])
    if hop_index >= max_hops:
        logger.info("Handoff cap reached; forcing %s", fallback)
        return fallback

    name = (requested or "").strip()
    if name in ("inquiry", "sales", "support"):
        name = f"{name}_agent"
    if name not in HANDOFF_TARGETS:
        logger.warning("Invalid handoff target %r; using %s", requested, fallback)
        return fallback
    if name == current_agent:
        return fallback if current_agent != fallback else "inquiry_agent"
    try:
        get_agent_definition(name)
    except KeyError:
        return fallback
    if name not in CUSTOMER_SPECIALISTS:
        return fallback
    return name
