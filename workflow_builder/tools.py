"""Owner-facing LangChain tools for the AI workflow builder."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from workflow_builder.catalog import available_tools_for_business
from workflow_builder.activation import (
    activate_tested_workflow,
    pause_workflow_execution,
    resume_workflow_execution,
)
from workflow_builder.decomposer import decompose_workflow
from workflow_builder.deployer import deploy_workflow
from workflow_builder.models import WorkflowDefinition
from workflow_builder.monitor import scan_and_auto_pause, workflow_health
from workflow_builder.owner_test import run_owner_facing_test
from workflow_builder.simulator import simulate_workflow
from workflow_builder.store import get_workflow, list_workflows
from workflow_builder.studio import regenerate_studio_link
from workflow_builder.validator import validate_workflow

BUILDER_TOOL_NAMES = (
    "build_workflow",
    "test_workflow",
    "deploy_workflow",
    "run_workflow_scenario_test",
    "activate_workflow",
    "generate_workflow_studio_link",
    "list_active_workflows",
    "pause_workflow",
    "resume_workflow",
    "get_workflow_health",
)


class _BuildArgs(BaseModel):
    description: str = Field(description="Natural language workflow description")


class _TestArgs(BaseModel):
    definition: dict[str, Any] = Field(description="Workflow definition JSON from build_workflow")


class _DeployArgs(BaseModel):
    definition: dict[str, Any]
    nl_description: str = ""


class _IdArgs(BaseModel):
    workflow_id: str


def _dump(definition: WorkflowDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json")


def bind_builder_tools(
    business_id: str,
    *,
    owner_conversation_id: str | None = None,
) -> list[StructuredTool]:
    async def build_workflow(description: str) -> dict[str, Any]:
        tools = await available_tools_for_business(business_id)
        definition = await decompose_workflow(
            description, business_id=business_id, available_tools=tools
        )
        validation = await validate_workflow(definition, tools, business_id)
        return {
            "definition": _dump(definition),
            "valid": validation.is_valid,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "persisted": False,
        }

    async def test_workflow(definition: dict[str, Any]) -> dict[str, Any]:
        parsed = WorkflowDefinition.model_validate(definition)
        return await simulate_workflow(parsed, business_id=business_id)

    async def deploy_workflow_tool(definition: dict[str, Any], nl_description: str = "") -> dict[str, Any]:
        parsed = WorkflowDefinition.model_validate(definition)
        tools = await available_tools_for_business(business_id)
        validation = await validate_workflow(parsed, tools, business_id)
        if not validation.is_valid:
            return {"deployed": False, "errors": validation.errors, "warnings": validation.warnings}
        simulation = await simulate_workflow(parsed, business_id=business_id)
        row = await deploy_workflow(
            business_id,
            parsed,
            nl_description=nl_description or parsed.nl_description,
            simulation=simulation,
        )
        return {
            "deployed": False,
            "draft_created": True,
            "workflow": row,
            "warnings": validation.warnings,
            "next_step": "Run run_workflow_scenario_test, then activate_workflow.",
        }

    async def run_workflow_scenario_test(workflow_id: str) -> dict[str, Any]:
        # Definition-only: the agent already answered this message, so replaying
        # the full pipeline per synthetic event would only add minutes of LLM work.
        return await run_owner_facing_test(
            business_id=business_id,
            workflow_id=workflow_id,
            run_agent_pipeline=False,
        )

    async def activate_workflow(workflow_id: str) -> dict[str, Any]:
        row = await activate_tested_workflow(business_id, workflow_id)
        return {"activated": True, "workflow": row}

    async def generate_workflow_studio_link(workflow_id: str) -> dict[str, Any]:
        from api.settings import get_api_settings

        session = await regenerate_studio_link(
            business_id=business_id,
            workflow_id=workflow_id,
            owner_conversation_id=owner_conversation_id,
        )
        base = get_api_settings().admin_frontend_origin.rstrip("/")
        return {
            "url": f"{base}/wf/{session['token']}",
            "expires_at": session["expires_at"],
            "workflow_id": workflow_id,
        }

    async def list_active_workflows() -> dict[str, Any]:
        items = await list_workflows(business_id, active_only=True)
        return {"items": items}

    async def pause_workflow(workflow_id: str) -> dict[str, Any]:
        row = await pause_workflow_execution(business_id, workflow_id)
        return {"ok": bool(row), "workflow": row}

    async def resume_workflow(workflow_id: str) -> dict[str, Any]:
        row = await resume_workflow_execution(business_id, workflow_id)
        return {"ok": bool(row), "workflow": row}

    async def get_workflow_health_tool(workflow_id: str) -> dict[str, Any]:
        await scan_and_auto_pause(business_id)
        row = await get_workflow(business_id, workflow_id)
        health = await workflow_health(business_id, workflow_id)
        return {"workflow": row, "health": health}

    return [
        StructuredTool.from_function(coroutine=build_workflow, name="build_workflow", description="Turn a natural-language automation into a structured workflow preview. Does not deploy.", args_schema=_BuildArgs),
        StructuredTool.from_function(coroutine=test_workflow, name="test_workflow", description="Dry-run a workflow definition with no side effects.", args_schema=_TestArgs),
        StructuredTool.from_function(coroutine=deploy_workflow_tool, name="deploy_workflow", description="Validate and save an AI-built workflow as an inactive draft. A stateful scenario test must pass before activation.", args_schema=_DeployArgs),
        StructuredTool.from_function(coroutine=run_workflow_scenario_test, name="run_workflow_scenario_test", description="Auto-select and run a stateful Temporal scenario test with simulated outbound, then save owner-readable results.", args_schema=_IdArgs),
        StructuredTool.from_function(coroutine=activate_workflow, name="activate_workflow", description="Activate an AI workflow only after its stateful scenario test passed.", args_schema=_IdArgs),
        StructuredTool.from_function(coroutine=generate_workflow_studio_link, name="generate_workflow_studio_link", description="Create a 48-hour owner link for visual review, stateful testing, and activation.", args_schema=_IdArgs),
        StructuredTool.from_function(coroutine=list_active_workflows, name="list_active_workflows", description="List AI-built workflows for this business."),
        StructuredTool.from_function(coroutine=pause_workflow, name="pause_workflow", description="Deactivate an AI-built workflow so events no longer start it.", args_schema=_IdArgs),
        StructuredTool.from_function(coroutine=resume_workflow, name="resume_workflow", description="Re-activate a paused AI-built workflow.", args_schema=_IdArgs),
        StructuredTool.from_function(coroutine=get_workflow_health_tool, name="get_workflow_health", description="24h execution stats for an AI-built workflow. May auto-pause failing workflows.", args_schema=_IdArgs),
    ]
