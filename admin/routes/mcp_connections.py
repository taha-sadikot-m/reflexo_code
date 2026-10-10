"""Admin CRUD for per-business custom MCP connections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin
from admin.helpers import audit, require_business
from agent.mcp_store import (
    McpStoreError,
    delete_connection,
    get_connection,
    insert_connection,
    list_connections,
    record_health,
    update_connection,
)
from agent.mcp_security import McpSecurityError

router = APIRouter(prefix="/api/admin/mcp-connections", tags=["admin-mcp-connections"])


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    server_url: str = Field(min_length=8)
    description: str = ""
    auth_headers: dict[str, str] | None = None


class PatchBody(BaseModel):
    name: str | None = None
    server_url: str | None = None
    description: str | None = None
    auth_headers: dict[str, str] | None = None
    clear_auth: bool = False
    is_active: bool | None = None


class ProbeBody(BaseModel):
    server_url: str = Field(min_length=8)
    name: str = "probe"
    auth_headers: dict[str, str] | None = None


class RegistryConnectBody(BaseModel):
    registry_name: str = Field(min_length=3, max_length=300)
    registry_version: str = Field(default="latest", max_length=100)
    remote_index: int = Field(default=0, ge=0)
    variables: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


@router.get("/registry")
async def browse_mcp_registry(
    admin: CurrentAdmin,
    search: str = Query(default="", max_length=200),
    cursor: str = Query(default="", max_length=1000),
    limit: int = Query(default=24, ge=1, le=100),
) -> dict[str, Any]:
    del admin
    from agent.mcp_registry import search_registry

    try:
        return await search_registry(search=search, cursor=cursor, limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="The MCP Registry is temporarily unavailable"
        ) from exc


@router.get("/registry/popular")
async def browse_popular_mcp_servers(admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    from agent.mcp_registry import list_popular_servers

    try:
        return await list_popular_servers()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="The MCP Registry is temporarily unavailable"
        ) from exc


def _registry_slug(registry_name: str) -> str:
    import hashlib
    import re

    tail = registry_name.rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", tail).strip("_")[:53]
    if not slug or not slug[0].isalpha():
        slug = f"mcp_{slug}"[:53]
    suffix = hashlib.sha256(registry_name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{suffix}"


async def _resolve_registry_body(
    body: RegistryConnectBody,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, str]]:
    from agent.mcp_registry import get_registry_server
    from agent.mcp_security import McpSecurityError, resolve_remote, validate_remote_url

    item = await get_registry_server(body.registry_name, body.registry_version)
    if not item:
        raise HTTPException(status_code=404, detail="Registry server not found")
    remote = next(
        (r for r in item["remotes"] if r["index"] == body.remote_index), None
    )
    if not remote:
        raise HTTPException(
            status_code=400, detail="Selected Streamable HTTP remote is unavailable"
        )
    try:
        url, headers = resolve_remote(
            remote, variables=body.variables, headers=body.headers
        )
        url = await validate_remote_url(url)
    except McpSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return item, remote, url, headers


@router.get("/oauth/client-metadata")
async def mcp_oauth_client_metadata() -> dict[str, Any]:
    from agent.mcp_oauth import client_metadata

    return client_metadata()


@router.get("/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> HTMLResponse:
    if error or not code or not state:
        message = "Authorization was denied or incomplete."
        return HTMLResponse(
            f"<html><body><h2>{message}</h2><script>setTimeout(()=>window.close(),1500)</script></body></html>",
            status_code=400,
        )
    try:
        from agent.mcp_oauth import finish_oauth

        result = await finish_oauth(code=code, state=state)
        await audit(
            result["admin_id"],
            "mcp_registry.oauth_connect",
            business_id=result["business_id"],
            entity_type="mcp_connection",
            entity_id=result["connection"].get("id"),
        )
        ok = bool(result["probe"].get("ok"))
        message = "MCP server connected." if ok else "Authorized, but tool discovery failed."
        import json
        from api.settings import get_api_settings

        target_origin = get_api_settings().admin_frontend_origin
        return HTMLResponse(
            f"<html><body><h2>{message}</h2><script>window.opener?.postMessage({{type:'reflexo-mcp-oauth',ok:{str(ok).lower()}}},{json.dumps(target_origin)});setTimeout(()=>window.close(),800)</script></body></html>"
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("MCP OAuth callback failed")
        return HTMLResponse(
            "<html><body><h2>Could not complete MCP authorization.</h2><script>setTimeout(()=>window.close(),1800)</script></body></html>",
            status_code=400,
        )


@router.post("/registry/oauth/{business_id}/start")
async def start_registry_oauth(
    business_id: str, body: RegistryConnectBody, admin: AdminPlus
) -> dict[str, str]:
    await require_business(business_id)
    item, _remote, url, headers = await _resolve_registry_body(body)
    from agent.mcp_oauth import start_oauth

    try:
        return await start_oauth(
            business_id=business_id,
            admin_id=admin.id,
            item=item,
            server_url=url,
            static_headers=headers,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc


@router.post("/registry/connect/{business_id}")
async def connect_registry_server(
    business_id: str, body: RegistryConnectBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    item, _remote, url, headers = await _resolve_registry_body(body)
    name = _registry_slug(item["name"])

    from agent.mcp_connector import invalidate_tool_cache, probe_mcp_server as probe_remote

    result = await probe_remote(
        server_url=url,
        connection_name=name,
        auth_headers=headers,
        business_id=business_id,
    )
    if not result.get("ok"):
        error = str(result.get("error") or "")
        auth_required = bool(result.get("auth_required")) or any(
            marker in error.lower()
            for marker in ("401", "403", "unauthorized", "authorization required")
        )
        return {
            "connected": False,
            "requires_oauth": auth_required,
            "status": result.get("status"),
            "status_code": result.get("status_code"),
            "error": error or "The MCP server could not be reached",
        }
    try:
        row = await insert_connection(
            business_id,
            name=name,
            server_url=url,
            description=item.get("description") or item.get("title") or "",
            auth_headers=headers or None,
            registry_name=item["name"],
            registry_version=item["version"],
            auth_type="headers" if headers else "none",
        )
    except Exception as exc:
        detail = str(exc)
        if "unique" in detail.lower() or "duplicate" in detail.lower():
            raise HTTPException(
                status_code=409, detail="This MCP server is already connected"
            ) from exc
        raise
    await record_health(
        business_id,
        str(row["id"]),
        status="healthy",
        discovered_tools=list(result.get("tools") or []),
    )
    invalidate_tool_cache(business_id)
    await audit(
        admin.id,
        "mcp_registry.connect",
        business_id=business_id,
        entity_type="mcp_connection",
        entity_id=row.get("id"),
    )
    return {
        "connected": True,
        "requires_oauth": False,
        "connection": await get_connection(business_id, str(row["id"])),
        "tools": result.get("tools") or [],
    }


@router.get("/{business_id}")
async def list_mcp_connections(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    items = await list_connections(business_id, active_only=False)
    return {"items": items}


@router.post("/{business_id}")
async def create_mcp_connection(
    business_id: str, body: CreateBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    try:
        from agent.mcp_security import validate_remote_url

        await validate_remote_url(body.server_url)
        row = await insert_connection(
            business_id,
            name=body.name,
            server_url=body.server_url,
            description=body.description,
            auth_headers=body.auth_headers,
        )
    except (McpStoreError, McpSecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        detail = str(exc)
        if "unique" in detail.lower() or "duplicate" in detail.lower():
            raise HTTPException(
                status_code=409, detail="A connection with that name already exists"
            ) from exc
        raise
    from agent.mcp_connector import invalidate_tool_cache

    invalidate_tool_cache(business_id)
    await audit(
        admin.id,
        "mcp_connection.create",
        business_id=business_id,
        entity_type="mcp_connection",
        entity_id=row.get("id"),
    )
    return row


@router.patch("/{business_id}/{connection_id}")
async def patch_mcp_connection(
    business_id: str, connection_id: str, body: PatchBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    try:
        if body.server_url is not None:
            from agent.mcp_security import validate_remote_url

            await validate_remote_url(body.server_url)
        row = await update_connection(
            business_id,
            connection_id,
            name=body.name,
            description=body.description,
            server_url=body.server_url,
            auth_headers=body.auth_headers,
            is_active=body.is_active,
            clear_auth=body.clear_auth,
        )
    except (McpStoreError, McpSecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Connection not found")
    from agent.mcp_connector import invalidate_tool_cache

    invalidate_tool_cache(business_id)
    await audit(
        admin.id,
        "mcp_connection.update",
        business_id=business_id,
        entity_type="mcp_connection",
        entity_id=connection_id,
    )
    return row


@router.delete("/{business_id}/{connection_id}")
async def delete_mcp_connection(
    business_id: str, connection_id: str, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    ok = await delete_connection(business_id, connection_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Connection not found")
    from agent.mcp_connector import invalidate_tool_cache

    invalidate_tool_cache(business_id)
    await audit(
        admin.id,
        "mcp_connection.delete",
        business_id=business_id,
        entity_type="mcp_connection",
        entity_id=connection_id,
    )
    return {"ok": True}


@router.post("/{business_id}/probe")
async def probe_mcp_server(
    business_id: str, body: ProbeBody, admin: AdminPlus
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from agent.mcp_connector import probe_mcp_server as probe_remote
    from agent.mcp_security import validate_remote_url
    from agent.mcp_store import validate_connection_name, validate_server_url

    try:
        url = validate_server_url(body.server_url)
        url = await validate_remote_url(url)
        name = validate_connection_name(body.name) if body.name != "probe" else "probe"
    except (McpStoreError, McpSecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await probe_remote(
        server_url=url,
        connection_name=name,
        auth_headers=body.auth_headers,
        business_id=business_id,
    )


@router.post("/{business_id}/{connection_id}/test")
async def test_mcp_connection(
    business_id: str, connection_id: str, admin: AdminPlus
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from agent.mcp_connector import invalidate_tool_cache, probe_mcp_server as probe_remote
    from agent.mcp_store import decrypt_auth_headers, get_connection_internal

    row = await get_connection_internal(business_id, connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="Connection not found")
    from agent.mcp_security import validate_remote_url

    url = await validate_remote_url(str(row["server_url"]))
    if str(row.get("auth_type") or "") == "oauth":
        from agent.mcp_oauth import oauth_headers

        try:
            auth_headers = await oauth_headers(
                business_id, connection_id, row.get("auth_config") or {}
            )
        except Exception:
            return {
                "ok": False,
                "status": "reauth_required",
                "tools": [],
                "error": "This MCP server needs authorization again",
                "connection": await get_connection(business_id, connection_id),
            }
    else:
        auth_headers = decrypt_auth_headers(row.get("auth_config") or {})
    result = await probe_remote(
        server_url=url,
        connection_name=str(row["name"]),
        auth_headers=auth_headers,
        business_id=business_id,
        connection_id=connection_id,
    )
    await record_health(
        business_id,
        connection_id,
        status=str(result.get("status") or "error"),
        discovered_tools=list(result.get("tools") or []),
    )
    invalidate_tool_cache(business_id)
    updated = await get_connection(business_id, connection_id)
    return {**result, "connection": updated}
