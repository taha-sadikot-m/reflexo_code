"""CRUD for per-business custom MCP connections."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from shared.db import tenant_connection

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ROW_FIELDS = (
    "id",
    "business_id",
    "name",
    "description",
    "server_url",
    "transport_type",
    "auth_config",
    "registry_name",
    "registry_version",
    "auth_type",
    "auth_status",
    "is_active",
    "last_health_check",
    "last_health_status",
    "discovered_tools",
    "created_at",
    "updated_at",
)


class McpStoreError(ValueError):
    """Invalid MCP connection input."""


def validate_connection_name(name: str) -> str:
    cleaned = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not _NAME_RE.match(cleaned):
        raise McpStoreError(
            "Connection name must start with a letter and use only lowercase "
            "letters, digits, and underscores (max 63 chars)."
        )
    return cleaned


def validate_server_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        raise McpStoreError("server_url must be an http or https MCP endpoint")
    return cleaned.rstrip("/")


def encrypt_auth_headers(headers: dict[str, str] | None) -> dict[str, Any]:
    """Store auth headers as a Fernet ciphertext, never as plaintext JSON."""
    if not headers:
        return {}
    clean = {str(k): str(v) for k, v in headers.items() if str(k).strip() and str(v)}
    if not clean:
        return {}
    from mcp_servers.meta_mcp.auth.tokens import encrypt_token

    token = encrypt_token(json.dumps(clean, separators=(",", ":")))
    return {"headers_encrypted": token.decode("utf-8") if isinstance(token, bytes) else str(token)}


def _as_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def decrypt_auth_headers(auth_config: dict[str, Any] | str | None) -> dict[str, str]:
    auth_config = _as_dict(auth_config)
    if not auth_config:
        return {}
    blob = auth_config.get("headers_encrypted")
    if not blob:
        return {}
    from mcp_servers.meta_mcp.auth.tokens import decrypt_token

    raw = decrypt_token(blob.encode("utf-8") if isinstance(blob, str) else blob)
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    mapping = dict(row)
    out: dict[str, Any] = {}
    for key in _ROW_FIELDS:
        value = mapping.get(key)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif key == "auth_config":
            value = _as_dict(value)
        elif key == "discovered_tools":
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = []
            elif value is None:
                value = []
        out[key] = value
    public_auth = out.get("auth_config") or {}
    out["has_auth"] = bool(
        public_auth.get("headers_encrypted") or public_auth.get("oauth_encrypted")
    )
    # Never leak ciphertext to API/UI consumers.
    out.pop("auth_config", None)
    return out


def _row_internal(row: Any) -> dict[str, Any]:
    """Like `_row` but keeps encrypted auth_config for the connector."""
    data = dict(row)
    out: dict[str, Any] = {}
    for key in _ROW_FIELDS:
        value = data.get(key)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif key == "auth_config":
            value = _as_dict(value)
        elif key == "discovered_tools" and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = []
        out[key] = value
    return out


async def list_connections(business_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT {", ".join(_ROW_FIELDS)}
            FROM mcp_connections
            WHERE business_id = $1::uuid
              AND ($2::bool = false OR is_active = true)
            ORDER BY name
            """,
            business_id,
            active_only,
        )
    return [_row(r) for r in rows]


async def list_connections_internal(
    business_id: str, *, active_only: bool = True
) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT {", ".join(_ROW_FIELDS)}
            FROM mcp_connections
            WHERE business_id = $1::uuid
              AND ($2::bool = false OR is_active = true)
            ORDER BY name
            """,
            business_id,
            active_only,
        )
    return [_row_internal(r) for r in rows]


async def get_connection(business_id: str, connection_id: str) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {", ".join(_ROW_FIELDS)}
            FROM mcp_connections
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
        )
    return _row(row) if row else None


async def get_connection_internal(
    business_id: str, connection_id: str
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {", ".join(_ROW_FIELDS)}
            FROM mcp_connections
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
        )
    return _row_internal(row) if row else None


async def get_connection_by_registry_internal(
    business_id: str, registry_name: str
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {", ".join(_ROW_FIELDS)}
            FROM mcp_connections
            WHERE business_id = $1::uuid AND registry_name = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            business_id,
            registry_name,
        )
    return _row_internal(row) if row else None


async def count_active(business_id: str) -> int:
    async with tenant_connection(business_id) as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM mcp_connections
            WHERE business_id = $1::uuid AND is_active = true
            """,
            business_id,
        )
    return int(n or 0)


async def insert_connection(
    business_id: str,
    *,
    name: str,
    server_url: str,
    description: str = "",
    auth_headers: dict[str, str] | None = None,
    registry_name: str | None = None,
    registry_version: str | None = None,
    auth_type: str | None = None,
    auth_config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slug = validate_connection_name(name)
    url = validate_server_url(server_url)
    auth_config = (
        auth_config_override
        if auth_config_override is not None
        else encrypt_auth_headers(auth_headers)
    )
    resolved_auth_type = auth_type or ("headers" if auth_headers else "none")
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO mcp_connections (
                business_id, name, description, server_url, auth_config,
                registry_name, registry_version, auth_type
            )
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7, $8)
            RETURNING {", ".join(_ROW_FIELDS)}
            """,
            business_id,
            slug,
            (description or "").strip() or None,
            url,
            json.dumps(auth_config),
            registry_name,
            registry_version,
            resolved_auth_type,
        )
    logger.info("MCP_CONN created business=%s name=%s", business_id, slug)
    return _row(row)


async def update_connection(
    business_id: str,
    connection_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    server_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
    is_active: bool | None = None,
    clear_auth: bool = False,
) -> dict[str, Any] | None:
    current = await get_connection_internal(business_id, connection_id)
    if not current:
        return None
    slug = validate_connection_name(name) if name is not None else current["name"]
    url = validate_server_url(server_url) if server_url is not None else current["server_url"]
    desc = current["description"] if description is None else ((description or "").strip() or None)
    active = current["is_active"] if is_active is None else bool(is_active)
    if clear_auth:
        auth_config: dict[str, Any] = {}
    elif auth_headers is not None:
        auth_config = encrypt_auth_headers(auth_headers)
    else:
        auth_config = current.get("auth_config") or {}
        if isinstance(auth_config, str):
            auth_config = json.loads(auth_config)
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE mcp_connections
            SET name = $3,
                description = $4,
                server_url = $5,
                auth_config = $6::jsonb,
                is_active = $7,
                updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            RETURNING {", ".join(_ROW_FIELDS)}
            """,
            connection_id,
            business_id,
            slug,
            desc,
            url,
            json.dumps(auth_config),
            active,
        )
    return _row(row) if row else None


async def delete_connection(business_id: str, connection_id: str) -> bool:
    async with tenant_connection(business_id) as conn:
        status = await conn.execute(
            """
            DELETE FROM mcp_connections
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
        )
    deleted = str(status).endswith("1")
    if deleted:
        logger.info("MCP_CONN deleted business=%s id=%s", business_id, connection_id)
    return deleted


async def record_health(
    business_id: str,
    connection_id: str,
    *,
    status: str,
    discovered_tools: list[str] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE mcp_connections
            SET last_health_check = $3,
                last_health_status = $4,
                discovered_tools = COALESCE($5::jsonb, discovered_tools),
                updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
            now,
            status,
            json.dumps(discovered_tools) if discovered_tools is not None else None,
        )


async def replace_auth_config(
    business_id: str,
    connection_id: str,
    *,
    auth_config: dict[str, Any],
    auth_type: str = "oauth",
    auth_status: str = "connected",
) -> None:
    """Persist already-encrypted auth configuration and public auth state."""
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE mcp_connections
            SET auth_config = $3::jsonb,
                auth_type = $4,
                auth_status = $5,
                updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
            json.dumps(auth_config),
            auth_type,
            auth_status,
        )


async def mark_auth_status(
    business_id: str, connection_id: str, status: str
) -> None:
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE mcp_connections
            SET auth_status = $3, updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            connection_id,
            business_id,
            status,
        )
