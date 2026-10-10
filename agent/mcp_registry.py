"""Read-only client for the official MCP Registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REGISTRY_BASE_URL = os.getenv(
    "MCP_REGISTRY_URL", "https://registry.modelcontextprotocol.io"
).rstrip("/")
REGISTRY_CACHE_SECONDS = max(60, int(os.getenv("MCP_REGISTRY_CACHE_SECONDS", "3600")))
REGISTRY_STALE_SECONDS = max(
    REGISTRY_CACHE_SECONDS, int(os.getenv("MCP_REGISTRY_STALE_SECONDS", "86400"))
)
_memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}
POPULAR_CACHE_KEY = "mcp_registry:popular:v1"
# Official Streamable HTTP remotes shown first in Browse. Order is intentional.
POPULAR_REGISTRY_NAMES: tuple[str, ...] = (
    "io.github.github/github-mcp-server",
    "com.stripe/mcp",
    "com.notion/mcp",
    "com.supabase/mcp",
    "com.atlassian/atlassian-mcp-server",
    "com.figma.mcp/mcp",
    "com.paypal.mcp/mcp",
    "com.cloudflare.mcp/mcp",
)


def _cache_key(search: str, cursor: str, limit: int) -> str:
    digest = hashlib.sha256(
        f"{search}\0{cursor}\0{limit}".encode("utf-8")
    ).hexdigest()[:32]
    return f"mcp_registry:v1:{digest}"


def _safe_icon(server: dict[str, Any]) -> str | None:
    for icon in server.get("icons") or []:
        if not isinstance(icon, dict):
            continue
        src = str(icon.get("src") or "")
        mime = str(icon.get("mimeType") or "").lower()
        if src.startswith("https://") and mime in {
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/webp",
        }:
            return src
    return None


def _input_spec(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    choices = raw.get("choices")
    return {
        "description": str(raw.get("description") or "")[:500],
        "required": bool(raw.get("isRequired", False)),
        "secret": bool(raw.get("isSecret", False)),
        "default": raw.get("default"),
        "choices": list(choices)[:100] if isinstance(choices, list) else [],
    }


def normalize_registry_item(item: Any) -> dict[str, Any] | None:
    """Return an admin-safe latest Streamable HTTP registry item."""
    if not isinstance(item, dict) or not isinstance(item.get("server"), dict):
        return None
    server = item["server"]
    official = ((item.get("_meta") or {}).get(
        "io.modelcontextprotocol.registry/official"
    ) or {})
    if official.get("status", "active") != "active":
        return None
    if official.get("isLatest") is False:
        return None

    remotes: list[dict[str, Any]] = []
    for idx, remote in enumerate(server.get("remotes") or []):
        if not isinstance(remote, dict) or remote.get("type") != "streamable-http":
            continue
        url = str(remote.get("url") or "").strip()
        if not url:
            continue
        variables = remote.get("variables") if isinstance(remote.get("variables"), dict) else {}
        headers: list[dict[str, Any]] = []
        for header in remote.get("headers") or []:
            if not isinstance(header, dict) or not header.get("name"):
                continue
            headers.append(
                {"name": str(header["name"])[:128], **_input_spec(header)}
            )
        remotes.append(
            {
                "index": idx,
                "url": url,
                "variables": {
                    str(name): _input_spec(spec)
                    for name, spec in variables.items()
                    if str(name)
                },
                "headers": headers,
            }
        )
    if not remotes:
        return None

    name = str(server.get("name") or "").strip()
    version = str(server.get("version") or "").strip()
    if not name or not version:
        return None
    repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    return {
        "name": name,
        "version": version,
        "title": str(server.get("title") or name.split("/")[-1])[:200],
        "description": str(server.get("description") or "")[:1000],
        "icon": _safe_icon(server),
        "repository_url": str(repository.get("url") or "")[:1000] or None,
        "remotes": remotes,
    }


def normalize_registry_response(payload: Any) -> dict[str, Any]:
    raw_servers = payload.get("servers") if isinstance(payload, dict) else []
    items = [
        normalized
        for raw in (raw_servers or [])
        if (normalized := normalize_registry_item(raw)) is not None
    ]
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    return {
        "items": items,
        "next_cursor": str((metadata or {}).get("nextCursor") or "") or None,
    }


async def _redis_get(key: str) -> dict[str, Any] | None:
    try:
        from shared.redis_client import get_redis

        raw = await get_redis().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _redis_set(key: str, value: dict[str, Any]) -> None:
    try:
        from shared.redis_client import get_redis

        await get_redis().set(key, json.dumps(value), ex=REGISTRY_STALE_SECONDS)
    except Exception:
        pass


async def search_registry(
    *, search: str = "", cursor: str = "", limit: int = 24
) -> dict[str, Any]:
    """Search connectable remote servers with fresh/stale cache fallback."""
    search = search.strip()[:200]
    cursor = cursor.strip()[:1000]
    limit = min(max(int(limit), 1), 100)
    key = _cache_key(search, cursor, limit)
    now = time.time()
    cached = await _redis_get(key)
    if cached is None:
        local = _memory_cache.get(key)
        cached = local[1] if local and now - local[0] < REGISTRY_STALE_SECONDS else None
    if cached and now - float(cached.get("_cached_at", 0)) < REGISTRY_CACHE_SECONDS:
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    params: dict[str, Any] = {"limit": limit, "version": "latest"}
    if search:
        params["search"] = search
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(f"{REGISTRY_BASE_URL}/v0.1/servers", params=params)
            response.raise_for_status()
            result = normalize_registry_response(response.json())
        stored = {**result, "_cached_at": now}
        _memory_cache[key] = (now, stored)
        await _redis_set(key, stored)
        return result
    except Exception:
        if cached:
            logger.warning("MCP Registry unavailable; serving stale cache", exc_info=True)
            return {k: v for k, v in cached.items() if not k.startswith("_")}
        raise


async def get_registry_server(name: str, version: str = "latest") -> dict[str, Any] | None:
    """Fetch and normalize one registry record for server-side revalidation."""
    from urllib.parse import quote

    encoded_name = quote(name, safe="")
    encoded_version = quote(version or "latest", safe="")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.get(
            f"{REGISTRY_BASE_URL}/v0.1/servers/{encoded_name}/versions/{encoded_version}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    # Detail endpoint returns one ServerResponse rather than a list.
    return normalize_registry_item(payload)


async def _lookup_popular(name: str) -> dict[str, Any] | None:
    try:
        return await get_registry_server(name)
    except Exception:
        logger.warning("MCP popular lookup failed name=%s", name, exc_info=True)
        return None


async def list_popular_servers() -> dict[str, Any]:
    """Return curated official remotes in boost order, skipping missing records."""
    now = time.time()
    cached = await _redis_get(POPULAR_CACHE_KEY)
    if cached is None:
        local = _memory_cache.get(POPULAR_CACHE_KEY)
        cached = local[1] if local and now - local[0] < REGISTRY_STALE_SECONDS else None
    if cached and now - float(cached.get("_cached_at", 0)) < REGISTRY_CACHE_SECONDS:
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    lookups = await asyncio.gather(*(_lookup_popular(name) for name in POPULAR_REGISTRY_NAMES))
    by_name = {item["name"]: item for item in lookups if item}
    items = [by_name[name] for name in POPULAR_REGISTRY_NAMES if name in by_name]
    result = {"items": items}
    stored = {**result, "_cached_at": now}
    _memory_cache[POPULAR_CACHE_KEY] = (now, stored)
    await _redis_set(POPULAR_CACHE_KEY, stored)
    return result
