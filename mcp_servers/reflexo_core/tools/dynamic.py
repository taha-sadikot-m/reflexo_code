"""Dynamic Redis data tools."""

from __future__ import annotations

import json
from typing import Any

from shared.redis_keys import dynamic_data_key

from mcp_servers.reflexo_core.utils.cache import get_redis
from mcp_servers.reflexo_core.utils.db_helpers import business_exists
from mcp_servers.reflexo_core.utils.errors import tool_error


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def set_dynamic_data(
        business_id: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Set ephemeral business data in Redis (e.g. today's special)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not key or not key.strip():
            return tool_error("INVALID_INPUT", "key is required")

        redis_key = dynamic_data_key(business_id, key.strip())
        payload = value if isinstance(value, str) else json.dumps(value, default=str)
        client = get_redis()
        if ttl_seconds and ttl_seconds > 0:
            await client.set(redis_key, payload, ex=ttl_seconds)
        else:
            await client.set(redis_key, payload)
        return {"key": key, "status": "set", "ttl_seconds": ttl_seconds}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_dynamic_data(business_id: str, key: str) -> dict[str, Any]:
        """Get ephemeral business data from Redis."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        redis_key = dynamic_data_key(business_id, key.strip())
        raw = await get_redis().get(redis_key)
        if raw is None:
            return {"key": key, "value": None, "found": False}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            value = raw
        return {"key": key, "value": value, "found": True}
