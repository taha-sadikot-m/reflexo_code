"""Workflow rule tools."""

from __future__ import annotations

from typing import Any

from mcp_servers.reflexo_core.utils.db_helpers import business_exists
from mcp_servers.reflexo_core.utils.errors import tool_error
from workflow_builder.legacy_rules import (
    create_temporal_rule,
    list_temporal_rules,
    temporal_rule_performance,
    update_temporal_rule,
)
from workflow_builder.activation import pause_workflow_execution
from workflow_builder.store import WorkflowTestRequired

VALID_TRIGGERS = {
    "whatsapp_message",
    "instagram_dm",
    "instagram_comment",
    "facebook_message",
    "facebook_comment",
    "email_received",
    "slack_message",
    "voice_call",
    "payment_received",
    "payment_failed",
    "order_created",
    "schedule",
    "manual",
}


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def create_workflow_rule(
        business_id: str,
        name: str,
        trigger_type: str,
        action_sequence: list[dict],
        description: str | None = None,
        trigger_config: dict | None = None,
        # A bare `list` yields a schema without `items`, which Gemini rejects.
        conditions: list[dict] | None = None,
        requires_owner_confirmation: bool = False,
        max_executions_per_day: int = 1000,
    ) -> dict[str, Any]:
        """Create an inactive Temporal workflow draft.

        The legacy tool name remains for compatibility; there is no separate
        workflow_rules execution engine.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if trigger_type not in VALID_TRIGGERS:
            return tool_error("INVALID_TRIGGER", f"Invalid trigger_type: {trigger_type}")
        if not action_sequence:
            return tool_error("INVALID_ACTION", "action_sequence cannot be empty")
        for action in action_sequence:
            if not isinstance(action, dict) or "tool" not in action:
                return tool_error("INVALID_ACTION", "Each action must have a 'tool' field")

        return await create_temporal_rule(
            business_id,
            name=name,
            description=description,
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            conditions=conditions,
            action_sequence=action_sequence,
            requires_owner_confirmation=requires_owner_confirmation,
            max_executions_per_day=max_executions_per_day,
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def list_workflow_rules(
        business_id: str,
        trigger_type: str | None = None,
        is_active: bool | None = True,
    ) -> dict[str, Any]:
        """List canonical Temporal workflows for a business."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        return await list_temporal_rules(
            business_id,
            trigger_type=trigger_type,
            is_active=is_active,
        )

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def update_workflow_rule(
        business_id: str,
        rule_id: str,
        updates: dict,
    ) -> dict[str, Any]:
        """Update a Temporal workflow; definition changes reset its test gate."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        allowed = {
            "name",
            "description",
            "trigger_config",
            "conditions",
            "action_sequence",
            "is_active",
            "requires_owner_confirmation",
            "max_executions_per_day",
        }
        bad = set(updates.keys()) - allowed
        if bad:
            return tool_error("FIELD_NOT_ALLOWED", f"Cannot update fields: {sorted(bad)}")

        try:
            row = await update_temporal_rule(business_id, rule_id, dict(updates))
        except WorkflowTestRequired as exc:
            return tool_error("WORKFLOW_TEST_REQUIRED", str(exc))
        if not row:
            return tool_error("ENTRY_NOT_FOUND", f"Temporal workflow {rule_id} not found")
        if row.get("status") == "invalid":
            return tool_error(
                "INVALID_WORKFLOW",
                "; ".join(str(error) for error in row.get("errors") or []),
            )
        return {"id": rule_id, "status": "updated", "workflow": row, "engine": "temporal"}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def delete_workflow_rule(business_id: str, rule_id: str) -> dict[str, Any]:
        """Deactivate a Temporal workflow (soft delete)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            row = await pause_workflow_execution(business_id, rule_id)
        except ValueError:
            return tool_error("ENTRY_NOT_FOUND", f"Temporal workflow {rule_id} not found")
        return {"id": rule_id, "status": "deactivated", "engine": "temporal"}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_workflow_performance(
        business_id: str,
        rule_id: str | None = None,
    ) -> dict[str, Any]:
        """Get execution stats for canonical Temporal workflows."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        result = await temporal_rule_performance(business_id, rule_id)
        if rule_id and not result["rules"]:
            return tool_error("ENTRY_NOT_FOUND", f"Temporal workflow {rule_id} not found")
        return result
