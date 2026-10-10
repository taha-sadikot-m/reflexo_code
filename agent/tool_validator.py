"""Pydantic schemas for critical MCP tool args (MVP Stage 2 §1.2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mcp_servers.reflexo_core.tools.entities import VALID_ENTITY_TYPES

VALIDATION_MAX_RETRIES = 2


class UpsertEntityArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    business_id: str
    entity_type: str
    name: str
    data: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    entity_id: str | None = None

    @field_validator("entity_type")
    @classmethod
    def _entity_type_allowed(cls, value: str) -> str:
        if value not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"Unknown entity_type: {value}. "
                f"Use one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"
            )
        return value

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("name must be a non-empty string")
        return stripped


class AddKnowledgeArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    business_id: str
    answer: str
    question: str | None = None
    category: str = "general"
    source: str = "owner"

    @field_validator("answer")
    @classmethod
    def _answer_nonempty(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("answer must be a non-empty string")
        return stripped


TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "upsert_entity": UpsertEntityArgs,
    "add_knowledge": AddKnowledgeArgs,
}


def validation_error_payload(tool_name: str, detail: str) -> dict[str, Any]:
    return {
        "error": {
            "code": "TOOL_VALIDATION",
            "message": detail,
            "agent_guidance": (
                f"Arguments for {tool_name} are invalid. Fix them and retry "
                f"(at most {VALIDATION_MAX_RETRIES} retries). Do not call the "
                "tool again with the same invalid values."
            ),
        }
    }


def validate_tool_args(
    name: str,
    args: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (cleaned_args, None) or (None, error_dict). Unknown tools pass through."""
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return dict(args), None
    try:
        validated = schema(**args)
        return validated.model_dump(), None
    except ValidationError as exc:
        return None, validation_error_payload(name, str(exc))
