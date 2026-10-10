"""Temporal activities: MCP tool calls, notifications, step recording."""

from __future__ import annotations

import logging
import json
from typing import Any

from temporalio import activity

logger = logging.getLogger(__name__)


def _server_for_tool(tool_name: str) -> str:
    from agent.tools import _CUSTOMER_META_TOOLS

    if tool_name in _CUSTOMER_META_TOOLS or tool_name.startswith("send_"):
        return "meta"
    return "core"


_SEND_PREFIXES = ("send_", "reply_")


def _is_outbound_tool(tool_name: str) -> bool:
    if tool_name in {"mark_as_read", "send_email", "send_sms"}:
        return True
    return tool_name.startswith(_SEND_PREFIXES)


async def _is_personal_recipient(
    business_id: str,
    arguments: dict[str, Any],
) -> bool:
    recipient = str(
        arguments.get("to")
        or arguments.get("phone")
        or arguments.get("to_ig_user_id")
        or arguments.get("to_psid")
        or ""
    ).strip()
    if not recipient:
        return False
    from shared.db import tenant_connection

    async with tenant_connection(business_id) as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT 1
                FROM businesses b
                JOIN customers c ON c.business_id = b.id
                LEFT JOIN identity_map im
                  ON im.business_id = b.id AND im.customer_id = c.id
                WHERE b.id = $1::uuid
                  AND b.smart_number_mode = true
                  AND c.contact_type = 'personal'
                  AND (
                    c.phone = $2
                    OR replace(c.phone, '+', '') = replace($2, '+', '')
                    OR im.external_id = $2
                  )
                LIMIT 1
                """,
                business_id,
                recipient,
            )
        )


@activity.defn
async def execute_tool_activity(
    tool_name: str,
    params: dict[str, Any],
    business_id: str,
) -> dict[str, Any]:
    from agent.tools import call_mcp_tool

    arguments = dict(params or {})
    simulate = bool(
        arguments.pop("simulate_outbound", False) or arguments.pop("skip_send", False)
    )
    arguments.setdefault("business_id", business_id)
    if simulate and _is_outbound_tool(tool_name):
        return {"status": "simulated", "ok": True, "tool": tool_name}
    if (
        _is_outbound_tool(tool_name)
        and await _is_personal_recipient(business_id, arguments)
    ):
        return {
            "status": "skipped_personal_contact",
            "ok": True,
            "tool": tool_name,
        }
    try:
        result = await call_mcp_tool(_server_for_tool(tool_name), tool_name, arguments)
        if isinstance(result, dict):
            return result
        return {"result": result}
    except Exception as exc:
        logger.warning("WF_TOOL_FAIL tool=%s business=%s", tool_name, business_id, exc_info=True)
        return {"ok": False, "error": str(exc)[:300]}


@activity.defn
async def send_notification_activity(
    business_id: str,
    channel: str,
    recipient: str,
    message: str,
    simulate_outbound: bool = False,
) -> dict[str, Any]:
    tool = "send_email" if channel == "email" else f"send_{channel}_message"
    if channel in {"instagram", "instagram_dm"}:
        tool = "send_instagram_dm"
    elif channel in {"facebook", "facebook_messenger"}:
        tool = "send_facebook_message"
    params: dict[str, Any] = {"to": recipient, "text": message, "phone": recipient}
    if simulate_outbound:
        params["simulate_outbound"] = True
    return await execute_tool_activity(
        tool,
        params,
        business_id,
    )


@activity.defn
async def request_workflow_confirmation_activity(
    business_id: str,
    workflow_id: str,
    temporal_instance_id: str,
    summary: str,
    simulate_outbound: bool = False,
) -> dict[str, Any]:
    """Notify the owner and enqueue a FIFO approval for this Temporal run."""
    if simulate_outbound:
        return {"status": "simulated", "approved": True}

    from mcp_servers.reflexo_core.utils.db_helpers import get_business_row
    from shared.redis_client import get_redis
    from shared.redis_keys import workflow_confirmation_key

    business = await get_business_row(business_id)
    owner_phone = str((business or {}).get("owner_phone") or "")
    if not owner_phone:
        raise RuntimeError("Workflow confirmation requires an owner phone number")

    entry = {
        "business_id": business_id,
        "workflow_id": workflow_id,
        "temporal_instance_id": temporal_instance_id,
        "summary": summary[:500],
    }
    redis = get_redis()
    key = workflow_confirmation_key(business_id)
    await redis.rpush(key, json.dumps(entry))
    await redis.expire(key, 15 * 60)

    result = await execute_tool_activity(
        "send_whatsapp_message",
        {
            "to": owner_phone,
            "text": (
                "Approve this workflow run?\n"
                f"{summary[:500]}\n\nReply YES to execute or NO to cancel."
            ),
        },
        business_id,
    )
    if result.get("ok") is False or result.get("error"):
        await redis.lrem(key, 1, json.dumps(entry))
        raise RuntimeError(str(result.get("error") or "Owner confirmation send failed"))
    return {"status": "pending", "approved": None}


@activity.defn
async def clear_workflow_confirmation_activity(
    business_id: str,
    temporal_instance_id: str,
) -> None:
    """Remove a timed-out/cancelled approval from the business FIFO."""
    from shared.redis_client import get_redis
    from shared.redis_keys import workflow_confirmation_key

    redis = get_redis()
    key = workflow_confirmation_key(business_id)
    entries = await redis.lrange(key, 0, -1)
    for raw in entries or []:
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            entry = json.loads(text)
        except json.JSONDecodeError:
            continue
        if str(entry.get("temporal_instance_id")) == temporal_instance_id:
            await redis.lrem(key, 1, text)
            break


@activity.defn
async def start_scheduled_run_activity(
    business_id: str,
    workflow_id: str,
    trigger_event: dict[str, Any],
    temporal_instance_id: str,
) -> str:
    """Create the DB run row for a Temporal Schedule-started execution."""
    from workflow_builder.store import insert_run

    run = await insert_run(
        business_id,
        workflow_id,
        trigger_event=trigger_event,
        temporal_instance_id=temporal_instance_id,
        status="running",
    )
    return str(run.get("id") or "")


@activity.defn
async def finish_workflow_run_activity(
    business_id: str,
    run_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    if not run_id or run_id == "probe":
        return
    from workflow_builder.store import finish_run

    await finish_run(
        business_id,
        run_id,
        status=status,
        error_message=error_message,
    )


@activity.defn
async def record_workflow_step(
    business_id: str,
    run_id: str,
    step_name: str,
    step_type: str,
    status: str,
    result: dict[str, Any] | None,
    latency_ms: int,
) -> None:
    if not run_id or run_id == "probe":
        return
    from workflow_builder.store import append_step, finish_run

    await append_step(
        business_id,
        run_id,
        {
            "name": step_name,
            "type": step_type,
            "status": status,
            "result": result,
            "latency_ms": latency_ms,
        },
    )
    if status == "failed":
        await finish_run(business_id, run_id, status="failed", error_message=step_name)
