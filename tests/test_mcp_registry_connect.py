"""Registry browsing, direct connect security, and MCP OAuth persistence."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from agent.mcp_registry import normalize_registry_item, normalize_registry_response
from agent.mcp_security import McpSecurityError, resolve_remote, validate_remote_url

BIZ = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _registry_item(
    *,
    status: str = "active",
    latest: bool = True,
    remotes: list[dict] | None = None,
) -> dict:
    return {
        "server": {
            "name": "com.example/crm",
            "title": "Example CRM",
            "description": "CRM tools",
            "version": "1.2.3",
            "icons": [
                {"src": "https://example.com/icon.svg", "mimeType": "image/svg+xml"},
                {"src": "https://example.com/icon.png", "mimeType": "image/png"},
            ],
            "remotes": remotes
            or [
                {
                    "type": "streamable-http",
                    "url": "https://{tenant}.example.com/mcp",
                    "variables": {
                        "tenant": {"isRequired": True, "description": "Tenant"}
                    },
                    "headers": [
                        {
                            "name": "X-API-Key",
                            "isRequired": True,
                            "isSecret": True,
                        }
                    ],
                },
                {"type": "sse", "url": "https://example.com/sse"},
            ],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": status,
                "isLatest": latest,
            }
        },
    }


def test_registry_normalizes_only_latest_active_streamable_http() -> None:
    normalized = normalize_registry_item(_registry_item())
    assert normalized is not None
    assert normalized["name"] == "com.example/crm"
    assert normalized["icon"].endswith(".png")
    assert len(normalized["remotes"]) == 1
    assert normalized["remotes"][0]["variables"]["tenant"]["required"] is True
    assert normalized["remotes"][0]["headers"][0]["secret"] is True
    assert normalize_registry_item(_registry_item(status="deleted")) is None
    assert normalize_registry_item(_registry_item(latest=False)) is None
    assert normalize_registry_item(
        _registry_item(remotes=[{"type": "stdio"}])
    ) is None


def test_registry_response_preserves_cursor_and_filters() -> None:
    response = normalize_registry_response(
        {
            "servers": [_registry_item(), _registry_item(status="deprecated")],
            "metadata": {"nextCursor": "opaque"},
        }
    )
    assert len(response["items"]) == 1
    assert response["next_cursor"] == "opaque"


@pytest.mark.asyncio
async def test_registry_fresh_cache_avoids_upstream() -> None:
    from agent.mcp_registry import search_registry

    cached = {
        "items": [{"name": "cached/server"}],
        "next_cursor": None,
        "_cached_at": 5000,
    }
    with (
        patch("agent.mcp_registry.time.time", return_value=5001),
        patch("agent.mcp_registry._redis_get", new=AsyncMock(return_value=cached)),
        patch("agent.mcp_registry.httpx.AsyncClient") as http_client,
    ):
        result = await search_registry(search="cached", limit=5)
    assert result["items"][0]["name"] == "cached/server"
    http_client.assert_not_called()


def test_registry_declared_inputs_are_strictly_resolved() -> None:
    remote = normalize_registry_item(_registry_item())["remotes"][0]
    url, headers = resolve_remote(
        remote,
        variables={"tenant": "acme"},
        headers={"X-API-Key": "secret"},
    )
    assert url == "https://acme.example.com/mcp"
    assert headers == {"X-API-Key": "secret"}
    with pytest.raises(McpSecurityError, match="Unknown MCP header"):
        resolve_remote(
            remote,
            variables={"tenant": "acme"},
            headers={"Authorization": "surprise"},
        )
    with pytest.raises(McpSecurityError, match="Missing required URL variable"):
        resolve_remote(remote, variables={}, headers={"X-API-Key": "secret"})


@pytest.mark.asyncio
async def test_ssrf_rejects_private_addresses_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(McpSecurityError, match="HTTPS"):
        await validate_remote_url("http://example.com/mcp")
    with patch(
        "agent.mcp_security.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("169.254.169.254", 443))],
    ):
        with pytest.raises(McpSecurityError, match="Private or local"):
            await validate_remote_url("https://metadata.example/mcp")


def _admin_client() -> TestClient:
    from admin.routes.mcp_connections import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email="admin@example.com",
        name="Admin",
        role="admin",
        is_active=True,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_registry_browse_route_is_not_shadowed_by_business_route() -> None:
    with patch(
        "agent.mcp_registry.search_registry",
        new=AsyncMock(return_value={"items": [], "next_cursor": None}),
    ):
        response = _admin_client().get("/api/admin/mcp-connections/registry")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_registry_popular_route_is_not_shadowed_by_business_route() -> None:
    with patch(
        "agent.mcp_registry.list_popular_servers",
        new=AsyncMock(return_value={"items": [{"name": "com.stripe/mcp"}]}),
    ):
        response = _admin_client().get("/api/admin/mcp-connections/registry/popular")
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "com.stripe/mcp"


@pytest.mark.asyncio
async def test_popular_servers_keep_curated_order_and_skip_missing() -> None:
    from agent import mcp_registry

    stripe_payload = _registry_item(
        remotes=[{"type": "streamable-http", "url": "https://mcp.stripe.com"}]
    )
    stripe_payload["server"]["name"] = "com.stripe/mcp"
    stripe_payload["server"]["title"] = "Stripe"
    stripe = normalize_registry_item(stripe_payload)
    notion_payload = _registry_item(
        remotes=[{"type": "streamable-http", "url": "https://mcp.notion.com/mcp"}]
    )
    notion_payload["server"]["name"] = "com.notion/mcp"
    notion_payload["server"]["title"] = "Notion"
    notion = normalize_registry_item(notion_payload)

    async def fake_lookup(name: str, version: str = "latest"):
        del version
        return {"com.stripe/mcp": stripe, "com.notion/mcp": notion}.get(name)

    with (
        patch.object(
            mcp_registry,
            "POPULAR_REGISTRY_NAMES",
            ("com.stripe/mcp", "com.missing/gone", "com.notion/mcp"),
        ),
        patch.object(mcp_registry, "get_registry_server", new=fake_lookup),
        patch.object(mcp_registry, "_redis_get", new=AsyncMock(return_value=None)),
        patch.object(mcp_registry, "_redis_set", new=AsyncMock()),
        patch.object(mcp_registry, "_memory_cache", {}),
    ):
        result = await mcp_registry.list_popular_servers()
    assert [item["name"] for item in result["items"]] == [
        "com.stripe/mcp",
        "com.notion/mcp",
    ]


def test_registry_direct_connect_revalidates_and_persists() -> None:
    item = normalize_registry_item(
        _registry_item(
            remotes=[{"type": "streamable-http", "url": "http://127.0.0.1:8099/mcp"}]
        )
    )
    row = {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "crm",
        "registry_name": "com.example/crm",
    }
    with (
        patch(
            "admin.routes.mcp_connections.require_business",
            new=AsyncMock(return_value={"id": BIZ}),
        ),
        patch("admin.routes.mcp_connections.audit", new=AsyncMock()),
        patch(
            "agent.mcp_registry.get_registry_server",
            new=AsyncMock(return_value=item),
        ),
        patch(
            "agent.mcp_connector.probe_mcp_server",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "status": "healthy",
                    "tools": ["crm__lookup"],
                }
            ),
        ),
        patch(
            "admin.routes.mcp_connections.insert_connection",
            new=AsyncMock(return_value=row),
        ) as insert,
        patch(
            "admin.routes.mcp_connections.get_connection",
            new=AsyncMock(return_value=row),
        ),
        patch("admin.routes.mcp_connections.record_health", new=AsyncMock()),
        patch("agent.mcp_connector.invalidate_tool_cache"),
    ):
        response = _admin_client().post(
            f"/api/admin/mcp-connections/registry/connect/{BIZ}",
            json={
                "registry_name": "com.example/crm",
                "registry_version": "1.2.3",
                "remote_index": 0,
                "variables": {},
                "headers": {},
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True
    assert insert.await_args.kwargs["registry_name"] == "com.example/crm"


def _task_group_status_error(
    status: int = 401, *, url: str = "https://mcp.abmeter.ai", headers: dict | None = None
) -> BaseException:
    """Mimic how anyio hides an HTTP failure inside the MCP client task group."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, headers=headers or {})
    return BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [httpx.HTTPStatusError("Client error", request=request, response=response)],
    )


def test_task_group_error_reveals_the_underlying_status() -> None:
    from agent.mcp_connector import describe_mcp_error

    info = describe_mcp_error(_task_group_status_error())
    assert info["status_code"] == 401
    assert info["auth_required"] is True
    assert "401" in info["message"]
    assert "mcp.abmeter.ai" in info["message"]
    assert "TaskGroup" not in info["message"]


def test_nested_groups_and_cancellation_are_flattened() -> None:
    from agent.mcp_connector import describe_mcp_error

    nested = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [_task_group_status_error(403, url="https://mcp.adplane.ai")],
    )
    info = describe_mcp_error(nested)
    assert info["status_code"] == 403
    assert info["auth_required"] is True

    challenged = describe_mcp_error(
        _task_group_status_error(400, headers={"WWW-Authenticate": "Bearer"})
    )
    assert challenged["auth_required"] is True

    cancelled = describe_mcp_error(
        BaseExceptionGroup("unhandled errors in a TaskGroup", [asyncio.CancelledError()])
    )
    assert cancelled["timeout"] is True
    assert cancelled["auth_required"] is False
    assert "TaskGroup" not in cancelled["message"]


def test_connect_routes_a_401_task_group_into_oauth() -> None:
    item = normalize_registry_item(
        _registry_item(
            remotes=[{"type": "streamable-http", "url": "http://127.0.0.1:8099/mcp"}]
        )
    )
    with (
        patch(
            "admin.routes.mcp_connections.require_business",
            new=AsyncMock(return_value={"id": BIZ}),
        ),
        patch(
            "agent.mcp_registry.get_registry_server",
            new=AsyncMock(return_value=item),
        ),
        patch(
            "agent.mcp_connector._discover_tools_from_server",
            new=AsyncMock(side_effect=_task_group_status_error()),
        ),
        patch(
            "admin.routes.mcp_connections.insert_connection", new=AsyncMock()
        ) as insert,
    ):
        response = _admin_client().post(
            f"/api/admin/mcp-connections/registry/connect/{BIZ}",
            json={
                "registry_name": "com.example/crm",
                "registry_version": "1.2.3",
                "remote_index": 0,
                "variables": {},
                "headers": {},
            },
        )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["connected"] is False
    assert body["requires_oauth"] is True
    assert body["status_code"] == 401
    assert "401" in body["error"]
    assert "TaskGroup" not in body["error"]
    insert.assert_not_awaited()


def test_oauth_payload_is_fernet_encrypted() -> None:
    from agent.mcp_oauth import decrypt_oauth_config, encrypt_oauth_config
    from mcp_servers.meta_mcp.auth.tokens import init_fernet

    init_fernet(Fernet.generate_key().decode())
    payload = {
        "token": {"access_token": "top-secret", "refresh_token": "refresh"},
        "token_endpoint": "https://auth.example/token",
    }
    stored = encrypt_oauth_config(payload)
    serialized = json.dumps(stored)
    assert "top-secret" not in serialized
    assert decrypt_oauth_config(stored) == payload


@pytest.mark.asyncio
async def test_oauth_start_uses_pkce_and_short_lived_state() -> None:
    from agent.mcp_oauth import start_oauth

    class Response:
        status_code = 401
        headers = {"WWW-Authenticate": 'Bearer scope="tools.read"'}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "client_id": "reflexo-client",
                "client_secret": "registered-secret",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

        async def post(self, *_args, **_kwargs):
            return Response()

    redis = AsyncMock()
    metadata = {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://auth.example/token",
        "registration_endpoint": "https://auth.example/register",
    }
    with (
        patch("agent.mcp_oauth.httpx.AsyncClient", return_value=Client()),
        patch(
            "agent.mcp_oauth._get_json",
            new=AsyncMock(
                side_effect=[
                    ({"resource": "https://mcp.example", "authorization_servers": ["https://auth.example"]}, "prm"),
                    (metadata, "metadata"),
                ]
            ),
        ),
        patch("agent.mcp_oauth.validate_remote_url", new=AsyncMock()),
        patch("shared.redis_client.get_redis", return_value=redis),
    ):
        result = await start_oauth(
            business_id=BIZ,
            admin_id="admin-1",
            item={"name": "com.example/crm", "version": "1"},
            server_url="https://mcp.example/mcp",
            static_headers={},
        )
    assert "code_challenge=" in result["oauth_url"]
    assert "resource=https%3A%2F%2Fmcp.example" in result["oauth_url"]
    assert redis.set.await_args.kwargs["ex"] == 600


@pytest.mark.asyncio
async def test_expired_oauth_without_refresh_marks_reauthorization() -> None:
    from agent.mcp_oauth import encrypt_oauth_config, oauth_headers
    from mcp_servers.meta_mcp.auth.tokens import init_fernet

    init_fernet(Fernet.generate_key().decode())
    stored = encrypt_oauth_config(
        {
            "token": {
                "access_token": "expired",
                "obtained_at": 1,
                "expires_in": 1,
            },
            "static_headers": {},
        }
    )
    with patch(
        "agent.mcp_store.mark_auth_status", new=AsyncMock()
    ) as mark:
        with pytest.raises(ValueError, match="expired"):
            await oauth_headers(BIZ, "connection-1", stored)
    mark.assert_awaited_once_with(BIZ, "connection-1", "reauth_required")
