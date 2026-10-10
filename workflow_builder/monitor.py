"""Execution health: auto-pause workflows with a high recent failure rate."""

from __future__ import annotations

import logging
from typing import Any

from workflow_builder.store import auto_pause, list_workflows, runs_last_24h

logger = logging.getLogger(__name__)

FAILURE_RATE_THRESHOLD = 0.20
MIN_RUNS = 3


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    failed = sum(1 for r in rows if r.get("status") == "failed")
    rate = (failed / total) if total else 0.0
    return {"total": total, "failed": failed, "failure_rate": rate}


async def workflow_health(business_id: str, workflow_id: str) -> dict[str, Any]:
    rows = await runs_last_24h(business_id, workflow_id)
    stats = _stats(rows)
    return {"workflow_id": workflow_id, **stats, "window": "24h"}


async def scan_and_auto_pause(business_id: str) -> list[str]:
    paused: list[str] = []
    workflows = await list_workflows(business_id, active_only=True)
    for wf in workflows:
        wf_id = str(wf["id"])
        stats = _stats(await runs_last_24h(business_id, wf_id))
        if stats["total"] >= MIN_RUNS and stats["failure_rate"] > FAILURE_RATE_THRESHOLD:
            reason = (
                f"Auto-paused: {stats['failed']}/{stats['total']} runs failed "
                f"in 24h ({stats['failure_rate']:.0%})"
            )
            if str(wf.get("trigger_type")) == "schedule":
                from workflow_builder.deployer import delete_temporal_schedule

                await delete_temporal_schedule(wf_id)
            await auto_pause(business_id, wf_id, reason=reason)
            paused.append(wf_id)
            logger.warning("WF_AUTO_PAUSE business=%s workflow=%s %s", business_id, wf_id, reason)
            try:
                await _notify_owner(business_id, wf.get("name") or wf_id, reason)
            except Exception:
                logger.debug("WF_AUTO_PAUSE notify skipped", exc_info=True)
    return paused


async def _notify_owner(business_id: str, workflow_name: str, reason: str) -> None:
    from agent.tools import call_mcp_tool

    await call_mcp_tool(
        "core",
        "escalate_to_owner",
        {
            "business_id": business_id,
            "reason": f"Workflow '{workflow_name}' was auto-paused. {reason}",
        },
    )
