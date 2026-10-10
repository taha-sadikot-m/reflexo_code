"""HITL gates for owner destructive tools via LangGraph interrupt()."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langgraph.types import interrupt

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

TIER_AUTO = {
    "search_knowledge",
    "search_entities",
    "get_entity",
    "get_business_config",
    "get_business_hours",
    "get_dynamic_data",
    "list_workflow_rules",
    "get_workflow_performance",
    "list_active_workflows",
    "get_workflow_health",
    "build_workflow",
    "test_workflow",
    "get_analytics",
    "get_daily_summary",
    "get_recent_conversations",
    "search_customers",
    "get_customer",
    "list_entities",
    "log_gap",
}

TIER_CONFIRM = {
    "delete_knowledge",
    "delete_entity",
    "update_business_info",
    "create_workflow_rule",
    "update_workflow_rule",
    "delete_workflow_rule",
    "deploy_workflow",
    "pause_workflow",
    "resume_workflow",
}

TIER_BLOCK = {
    "bulk_delete_entities",
    "reset_knowledge_base",
}

HITL_APPROVE_ID = "hitl_approve"
HITL_DECLINE_ID = "hitl_decline"
HITL_PENDING_TTL_S = 15 * 60
HITL_MARKER = "__hitl__"

HITL_REMINDER_SUFFIX = (
    "Reply YES to proceed or NO to cancel. I will not start a new change until then."
)

_APPROVE_RE = re.compile(
    r"^\s*(yes|y|ok|okay|confirm|confirmed|approve|approved|proceed|do it|"
    r"हाँ|हां|जी|हाँ जी)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(
    r"^\s*(no|n|nope|cancel|cancelled|canceled|decline|declined|stop|"
    r"नहीं|नही|ना)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_HIDDEN_ARG_KEYS = frozenset({"business_id", "access_token", "token", "api_key"})


def hitl_pending_key(business_id: str, channel: str, sender_id: str) -> str:
    return f"hitl:pending:{business_id}:{channel}:{sender_id}"


def format_confirmation_preview(tool_name: str, tool_args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in (tool_args or {}).items():
        if key in _HIDDEN_ARG_KEYS:
            continue
        text = str(value)
        if len(text) > 80:
            text = text[:79] + "…"
        parts.append(f"{key}={text}")
    detail = ", ".join(parts) if parts else "(no extra details)"
    return f"{tool_name} ({detail})"


def maybe_confirm(tool_name: str, tool_args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return None to execute, a cancelled/blocked dict, or interrupt until resume."""
    args = dict(tool_args or {})
    if tool_name in TIER_AUTO:
        return None
    if tool_name in TIER_BLOCK:
        return {
            "status": "blocked",
            "reason": "This action requires manual execution",
            "tool": tool_name,
        }
    if tool_name not in TIER_CONFIRM:
        return None

    preview = format_confirmation_preview(tool_name, args)
    response = interrupt(
        {
            "type": "confirmation_required",
            "tool": tool_name,
            "args": {k: v for k, v in args.items() if k not in _HIDDEN_ARG_KEYS},
            "preview": preview,
            "message": f"Should I proceed with: {preview}?",
        }
    )
    approved = False
    if isinstance(response, dict):
        approved = bool(response.get("approved"))
    elif response is True:
        approved = True
    if approved:
        return None
    return {
        "status": "cancelled",
        "reason": "Owner declined",
        "tool": tool_name,
        "preview": preview,
    }


def interrupt_payload_from_result(result: Any) -> dict[str, Any] | None:
    """Pull LangGraph interrupt value from ainvoke output."""
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value") or first
    if isinstance(value, dict):
        return value
    if value is None:
        return {"type": "confirmation_required", "message": "Please confirm this action."}
    return {"type": "confirmation_required", "message": str(value), "preview": str(value)}


def parse_hitl_reply(message_text: str, *, reply_id: str | None = None) -> bool | None:
    """True=approve, False=decline, None=not a confirmation reply."""
    if reply_id == HITL_APPROVE_ID:
        return True
    if reply_id == HITL_DECLINE_ID:
        return False
    text = (message_text or "").strip()
    if not text:
        return None
    if _APPROVE_RE.match(text) or text.lower() in {"confirm", "yes"}:
        return True
    if _DECLINE_RE.match(text) or text.lower() in {"cancel", "no"}:
        return False
    return None


async def get_pending_hitl(
    business_id: str, channel: str, sender_id: str
) -> dict[str, Any] | None:
    try:
        raw = await get_redis().get(hitl_pending_key(business_id, channel, sender_id))
    except Exception as exc:
        logger.warning("HITL pending read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def set_pending_hitl(
    business_id: str,
    channel: str,
    sender_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        await get_redis().set(
            hitl_pending_key(business_id, channel, sender_id),
            json.dumps(payload, default=str),
            ex=HITL_PENDING_TTL_S,
        )
    except Exception as exc:
        logger.warning("HITL pending write failed: %s", exc)


async def clear_pending_hitl(business_id: str, channel: str, sender_id: str) -> None:
    try:
        await get_redis().delete(hitl_pending_key(business_id, channel, sender_id))
    except Exception as exc:
        logger.warning("HITL pending delete failed: %s", exc)


def confirmation_body(preview: str, message: str | None = None) -> str:
    line = (message or f"Should I proceed with: {preview}?").strip()
    return (
        f"{line}\n\n"
        "This change will not run until you confirm."
    )


async def send_confirmation_prompt(
    *,
    channel: str,
    recipient_id: str,
    business_id: str,
    preview: str,
    message: str | None = None,
    skip_send: bool = False,
) -> None:
    """WhatsApp interactive buttons; IG/FB (and fallback) use YES/NO text."""
    if skip_send:
        return
    from agent.tools import call_mcp_tool, is_mcp_error_result, mcp_error_message

    body = confirmation_body(preview, message)
    if channel == "whatsapp":
        result = await call_mcp_tool(
            "meta",
            "send_whatsapp_interactive",
            {
                "business_id": business_id,
                "to": recipient_id,
                "interactive_type": "button",
                "body": body[:1024],
                "buttons_or_sections": [
                    {"id": HITL_APPROVE_ID, "title": "Confirm"},
                    {"id": HITL_DECLINE_ID, "title": "Cancel"},
                ],
            },
        )
        if is_mcp_error_result(result):
            logger.warning(
                "HITL interactive send failed, falling back to text: %s",
                mcp_error_message(result),
            )
            from agent.pipeline import send_response

            await send_response(
                channel=channel,
                recipient_id=recipient_id,
                text=f"{body}\n\nReply YES to confirm or NO to cancel.",
                business_id=business_id,
            )
        return

    from agent.pipeline import send_response

    await send_response(
        channel=channel,
        recipient_id=recipient_id,
        text=f"{body}\n\nReply YES to confirm or NO to cancel.",
        business_id=business_id,
    )


def hitl_from_specialist_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("status") == "confirmation_required" and isinstance(result.get("hitl"), dict):
        return result["hitl"]
    return None


def hitl_from_broker_tool_output(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get(HITL_MARKER):
        return data
    return None
