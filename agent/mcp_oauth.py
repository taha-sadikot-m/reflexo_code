"""OAuth 2.1 authorization and refresh for remote MCP servers."""

from __future__ import annotations

import json
import logging
import secrets
import time
import asyncio
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from agent.mcp_security import validate_remote_url

logger = logging.getLogger(__name__)
OAUTH_STATE_TTL_SECONDS = 600
_refresh_locks: dict[str, asyncio.Lock] = {}


def _token_auth(
    data: dict[str, Any], client_info: dict[str, Any]
) -> tuple[dict[str, Any], httpx.Auth | None]:
    secret = client_info.get("client_secret")
    method = client_info.get("token_endpoint_auth_method") or (
        "client_secret_post" if secret else "none"
    )
    if secret and method == "client_secret_basic":
        return data, httpx.BasicAuth(str(client_info["client_id"]), str(secret))
    if secret and method == "client_secret_post":
        return {**data, "client_secret": secret}, None
    return data, None


def encrypt_oauth_config(payload: dict[str, Any]) -> dict[str, str]:
    from mcp_servers.meta_mcp.auth.tokens import encrypt_token

    raw = json.dumps(payload, separators=(",", ":"))
    encrypted = encrypt_token(raw)
    return {
        "oauth_encrypted": (
            encrypted.decode("utf-8") if isinstance(encrypted, bytes) else str(encrypted)
        )
    }


def decrypt_oauth_config(auth_config: Any) -> dict[str, Any]:
    from agent.mcp_store import _as_dict

    config = _as_dict(auth_config)
    blob = config.get("oauth_encrypted")
    if not blob:
        return {}
    from mcp_servers.meta_mcp.auth.tokens import decrypt_token

    raw = decrypt_token(blob.encode("utf-8") if isinstance(blob, str) else blob)
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _base_url() -> str:
    from api.settings import get_api_settings

    return get_api_settings().mcp_oauth_base_url.rstrip("/")


def client_metadata(*, include_client_id: bool = True) -> dict[str, Any]:
    base = _base_url()
    metadata = {
        "client_name": "Reflexo MCP Connector",
        "client_uri": base,
        "redirect_uris": [f"{base}/api/admin/mcp-connections/oauth/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
    if include_client_id:
        metadata["client_id"] = (
            f"{base}/api/admin/mcp-connections/oauth/client-metadata"
        )
    return metadata


async def _get_json(client: httpx.AsyncClient, urls: list[str]) -> tuple[dict[str, Any], str]:
    for url in urls:
        try:
            await validate_remote_url(url)
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data, url
        except Exception:
            continue
    return {}, ""


async def start_oauth(
    *,
    business_id: str,
    admin_id: str,
    item: dict[str, Any],
    server_url: str,
    static_headers: dict[str, str],
) -> dict[str, str]:
    """Discover OAuth metadata, register Reflexo, and return an authorization URL."""
    from mcp.client.auth.oauth2 import PKCEParameters
    from mcp.shared.auth_utils import resource_url_from_server_url
    from mcp.client.auth.utils import (
        build_oauth_authorization_server_metadata_discovery_urls,
        build_protected_resource_metadata_discovery_urls,
        extract_resource_metadata_from_www_auth,
        extract_scope_from_www_auth,
    )

    await validate_remote_url(server_url)
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        challenge = await client.get(server_url, headers=static_headers)
        resource_hint = extract_resource_metadata_from_www_auth(challenge)
        prm_urls = build_protected_resource_metadata_discovery_urls(
            resource_hint, server_url
        )
        prm, _ = await _get_json(client, prm_urls)
        auth_servers = prm.get("authorization_servers") or []
        auth_server = str(auth_servers[0]) if auth_servers else ""
        metadata_urls = build_oauth_authorization_server_metadata_discovery_urls(
            auth_server or None, server_url
        )
        metadata, _ = await _get_json(client, metadata_urls)
        authorization_endpoint = str(metadata.get("authorization_endpoint") or "")
        token_endpoint = str(metadata.get("token_endpoint") or "")
        if not authorization_endpoint or not token_endpoint:
            raise ValueError(
                "This MCP server requires authorization but publishes no OAuth "
                "metadata. Add it as a custom server with its API key or bearer token."
            )
        await validate_remote_url(authorization_endpoint)
        await validate_remote_url(token_endpoint)

        metadata_doc_url = (
            f"{_base_url()}/api/admin/mcp-connections/oauth/client-metadata"
        )
        use_cimd = bool(
            metadata.get("client_id_metadata_document_supported")
            and metadata_doc_url.startswith("https://")
        )
        client_info: dict[str, Any]
        if use_cimd:
            client_info = {"client_id": metadata_doc_url}
        else:
            registration_endpoint = str(metadata.get("registration_endpoint") or "")
            if not registration_endpoint:
                raise ValueError(
                    "This provider requires a pre-registered OAuth client"
                )
            await validate_remote_url(registration_endpoint)
            registration = await client.post(
                registration_endpoint, json=client_metadata(include_client_id=False)
            )
            registration.raise_for_status()
            client_info = registration.json()
        client_id = str(client_info.get("client_id") or "")
        if not client_id:
            raise ValueError("OAuth client registration returned no client_id")

    state = secrets.token_urlsafe(32)
    pkce = PKCEParameters.generate()
    resource = str(prm.get("resource") or resource_url_from_server_url(server_url))
    scope = (
        extract_scope_from_www_auth(challenge)
        or " ".join(prm.get("scopes_supported") or [])
        or " ".join(metadata.get("scopes_supported") or [])
    )
    pending = {
        "business_id": business_id,
        "admin_id": admin_id,
        "item": item,
        "server_url": server_url,
        "static_headers": static_headers,
        "token_endpoint": token_endpoint,
        "client_info": client_info,
        "code_verifier": pkce.code_verifier,
        "resource": resource,
        "scope": scope,
        "created_at": int(time.time()),
    }
    from shared.redis_client import get_redis

    await get_redis().set(
        f"mcp_oauth_state:{state}",
        json.dumps(pending),
        ex=OAUTH_STATE_TTL_SECONDS,
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": client_metadata()["redirect_uris"][0],
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = resource
    if scope:
        params["scope"] = scope
    return {"oauth_url": f"{authorization_endpoint}?{urlencode(params)}", "state": state}


async def finish_oauth(*, code: str, state: str) -> dict[str, Any]:
    """Exchange an OAuth callback and persist the connected registry server."""
    from shared.redis_client import get_redis

    redis = get_redis()
    key = f"mcp_oauth_state:{state}"
    raw = await redis.get(key)
    if not raw:
        raise ValueError("OAuth state is invalid or expired")
    await redis.delete(key)
    pending = json.loads(raw)
    client_info = pending["client_info"]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": client_metadata()["redirect_uris"][0],
        "client_id": client_info["client_id"],
        "code_verifier": pending["code_verifier"],
    }
    if pending.get("resource"):
        data["resource"] = pending["resource"]
    data, token_auth = _token_auth(data, client_info)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.post(
            pending["token_endpoint"], data=data, auth=token_auth
        )
        response.raise_for_status()
        token = response.json()
    if not token.get("access_token"):
        raise ValueError("OAuth provider returned no access token")
    token["obtained_at"] = int(time.time())
    oauth_payload = {
        "token": token,
        "token_endpoint": pending["token_endpoint"],
        "client_info": client_info,
        "resource": pending.get("resource") or "",
        "static_headers": pending.get("static_headers") or {},
    }
    item = pending["item"]
    from admin.routes.mcp_connections import _registry_slug
    from agent.mcp_store import (
        get_connection_by_registry_internal,
        insert_connection,
        record_health,
        replace_auth_config,
    )

    existing = await get_connection_by_registry_internal(
        pending["business_id"], item["name"]
    )
    if existing:
        await replace_auth_config(
            pending["business_id"],
            str(existing["id"]),
            auth_config=encrypt_oauth_config(oauth_payload),
            auth_type="oauth",
            auth_status="connected",
        )
        row = existing
    else:
        row = await insert_connection(
            pending["business_id"],
            name=_registry_slug(item["name"]),
            server_url=pending["server_url"],
            description=item.get("description") or item.get("title") or "",
            registry_name=item["name"],
            registry_version=item["version"],
            auth_type="oauth",
            auth_config_override=encrypt_oauth_config(oauth_payload),
        )
    from agent.mcp_connector import invalidate_tool_cache, probe_mcp_server

    headers = {
        **oauth_payload["static_headers"],
        "Authorization": f"Bearer {token['access_token']}",
    }
    probe = await probe_mcp_server(
        server_url=pending["server_url"],
        connection_name=_registry_slug(item["name"]),
        auth_headers=headers,
        business_id=pending["business_id"],
        connection_id=str(row["id"]),
    )
    await record_health(
        pending["business_id"],
        str(row["id"]),
        status=str(probe.get("status") or "error"),
        discovered_tools=list(probe.get("tools") or []),
    )
    invalidate_tool_cache(pending["business_id"])
    return {
        "connection": row,
        "probe": probe,
        "business_id": pending["business_id"],
        "admin_id": pending["admin_id"],
    }


async def oauth_headers(
    business_id: str, connection_id: str, auth_config: Any
) -> dict[str, str]:
    """Return a valid Bearer header, refreshing and rotating credentials if needed."""
    lock = _refresh_locks.setdefault(connection_id, asyncio.Lock())
    async with lock:
        return await _oauth_headers_locked(business_id, connection_id, auth_config)


async def _oauth_headers_locked(
    business_id: str, connection_id: str, auth_config: Any
) -> dict[str, str]:
    try:
        from agent.mcp_store import get_connection_internal

        current = await get_connection_internal(business_id, connection_id)
        if current:
            auth_config = current.get("auth_config") or auth_config
    except Exception:
        # Callers may provide a self-contained config in tests/probes.
        pass
    payload = decrypt_oauth_config(auth_config)
    token = payload.get("token") or {}
    if not token.get("access_token"):
        from agent.mcp_store import mark_auth_status

        await mark_auth_status(business_id, connection_id, "reauth_required")
        raise ValueError("MCP OAuth authorization is missing")
    expires_in = int(token.get("expires_in") or 0)
    expires_at = int(token.get("obtained_at") or 0) + expires_in
    if expires_in and time.time() >= expires_at - 60:
        refresh = token.get("refresh_token")
        if not refresh:
            from agent.mcp_store import mark_auth_status

            await mark_auth_status(business_id, connection_id, "reauth_required")
            raise ValueError("MCP OAuth authorization has expired")
        info = payload.get("client_info") or {}
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": info.get("client_id"),
        }
        if payload.get("resource"):
            data["resource"] = payload["resource"]
        data, token_auth = _token_auth(data, info)
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.post(
                    payload["token_endpoint"], data=data, auth=token_auth
                )
                response.raise_for_status()
                rotated = response.json()
            if not rotated.get("refresh_token"):
                rotated["refresh_token"] = refresh
            rotated["obtained_at"] = int(time.time())
            payload["token"] = rotated
            token = rotated
            from agent.mcp_store import replace_auth_config

            await replace_auth_config(
                business_id,
                connection_id,
                auth_config=encrypt_oauth_config(payload),
            )
        except Exception:
            from agent.mcp_store import mark_auth_status

            await mark_auth_status(business_id, connection_id, "reauth_required")
            raise
    return {
        **(payload.get("static_headers") or {}),
        "Authorization": f"Bearer {token['access_token']}",
    }
