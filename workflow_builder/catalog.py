"""Known MCP tool names for workflow validation and decomposition."""

from __future__ import annotations

from typing import Any

from agent.tools import (
    _CUSTOMER_CORE_TOOLS,
    _CUSTOMER_META_TOOLS,
    _OWNER_ONLY_CORE_TOOLS,
)


def core_tool_catalog() -> list[dict[str, str]]:
    names = list(dict.fromkeys(_CUSTOMER_CORE_TOOLS + _CUSTOMER_META_TOOLS + _OWNER_ONLY_CORE_TOOLS))
    return [{"name": n, "description": n} for n in names]


async def available_tools_for_business(business_id: str) -> list[dict[str, str]]:
    tools = core_tool_catalog()
    seen = {t["name"] for t in tools}
    if not business_id:
        return tools
    try:
        from agent.mcp_store import list_connections

        rows = await list_connections(business_id, active_only=True)
    except Exception:
        return tools
    for row in rows:
        prefix = str(row.get("name") or "").strip()
        discovered = row.get("discovered_tools") or []
        if isinstance(discovered, str):
            continue
        for raw in discovered:
            name = str(raw)
            if prefix and "__" not in name:
                name = f"{prefix}__{name}"
            if name and name not in seen:
                seen.add(name)
                tools.append({"name": name, "description": name})
    return tools
