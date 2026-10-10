"""Natural-language edits for owner Workflow Studio sessions."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, ConfigDict

from workflow_builder.catalog import available_tools_for_business
from workflow_builder.models import WorkflowDefinition, WorkflowStep
from workflow_builder.store import get_workflow, update_workflow_definition
from workflow_builder.validator import validate_workflow

logger = logging.getLogger(__name__)

EDITOR_PROMPT = """You edit an existing Temporal business workflow.

Return a WorkflowEditPatch containing ONLY fields the owner explicitly asked to
change. Omit every unrelated field. If changing steps, return the complete new
steps list while preserving every unrelated step, dependency and template.
Never add a tool outside AVAILABLE TOOLS. Do not activate or test the workflow.

AVAILABLE TOOLS:
{available_tools}

CURRENT DEFINITION:
{current_definition}
"""


class WorkflowEditPatch(BaseModel):
    """Sparse patch; fields omitted by the model are preserved exactly."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] | None = None
    steps: list[WorkflowStep] | None = None
    response_template: str | None = None
    instance_key: str | None = None
    max_duration_seconds: int | None = None
    continue_as_new: bool | None = None
    requires_owner_confirmation: bool | None = None
    max_executions_per_day: int | None = None


def definition_from_row(row: dict[str, Any]) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            key: row.get(key)
            for key in WorkflowDefinition.model_fields
            if row.get(key) is not None
        }
    )


async def apply_workflow_edit(
    *,
    business_id: str,
    workflow_id: str,
    instruction: str,
) -> dict[str, Any]:
    """Apply, validate, and persist one owner edit as a new inactive draft."""
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("Edit instruction is required")
    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise ValueError("Workflow not found")
    current = definition_from_row(workflow)
    tools = await available_tools_for_business(business_id)
    listing = "\n".join(f"- {tool['name']}" for tool in tools) or "(none)"

    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    llm = ChatGoogleGenerativeAI(
        model=settings.agent_model or "gemini-2.5-flash",
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    structured = llm.with_structured_output(WorkflowEditPatch)
    patch = await structured.ainvoke(
        [
            SystemMessage(
                content=EDITOR_PROMPT.format(
                    available_tools=listing,
                    current_definition=json.dumps(
                        current.model_dump(mode="json"),
                        ensure_ascii=False,
                        default=str,
                    ),
                )
            ),
            HumanMessage(content=instruction),
        ]
    )
    if not isinstance(patch, WorkflowEditPatch):
        patch = WorkflowEditPatch.model_validate(patch)
    # Gemini structured output can materialize omitted optionals as explicit
    # nulls. Null is not an edit instruction; preserve the existing value.
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        return {
            "updated": False,
            "errors": ["The edit did not specify any workflow changes"],
            "warnings": [],
        }
    merged = current.model_dump(mode="json")
    merged.update(changes)
    edited = WorkflowDefinition.model_validate(merged)
    validation = await validate_workflow(edited, tools, business_id)
    if not validation.is_valid:
        return {
            "updated": False,
            "errors": validation.errors,
            "warnings": validation.warnings,
        }
    row = await update_workflow_definition(business_id, workflow_id, edited)
    if not row:
        raise ValueError("Workflow not found")
    logger.info(
        "WF_STUDIO_EDIT business=%s workflow=%s instruction_chars=%s",
        business_id,
        workflow_id,
        len(instruction),
    )
    return {
        "updated": True,
        "workflow": row,
        "definition": edited.model_dump(mode="json"),
        "warnings": validation.warnings,
    }
