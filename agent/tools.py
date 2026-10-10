"""MCP tool binding for customer and owner sessions.

Tool descriptors are cached, but each invocation gets its own streamable-HTTP
session. This avoids AnyIO cancel scopes crossing worker tasks and lets an MCP
server restart without terminating the worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field, create_model

from agent.error_handling import ToolCallRetryWrapper
from agent.settings import get_agent_settings

logger = logging.getLogger(__name__)

_CUSTOMER_CORE_TOOLS = [
    "search_knowledge",
    "search_entities",
    "list_entities",
    "get_entity",
    "get_business_hours",
    "get_dynamic_data",
    "get_or_create_conversation",
    "log_message",
    "log_gap",
    "escalate_to_owner",
    "get_customer",
    "update_customer",
    "create_payment_link",
    "check_payment_status",
]

_CUSTOMER_META_TOOLS = [
    "send_whatsapp_message",
    "send_whatsapp_template",
    "send_whatsapp_interactive",
    "send_whatsapp_media",
    "send_whatsapp_carousel",
    "send_whatsapp_location",
    "send_whatsapp_catalog",
    "send_whatsapp_flow",
    "send_instagram_dm",
    "reply_instagram_comment",
    "get_instagram_post_info",
    "get_instagram_comments",
    "send_facebook_message",
    "reply_facebook_comment",
    "get_facebook_post_info",
    "get_facebook_comments",
    "mark_as_read",
]

_OWNER_ONLY_CORE_TOOLS = [
    "add_knowledge",
    "update_knowledge",
    "delete_knowledge",
    "upsert_entity",
    "delete_entity",
    "list_entities",
    "update_business_info",
    "upsert_business_context",
    "get_business_config",
    "get_daily_summary",
    "get_recent_conversations",
    "search_customers",
    "classify_contact",
    "get_contact_type",
    "list_personal_contacts",
    "create_workflow_rule",
    "list_workflow_rules",
    "update_workflow_rule",
    "delete_workflow_rule",
    "get_workflow_performance",
    "resolve_gap",
    "set_dynamic_data",
    "process_document",
    # Direct Channel Framework actions
    "send_email",
    "list_emails",
    "get_calendar_events",
    "check_availability",
    "create_calendar_event",
    "send_sms",
    "send_otp",
    "verify_otp",
    "create_order",
    "initiate_refund",
    # Connect helpers — recover when an action fails because not connected
    "connect_razorpay",
    "connect_fast2sms",
    "get_oauth_connect_url_tool",
    "list_connected_integrations",
    "get_integration_connect_instructions",
    "generate_instagram_oauth_url",
    "generate_facebook_oauth_url",
]

# Backward-compatible export name; implementations are Channel Framework tools.
OWNER_CHANNEL_ACTION_TOOLS = [
    "send_email",
    "list_emails",
    "get_calendar_events",
    "check_availability",
    "create_calendar_event",
    "send_sms",
    "send_otp",
    "verify_otp",
    "create_order",
    "initiate_refund",
]

OWNER_CONNECT_TOOLS = [
    "connect_razorpay",
    "connect_fast2sms",
    "get_oauth_connect_url_tool",
    "list_connected_integrations",
    "get_integration_connect_instructions",
    "generate_instagram_oauth_url",
    "generate_facebook_oauth_url",
]

_SERVER_NAMES = ("core", "meta", "channel")

_mcp_client: MultiServerMCPClient | None = None
_tools_by_name: dict[str, BaseTool] | None = None
_tool_server: dict[str, str] = {}
_available_servers: set[str] = set()
_tool_cache_lock = asyncio.Lock()


def _get_client() -> MultiServerMCPClient:
    global _mcp_client
    if _mcp_client is None:
        settings = get_agent_settings()
        _mcp_client = MultiServerMCPClient(
            {
                "core": {
                    "transport": "streamable_http",
                    "url": settings.core_mcp_url,
                },
                "meta": {
                    "transport": "streamable_http",
                    "url": settings.meta_mcp_tools_url,
                },
                "channel": {
                    "transport": "streamable_http",
                    "url": settings.channel_mcp_tools_url,
                },
            }
        )
    return _mcp_client


def format_exception(exc: BaseException) -> str:
    """Flatten ExceptionGroup children for readable logs."""
    parts = [f"{type(exc).__name__}: {exc}"]
    if isinstance(exc, BaseExceptionGroup):
        for i, sub in enumerate(exc.exceptions):
            parts.append(f"  [{i}] {format_exception(sub)}")
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        parts.append(f"  caused by: {format_exception(cause)}")
    return "\n".join(parts)


def _is_session_dead_error(exc: BaseException) -> bool:
    text = format_exception(exc).lower()
    needles = (
        "closed",
        "disconnect",
        "connection reset",
        "broken pipe",
        "not connected",
        "session terminated",
        "transport",
        "streamablehttp",
        "connectionerror",
        "connecterror",
    )
    return any(n in text for n in needles)


async def _open_server_session(server_name: str) -> list[BaseTool]:
    """Load stateless descriptors whose invocations create their own sessions."""
    tools = await _get_client().get_tools(server_name=server_name)
    _available_servers.add(server_name)
    logger.info(
        "Loaded stateless MCP catalog '%s' with %d tools",
        server_name,
        len(tools),
    )
    return tools


def _merge_server_tools(server_name: str, tools: list[BaseTool]) -> None:
    """Replace catalog entries for one server; leave the other server intact."""
    global _tools_by_name, _tool_server
    if _tools_by_name is None:
        _tools_by_name = {}
    for name, srv in list(_tool_server.items()):
        if srv == server_name:
            _tools_by_name.pop(name, None)
            _tool_server.pop(name, None)
    for tool in tools:
        _tools_by_name[tool.name] = tool
        _tool_server[tool.name] = server_name


async def ensure_server_session(server_name: str) -> bool:
    """
    Ensure one MCP server catalog is available. Does not disturb its peer.

    The retained name avoids churn in callers; no live session is retained.
    """
    if server_name not in _SERVER_NAMES:
        raise ValueError(f"Unknown MCP server: {server_name}")

    async with _tool_cache_lock:
        if _tools_by_name and any(
            srv == server_name for srv in _tool_server.values()
        ):
            _available_servers.add(server_name)
            return True

        try:
            tools = await _open_server_session(server_name)
            _merge_server_tools(server_name, tools)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to ensure MCP session '%s':\n%s",
                server_name,
                format_exception(exc),
            )
            return False


def missing_mcp_servers() -> list[str]:
    """Servers whose tool catalogs could not be loaded."""
    return [name for name in _SERVER_NAMES if name not in _available_servers]


def mcp_tool_counts() -> dict[str, int]:
    """Return cached descriptor counts for the worker readiness matrix."""
    return {
        server: sum(1 for value in _tool_server.values() if value == server)
        for server in _SERVER_NAMES
    }


async def ensure_all_mcp_sessions() -> dict[str, bool]:
    """Best-effort open all MCP servers without tearing down healthy sessions."""
    return {
        "core": await ensure_server_session("core"),
        "meta": await ensure_server_session("meta"),
        "channel": await ensure_server_session("channel"),
    }


async def _rebuild_catalog_from_sessions() -> dict[str, BaseTool]:
    """
    Refresh Core+Meta stateless tool descriptors. Raises only if both fail.
    """
    global _tools_by_name, _tool_server
    errors: dict[str, BaseException] = {}

    for server_name in _SERVER_NAMES:
        try:
            tools = await _open_server_session(server_name)
            _merge_server_tools(server_name, tools)
        except Exception as exc:  # noqa: BLE001
            _available_servers.discard(server_name)
            errors[server_name] = exc
            logger.warning(
                "Failed to load MCP catalog '%s':\n%s",
                server_name,
                format_exception(exc),
            )

    if not _tools_by_name:
        detail = "; ".join(f"{name}: {format_exception(err)}" for name, err in errors.items())
        raise RuntimeError(f"Failed to load tools from all MCP servers — {detail}")

    if errors:
        logger.warning(
            "Proceeding with %d tools from partial MCP catalog (failed: %s)",
            len(_tools_by_name),
            ", ".join(errors),
        )
    else:
        logger.info(
            "Loaded %d MCP tools from Core+Meta (stateless calls)",
            len(_tools_by_name),
        )

    return _tools_by_name


async def init_mcp_sessions() -> dict[str, BaseTool]:
    """Load Core/Meta stateless tool descriptors."""
    return await refresh_tool_catalog()


async def close_mcp_sessions() -> None:
    """Clear cached descriptors; per-call sessions close inside the adapter."""
    global _tools_by_name, _tool_server, _mcp_client
    async with _tool_cache_lock:
        _tools_by_name = None
        _tool_server = {}
        _available_servers.clear()
        _mcp_client = None
    logger.info("Cleared stateless MCP tool catalogs")


def abandon_mcp_sessions() -> None:
    """Synchronously clear stateless catalog references for short-lived scripts."""
    global _tools_by_name, _tool_server, _mcp_client
    _tools_by_name = None
    _tool_server = {}
    _available_servers.clear()
    _mcp_client = None
    logger.info("Cleared MCP catalog references")


async def refresh_tool_catalog() -> dict[str, BaseTool]:
    """Reload stateless descriptors without retaining transport contexts."""
    async with _tool_cache_lock:
        return await _rebuild_catalog_from_sessions()


async def reset_server_session(server_name: str) -> None:
    """Refresh one server's descriptors after a failed per-call transport."""
    async with _tool_cache_lock:
        try:
            tools = await _open_server_session(server_name)
        except Exception as exc:
            logger.error(
                "Failed to reset MCP session '%s':\n%s",
                server_name,
                format_exception(exc),
            )
            raise
        _merge_server_tools(server_name, tools)


async def _catalog() -> dict[str, BaseTool]:
    async with _tool_cache_lock:
        if _tools_by_name is None:
            return await _rebuild_catalog_from_sessions()
        return _tools_by_name


def _strip_business_id_schema(tool: BaseTool) -> type[BaseModel] | dict | None:
    """Build args schema without business_id for the LLM."""
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None

    if isinstance(schema, dict):
        props = {k: v for k, v in (schema.get("properties") or {}).items() if k != "business_id"}
        required = [r for r in (schema.get("required") or []) if r != "business_id"]
        return {
            "title": schema.get("title") or f"{tool.name}Args",
            "type": "object",
            "properties": props,
            "required": required,
        }

    try:
        fields: dict[str, Any] = {}
        for name, finfo in schema.model_fields.items():
            if name == "business_id":
                continue
            default = ... if finfo.is_required() else finfo.default
            annotation = finfo.annotation if finfo.annotation is not None else Any
            fields[name] = (
                annotation,
                Field(default=default, description=finfo.description or ""),
            )
        model_name = f"{getattr(schema, '__name__', tool.name)}_NoBizId"
        if not fields:
            return create_model(model_name)
        return create_model(model_name, **fields)
    except Exception as exc:
        logger.debug("Could not strip business_id from %s schema: %s", tool.name, exc)
        return schema


async def _ainvoke_with_session_lock(
    tool: BaseTool,
    server_name: str,
    kwargs: dict[str, Any],
) -> Any:
    """Invoke with a per-call session; refresh the descriptor once on transport failure."""
    started = time.monotonic()
    logger.info("TOOL_START server=%s tool=%s", server_name, tool.name)

    async def _invoke(target: BaseTool) -> Any:
        if hasattr(target, "ainvoke"):
            return await target.ainvoke(kwargs)
        return await asyncio.to_thread(target.invoke, kwargs)

    def _log_success(result: Any, *, recovered: bool = False) -> None:
        error = result.get("error") if isinstance(result, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        logger.info(
            "TOOL_END server=%s tool=%s status=%s error_code=%s recovered=%s duration_ms=%d",
            server_name,
            tool.name,
            status or ("error" if error_code else "ok"),
            error_code,
            recovered,
            int((time.monotonic() - started) * 1000),
        )

    try:
        result = await _invoke(tool)
        _log_success(result)
        return result
    except Exception as exc:
        if not _is_session_dead_error(exc):
            logger.error(
                "TOOL_ERROR server=%s tool=%s error_type=%s retryable=false duration_ms=%d",
                server_name,
                tool.name,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise
        dead_exc = exc
        logger.warning(
            "MCP call '%s' failed during %s — refreshing catalog: %s",
            server_name,
            tool.name,
            exc,
        )

    await reset_server_session(server_name)
    catalog = await _catalog()
    refreshed = catalog.get(tool.name)
    if refreshed is None:
        raise RuntimeError(
            f"MCP tool missing after session reset: {tool.name}"
        ) from dead_exc
    try:
        result = await _invoke(refreshed)
    except Exception as exc:
        logger.error(
            "TOOL_ERROR server=%s tool=%s error_type=%s retryable=%s recovered=true duration_ms=%d",
            server_name,
            tool.name,
            type(exc).__name__,
            str(_is_session_dead_error(exc)).lower(),
            int((time.monotonic() - started) * 1000),
        )
        raise
    _log_success(result, recovered=True)
    return result


def _wrap_with_business_id(
    tool: BaseTool,
    business_id: str,
    *,
    server_name: str,
    require_confirmation: bool = False,
) -> StructuredTool:
    """Inject business_id on every call; hide it from the LLM schema."""

    async def _ainvoke(**kwargs: Any) -> Any:
        kwargs = dict(kwargs)
        kwargs.pop("business_id", None)
        kwargs["business_id"] = business_id

        from agent.tool_validator import validate_tool_args

        cleaned, validation_error = validate_tool_args(tool.name, kwargs)
        if validation_error is not None:
            return validation_error
        kwargs = cleaned or kwargs

        if require_confirmation:
            from agent.confirmation import maybe_confirm

            gated = maybe_confirm(tool.name, kwargs)
            if gated is not None:
                return gated

        async def _call():
            return await _ainvoke_with_session_lock(tool, server_name, kwargs)

        result = await ToolCallRetryWrapper.execute_with_retry(_call)
        return _normalize_tool_result(result)

    args_schema = _strip_business_id_schema(tool)
    return StructuredTool(
        name=tool.name,
        description=tool.description or tool.name,
        coroutine=_ainvoke,
        args_schema=args_schema,
    )


def _normalize_tool_result(result: Any) -> Any:
    if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
        return result
    # LangChain content_and_artifact tuple
    if isinstance(result, tuple) and len(result) == 2:
        return _normalize_tool_result(result[0])
    if hasattr(result, "content"):
        content = result.content
        if isinstance(content, list) and content:
            first = content[0]
            text = getattr(first, "text", None) or str(first)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
        return content
    return str(result)


def is_mcp_error_result(result: Any) -> bool:
    """True when an MCP tool returned a structured error dict (not raised)."""
    return isinstance(result, dict) and "error" in result


def mcp_error_message(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    err = result.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or err.get("msg") or str(err)
        return f"{code}: {msg}" if code else str(msg)
    return str(err or result)


async def _tools_for_names(
    names: list[str],
    business_id: str,
    *,
    conversation_id: str | None = None,
    require_confirmation: bool = False,
) -> list[StructuredTool]:
    catalog = await _catalog()
    out: list[StructuredTool] = []
    for name in names:
        base = catalog.get(name)
        if base is None:
            logger.debug("MCP tool not available: %s", name)
            continue
        server_name = _tool_server.get(name, "core")
        if name == "escalate_to_owner":
            out.append(
                _wrap_escalate_with_cooldown(
                    base,
                    business_id,
                    conversation_id=conversation_id,
                    server_name=server_name,
                )
            )
        else:
            out.append(
                _wrap_with_business_id(
                    base,
                    business_id,
                    server_name=server_name,
                    require_confirmation=require_confirmation,
                )
            )
    return out


def _wrap_escalate_with_cooldown(
    tool: BaseTool,
    business_id: str,
    *,
    conversation_id: str | None = None,
    server_name: str = "core",
) -> StructuredTool:
    """Gate escalate_to_owner until owner responds (§6.3) before calling Core MCP."""

    async def _ainvoke(**kwargs: Any) -> Any:
        from agent.escalation import (
            ESCALATION_COOLDOWN_CUSTOMER_TEXT,
            escalation_recently_sent,
        )

        kwargs = dict(kwargs)
        kwargs.pop("business_id", None)
        kwargs["business_id"] = business_id
        conv_id = kwargs.get("conversation_id") or conversation_id
        if conv_id:
            kwargs.setdefault("conversation_id", conv_id)
            try:
                if await escalation_recently_sent(business_id, str(conv_id)):
                    return {
                        "status": "pending_owner",
                        "escalated": False,
                        "agent_guidance": (
                            "An escalation is already waiting for the owner. "
                            f'Tell the customer: "{ESCALATION_COOLDOWN_CUSTOMER_TEXT}" '
                            "Do NOT call escalate_to_owner again."
                        ),
                        "customer_message": ESCALATION_COOLDOWN_CUSTOMER_TEXT,
                    }
            except Exception as exc:
                logger.warning("Escalation owner-gate check failed: %s", exc)

        async def _call():
            return await _ainvoke_with_session_lock(tool, server_name, kwargs)

        result = await ToolCallRetryWrapper.execute_with_retry(_call)
        return _normalize_tool_result(result)

    args_schema = _strip_business_id_schema(tool)
    return StructuredTool(
        name=tool.name,
        description=tool.description or tool.name,
        coroutine=_ainvoke,
        args_schema=args_schema,
    )


async def tools_for_names(
    names: list[str],
    business_id: str,
    *,
    conversation_id: str | None = None,
    require_confirmation: bool = False,
) -> list[StructuredTool]:
    """Public alias used by specialist factory / onboarding extras."""
    return await _tools_for_names(
        names,
        business_id,
        conversation_id=conversation_id,
        require_confirmation=require_confirmation,
    )


async def get_customer_tools(
    business_id: str,
    *,
    conversation_id: str | None = None,
) -> list[StructuredTool]:
    # Recover late-starting Core/Meta without tearing down a healthy peer session
    await ensure_all_mcp_sessions()
    core = await _tools_for_names(
        _CUSTOMER_CORE_TOOLS, business_id, conversation_id=conversation_id
    )
    meta = await _tools_for_names(_CUSTOMER_META_TOOLS, business_id)
    return core + meta


async def get_owner_tools(
    business_id: str,
    *,
    conversation_id: str | None = None,
    require_confirmation: bool = True,
) -> list[StructuredTool]:
    await ensure_all_mcp_sessions()
    customer = await get_customer_tools(business_id, conversation_id=conversation_id)
    owner = await _tools_for_names(
        _OWNER_ONLY_CORE_TOOLS,
        business_id,
        require_confirmation=require_confirmation,
    )
    by_name: dict[str, StructuredTool] = {t.name: t for t in customer}
    for t in owner:
        by_name[t.name] = t
    return list(by_name.values())


async def call_mcp_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """
    Direct MCP tool call for pipeline (send_response, log helpers).

    server: 'core' | 'meta'
    """
    catalog = await _catalog()
    tool = catalog.get(tool_name)
    if tool is None:
        # Open only the needed server — never full refresh (that killed live Meta)
        await ensure_server_session(server)
        catalog = await _catalog()
        tool = catalog.get(tool_name)
    if tool is None:
        raise RuntimeError(f"MCP tool not found: {tool_name}")

    server_name = _tool_server.get(tool_name) or server
    result = await _ainvoke_with_session_lock(tool, server_name, arguments)
    return _normalize_tool_result(result)
