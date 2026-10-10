"""Activation policy for tested AI workflows."""

from __future__ import annotations

from typing import Any

from workflow_builder.store import WorkflowTestRequired, get_workflow, set_active


async def activate_tested_workflow(
    business_id: str,
    workflow_id: str,
    *,
    shadow_mode: bool = False,
    shadow_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise ValueError("Workflow not found")
    results = (workflow.get("metadata") or {}).get("test_results") or {}
    if not workflow.get("is_tested") or not results.get("all_passed"):
        raise WorkflowTestRequired(
            "Tests must pass before activation. Run the stateful scenario test first."
        )
    row = await set_active(
        business_id,
        workflow_id,
        True,
        shadow_mode=shadow_mode,
        shadow_allowlist=shadow_allowlist or [],
    )
    if not row:
        raise ValueError("Workflow not found")
    if str(row.get("trigger_type")) == "schedule":
        from workflow_builder.deployer import ensure_temporal_schedule

        try:
            await ensure_temporal_schedule(business_id, row)
        except Exception:
            await set_active(business_id, workflow_id, False)
            raise
    await _notify_owner_activation(
        business_id,
        str(row.get("name") or workflow.get("name") or "Workflow"),
        shadow_mode=shadow_mode,
    )
    return row


async def pause_workflow_execution(
    business_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Pause DB routing and its Temporal Schedule as one fail-safe operation."""
    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise ValueError("Workflow not found")
    if str(workflow.get("trigger_type")) == "schedule":
        from workflow_builder.deployer import delete_temporal_schedule

        # Delete first: if Postgres then fails, the safer state is no schedule.
        await delete_temporal_schedule(workflow_id)
    row = await set_active(business_id, workflow_id, False)
    if not row:
        raise ValueError("Workflow not found")
    return row


async def resume_workflow_execution(
    business_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Resume through the tested activation gate and recreate schedules."""
    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise ValueError("Workflow not found")
    return await activate_tested_workflow(
        business_id,
        workflow_id,
        shadow_mode=bool(workflow.get("shadow_mode")),
        shadow_allowlist=list(workflow.get("shadow_allowlist") or []),
    )


async def _notify_owner_activation(
    business_id: str,
    workflow_name: str,
    *,
    shadow_mode: bool,
) -> None:
    """Best-effort activation confirmation on the owner's WhatsApp."""
    try:
        from agent.tools import call_mcp_tool
        from mcp_servers.reflexo_core.utils.db_helpers import get_business_row

        business = await get_business_row(business_id)
        owner_phone = str((business or {}).get("owner_phone") or "")
        if not owner_phone:
            return
        state = (
            "live in shadow mode for allowlisted testers"
            if shadow_mode
            else "live for matching customers"
        )
        await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {
                "business_id": business_id,
                "to": owner_phone,
                "text": f"Workflow '{workflow_name}' is now {state}.",
            },
        )
    except Exception:
        # Activation is committed; a notification outage must not roll it back.
        return
