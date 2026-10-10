"""Compatibility layer: legacy workflow-rule tools write Temporal definitions.

There is intentionally no second rule executor. Existing tool names remain so
older prompts and clients continue to work, but every new automation is stored
in ``ai_workflows`` and must pass the Studio scenario test before activation.
"""

from __future__ import annotations

from typing import Any

from workflow_builder.activation import (
    activate_tested_workflow,
    pause_workflow_execution,
)
from workflow_builder.catalog import available_tools_for_business
from workflow_builder.deployer import deploy_workflow
from workflow_builder.models import WorkflowDefinition, WorkflowStep, StepType
from workflow_builder.store import (
    get_workflow,
    list_workflows,
    update_workflow_definition,
)
from workflow_builder.validator import validate_workflow


def _inject_recipient(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Fill channel recipient fields from the triggering event when omitted."""
    resolved = dict(params)
    if tool.startswith("send_whatsapp_"):
        resolved.setdefault("to", "{{external_sender_id}}")
    elif tool == "send_instagram_dm":
        resolved.setdefault("to_ig_user_id", "{{external_sender_id}}")
    elif tool == "send_facebook_message":
        resolved.setdefault("to_psid", "{{external_sender_id}}")
    elif tool in {"reply_instagram_comment", "reply_facebook_comment"}:
        resolved.setdefault("comment_id", "{{comment_id}}")
    elif tool == "send_email":
        resolved.setdefault("to", "{{external_sender_id}}")
    elif tool == "send_sms":
        resolved.setdefault("phone", "{{external_sender_id}}")
    return resolved


def compile_rule_definition(
    *,
    name: str,
    trigger_type: str,
    action_sequence: list[dict[str, Any]],
    description: str | None = None,
    trigger_config: dict[str, Any] | None = None,
    conditions: list[dict[str, Any]] | None = None,
    requires_owner_confirmation: bool = False,
    max_executions_per_day: int = 1000,
) -> WorkflowDefinition:
    """Translate a sequential legacy rule into CALL_TOOL Temporal steps."""
    steps: list[WorkflowStep] = []
    previous: str | None = None
    for index, action in enumerate(action_sequence, start=1):
        tool = str(action.get("tool") or "").strip()
        if not tool:
            raise ValueError(f"Action {index} is missing a tool")
        step_name = f"action_{index}_{tool}"[:120]
        raw_params = action.get("params")
        params = _inject_recipient(
            tool,
            dict(raw_params)
            if isinstance(raw_params, dict)
            else {key: value for key, value in action.items() if key != "tool"},
        )
        steps.append(
            WorkflowStep(
                name=step_name,
                type=StepType.CALL_TOOL,
                tool=tool,
                params=params,
                depends_on=[previous] if previous else [],
            )
        )
        previous = step_name
    return WorkflowDefinition(
        name=name,
        description=description or "",
        nl_description=description or name,
        trigger_type=trigger_type,
        trigger_config=trigger_config or {},
        instance_key="{{customer.id}}",
        conditions=conditions or [],
        steps=steps,
        requires_owner_confirmation=requires_owner_confirmation,
        max_executions_per_day=max_executions_per_day,
    )


async def create_temporal_rule(
    business_id: str,
    **rule: Any,
) -> dict[str, Any]:
    definition = compile_rule_definition(**rule)
    tools = await available_tools_for_business(business_id)
    validation = await validate_workflow(definition, tools, business_id)
    if not validation.is_valid:
        return {
            "status": "invalid",
            "errors": validation.errors,
            "warnings": validation.warnings,
        }
    row = await deploy_workflow(
        business_id,
        definition,
        nl_description=definition.nl_description,
    )
    return {
        "id": str(row["id"]),
        "status": "draft_created",
        "name": row.get("name") or definition.name,
        "engine": "temporal",
        "is_active": False,
        "next_step": "Open Workflow Studio, run the stateful test, then activate.",
    }


async def list_temporal_rules(
    business_id: str,
    *,
    trigger_type: str | None = None,
    is_active: bool | None = True,
) -> dict[str, Any]:
    rows = await list_workflows(business_id, active_only=is_active is True)
    rows = [
        row
        for row in rows
        if (trigger_type is None or str(row.get("trigger_type")) == trigger_type)
        and (is_active is None or bool(row.get("is_active")) is is_active)
    ]
    return {"rules": rows, "total_count": len(rows), "engine": "temporal"}


def _definition_from_row(row: dict[str, Any]) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            key: row.get(key)
            for key in WorkflowDefinition.model_fields
            if row.get(key) is not None
        }
    )


async def update_temporal_rule(
    business_id: str,
    workflow_id: str,
    updates: dict[str, Any],
) -> dict[str, Any] | None:
    row = await get_workflow(business_id, workflow_id)
    if not row:
        return None

    active_requested = updates.pop("is_active", None)
    definition = _definition_from_row(row)
    definition_updates: dict[str, Any] = {}
    for key in (
        "name",
        "description",
        "trigger_config",
        "conditions",
        "requires_owner_confirmation",
        "max_executions_per_day",
    ):
        if key in updates:
            definition_updates[key] = updates[key]
    if "action_sequence" in updates:
        compiled = compile_rule_definition(
            name=definition_updates.get("name", definition.name),
            description=definition_updates.get("description", definition.description),
            trigger_type=definition.trigger_type,
            trigger_config=definition_updates.get(
                "trigger_config", definition.trigger_config
            ),
            conditions=definition_updates.get("conditions", definition.conditions),
            action_sequence=updates["action_sequence"],
            requires_owner_confirmation=definition_updates.get(
                "requires_owner_confirmation",
                definition.requires_owner_confirmation,
            ),
            max_executions_per_day=definition_updates.get(
                "max_executions_per_day",
                definition.max_executions_per_day,
            ),
        )
        definition_updates["steps"] = compiled.steps

    if definition_updates:
        definition = definition.model_copy(update=definition_updates)
        tools = await available_tools_for_business(business_id)
        validation = await validate_workflow(definition, tools, business_id)
        if not validation.is_valid:
            return {"status": "invalid", "errors": validation.errors}
        row = await update_workflow_definition(business_id, workflow_id, definition)
    if active_requested is not None:
        row = (
            await activate_tested_workflow(business_id, workflow_id)
            if bool(active_requested)
            else await pause_workflow_execution(business_id, workflow_id)
        )
    return row


async def temporal_rule_performance(
    business_id: str,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    rows = (
        [await get_workflow(business_id, workflow_id)]
        if workflow_id
        else await list_workflows(business_id)
    )
    results = []
    for row in rows:
        if not row:
            continue
        results.append(
            {
                "id": row["id"],
                "name": row.get("name"),
                "total_executions": row.get("total_runs", 0),
                "successful_executions": row.get("successful_runs", 0),
                "failed_executions": row.get("failed_runs", 0),
                "avg_latency_ms": row.get("avg_latency_ms"),
                "last_executed_at": row.get("last_run_at"),
                "is_active": row.get("is_active"),
                "engine": "temporal",
            }
        )
    return {"rules": results, "engine": "temporal"}
