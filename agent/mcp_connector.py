"""Discover and bind tools from a business's custom MCP servers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agent.mcp_store import (
    count_active,
    decrypt_auth_headers,
    list_connections_internal,
    record_health,
)

logger = logging.getLogger(__name__)

TOOL_CACHE_TTL_SECONDS = 1800
DISCOVER_TIMEOUT_SECONDS = 5.0
# Admin clicks tolerate a cold remote server; live conversations do not.
PROBE_TIMEOUT_SECONDS = 20.0
_ERROR_WALK_MAX_DEPTH = 8

# business_id -> {tools, fetched_at, connection_ids}
_tool_cache: dict[str, dict[str, Any]] = {}


def invalidate_tool_cache(business_id: str) -> None:
    _tool_cache.pop(str(business_id), None)


def describe_mcp_error(exc: BaseException) -> dict[str, Any]:
    """Unwrap anyio task groups and cause chains into the failure that matters.

    The MCP Streamable HTTP client runs inside ``anyio.create_task_group()``, so
    every transport failure reaches us as a ``BaseExceptionGroup`` that stringifies
    to "unhandled errors in a TaskGroup" and hides the HTTP status underneath.
    """
    import httpx

    leaves: list[BaseException] = []
    seen: set[int] = set()

    def walk(node: BaseException | None, depth: int) -> None:
        if node is None or depth > _ERROR_WALK_MAX_DEPTH or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, BaseExceptionGroup):
            for sub in node.exceptions:
                walk(sub, depth + 1)
            return
        leaves.append(node)
        walk(node.__cause__ or node.__context__, depth + 1)

    walk(exc, 0)

    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPStatusError):
            response = leaf.response
            status = int(response.status_code)
            try:
                challenged = "www-authenticate" in response.headers
            except Exception:
                challenged = False
            reason = str(getattr(response, "reason_phrase", "") or "").strip()
            label = f"{status} {reason}".strip()
            return {
                "message": f"{label} from {leaf.request.url}",
                "status_code": status,
                "auth_required": status in (401, 403) or challenged,
                "timeout": False,
            }

    for leaf in leaves:
        if isinstance(leaf, httpx.TimeoutException):
            return {
                "message": "The MCP server did not respond in time",
                "status_code": None,
                "auth_required": False,
                "timeout": True,
            }
        if isinstance(leaf, httpx.TransportError):
            return {
                "message": f"Could not reach the MCP server: {leaf}",
                "status_code": None,
                "auth_required": False,
                "timeout": False,
            }

    concrete = [leaf for leaf in leaves if not isinstance(leaf, asyncio.CancelledError)]
    if not concrete:
        return {
            "message": "MCP tool discovery timed out",
            "status_code": None,
            "auth_required": False,
            "timeout": True,
        }
    leaf = concrete[0]
    text = str(leaf).strip()
    return {
        "message": f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__,
        "status_code": None,
        "auth_required": False,
        "timeout": False,
    }


async def has_mcp_connections(business_id: str) -> bool:
    if not business_id:
        return False
    try:
        return await count_active(business_id) > 0
    except Exception:
        logger.debug("MCP_CONN has_mcp_connections failed", exc_info=True)
        return False


async def get_business_mcp_tools(business_id: str) -> list[BaseTool]:
    """Return prefixed LangChain tools for every healthy active connection."""
    if not business_id:
        return []
    cached = _tool_cache.get(business_id)
    if cached:
        age = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds()
        if age < TOOL_CACHE_TTL_SECONDS:
            return list(cached["tools"])

    try:
        connections = await list_connections_internal(business_id, active_only=True)
    except Exception:
        logger.warning("MCP_CONN list failed business=%s", business_id, exc_info=True)
        return []

    all_tools: list[BaseTool] = []
    for conn in connections:
        try:
            tools = await _discover_tools_from_server(
                server_url=str(conn["server_url"]),
                connection_name=str(conn["name"]),
                connection_id=str(conn["id"]),
                business_id=business_id,
                auth_config=conn.get("auth_config") or {},
                auth_type=str(conn.get("auth_type") or "none"),
            )
            all_tools.extend(tools)
            await record_health(
                business_id,
                str(conn["id"]),
                status="healthy" if tools else "error",
                discovered_tools=[t.name for t in tools],
            )
        except Exception:
            logger.warning(
                "MCP_CONN discover failed business=%s name=%s",
                business_id,
                conn.get("name"),
                exc_info=True,
            )
            try:
                await record_health(
                    business_id, str(conn["id"]), status="error", discovered_tools=[]
                )
            except Exception:
                pass

    _tool_cache[business_id] = {
        "tools": all_tools,
        "fetched_at": datetime.now(timezone.utc),
    }
    return list(all_tools)


async def probe_mcp_server(
    *,
    server_url: str,
    connection_name: str,
    auth_headers: dict[str, str] | None = None,
    business_id: str = "",
    connection_id: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Probe a server without requiring a persisted row (used by admin Test)."""
    try:
        tools = await _discover_tools_from_server(
            server_url=server_url,
            connection_name=connection_name,
            connection_id=connection_id or "probe",
            business_id=business_id or "-",
            auth_config={"headers": auth_headers} if auth_headers else {},
            raw_headers=auth_headers,
            timeout=timeout if timeout is not None else PROBE_TIMEOUT_SECONDS,
        )
        names = [t.name for t in tools]
        return {
            "ok": True,
            "status": "healthy",
            "tools": names,
            "status_code": None,
            "auth_required": False,
        }
    except TimeoutError:
        return {
            "ok": False,
            "status": "timeout",
            "tools": [],
            "error": "MCP server timed out",
            "status_code": None,
            "auth_required": False,
        }
    except Exception as exc:
        info = describe_mcp_error(exc)
        auth_required = bool(info["auth_required"])
        return {
            "ok": False,
            "status": "unauthorized"
            if auth_required
            else "timeout"
            if info["timeout"]
            else "error",
            "tools": [],
            "error": str(info["message"])[:300],
            "status_code": info["status_code"],
            "auth_required": auth_required,
        }


async def _discover_tools_from_server(
    *,
    server_url: str,
    connection_name: str,
    connection_id: str,
    business_id: str,
    auth_config: dict[str, Any],
    raw_headers: dict[str, str] | None = None,
    auth_type: str = "none",
    timeout: float | None = None,
) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from agent.mcp_security import validate_remote_url

    server_url = await validate_remote_url(server_url)
    if raw_headers is not None:
        headers = raw_headers
    elif auth_type == "oauth":
        from agent.mcp_oauth import oauth_headers

        headers = await oauth_headers(business_id, connection_id, auth_config)
    else:
        headers = decrypt_auth_headers(auth_config)
    # Also accept already-plain headers from a probe payload.
    if not headers and isinstance(auth_config.get("headers"), dict):
        headers = {str(k): str(v) for k, v in auth_config["headers"].items()}

    server_cfg: dict[str, Any] = {
        "transport": "streamable_http",
        "url": server_url,
    }
    if headers:
        server_cfg["headers"] = headers

    client = MultiServerMCPClient({connection_name: server_cfg})
    try:
        raw_tools = await asyncio.wait_for(
            client.get_tools(),
            timeout=DISCOVER_TIMEOUT_SECONDS if timeout is None else timeout,
        )
    except TimeoutError:
        logger.warning(
            "MCP_CONN timeout name=%s url=%s",
            connection_name,
            server_url,
        )
        raise
    except Exception as exc:
        info = describe_mcp_error(exc)
        logger.warning(
            "MCP_CONN get_tools failed name=%s url=%s status=%s reason=%s",
            connection_name,
            server_url,
            info["status_code"],
            info["message"],
            exc_info=True,
        )
        raise

    prefixed: list[BaseTool] = []
    for tool in raw_tools or []:
        wrapped = _wrap_prefixed_tool(
            tool,
            connection_name=connection_name,
            connection_id=connection_id,
            business_id=business_id,
        )
        if wrapped is not None:
            prefixed.append(wrapped)
    logger.info(
        "MCP_CONN discovered n=%s name=%s business=%s",
        len(prefixed),
        connection_name,
        business_id,
    )
    return prefixed


def _wrap_prefixed_tool(
    tool: BaseTool,
    *,
    connection_name: str,
    connection_id: str,
    business_id: str,
) -> BaseTool | None:
    original = str(getattr(tool, "name", "") or "").strip()
    if not original:
        return None
    new_name = f"{connection_name}__{original}"
    description = str(getattr(tool, "description", "") or original)

    async def _call(**kwargs: Any) -> Any:
        logger.info(
            "CUSTOM_MCP call tool=%s connection_id=%s business=%s",
            new_name,
            connection_id,
            business_id,
        )
        return await tool.ainvoke(kwargs)

    kwargs: dict[str, Any] = {
        "coroutine": _call,
        "name": new_name,
        "description": description,
    }
    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        kwargs["args_schema"] = schema
    return StructuredTool.from_function(**kwargs)
