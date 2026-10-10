"""Standard MCP tool error formatting."""

from __future__ import annotations

from typing import Any


ERROR_GUIDANCE: dict[str, str] = {
    "BUSINESS_NOT_FOUND": "The business_id may be incorrect. Check the session context.",
    "ENTRY_NOT_FOUND": "The ID was not found for this business. List or search again.",
    "EMPTY_QUERY": "Provide a non-empty search query.",
    "EMPTY_ANSWER": "Provide a non-empty answer before adding knowledge.",
    "DUPLICATE_DETECTED": "A very similar entry already exists. Update it instead of creating a new one.",
    "DUPLICATE_NAME": "An entity with this name and type already exists. Pass entity_id to update.",
    "FIELD_NOT_ALLOWED": "That field cannot be updated. Use an allowed configuration field.",
    "INVALID_TRIGGER": "Use a valid workflow_trigger enum value.",
    "INVALID_ACTION": "Each action needs a known tool name.",
    "INVALID_ENTITY_TYPE": "Use a valid entity_type enum value.",
    "RATE_LIMITED": "Slow down and retry shortly.",
    "MESSAGE_LIMIT_REACHED": "The business has hit its plan message limit.",
    "MEDIA_DOWNLOAD_UNAVAILABLE": "media_id download requires Meta MCP. Use media_url instead for now.",
    "INVALID_INPUT": "Check the tool arguments and try again.",
}


def tool_error(code: str, message: str, agent_guidance: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "agent_guidance": agent_guidance or ERROR_GUIDANCE.get(code, "Fix the input and retry."),
        }
    }
