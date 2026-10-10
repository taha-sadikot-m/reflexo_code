from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_hub_token_is_tenant_bound(monkeypatch):
    monkeypatch.setenv("CONNECTION_HUB_SECRET", "test-secret")
    from channel_framework.hub_tokens import issue_hub_token, verify_hub_token

    token = issue_hub_token("business-a")
    assert verify_hub_token(token, "business-a")["business_id"] == "business-a"
    with pytest.raises(ValueError, match="another business"):
        verify_hub_token(token, "business-b")


@pytest.mark.parametrize(
    ("integration_id", "expected"),
    [
        ("calendar", "gmail"),
        ("google-calendar", "gmail"),
        ("google_calendar", "gmail"),
        (" GMAIL ", "gmail"),
        ("outlook", "outlook"),
        (None, ""),
    ],
)
def test_oauth_integration_id_normalizes_calendar_to_gmail(integration_id, expected):
    from channel_framework.mcp_server import _normalize_oauth_integration_id

    assert _normalize_oauth_integration_id(integration_id) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2026-09-17T20:10:14.486000+00:00",
            datetime(2026, 9, 17, 20, 10, 14, 486000, tzinfo=timezone.utc),
        ),
        ("2026-09-17T20:10:14Z", datetime(2026, 9, 17, 20, 10, 14, tzinfo=timezone.utc)),
        ("2026-09-17T20:10:14", datetime(2026, 9, 17, 20, 10, 14, tzinfo=timezone.utc)),
        (None, None),
        ("", None),
    ],
)
def test_subscription_expiry_returns_aware_datetime(value, expected):
    from channel_framework.adapters.base import subscription_expiry

    assert subscription_expiry(value) == expected


def test_hub_http_rejects_another_tenant(monkeypatch):
    monkeypatch.setenv("CONNECTION_HUB_SECRET", "test-secret")
    from channel_framework.connection_hub import router
    from channel_framework.hub_tokens import issue_hub_token

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get(
        "/api/connect/business-b",
        params={"token": issue_hub_token("business-a")},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_oauth_callback_stores_tokens_and_subscription(monkeypatch):
    monkeypatch.setenv("CONNECTION_HUB_SECRET", "test-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://app.test")
    from channel_framework import connection_hub
    from channel_framework.adapters.base import WebhookSubscription
    from channel_framework.hub_tokens import issue_hub_token

    business_id = "11111111-1111-1111-1111-111111111111"
    store = AsyncMock(return_value={"id": "22222222-2222-2222-2222-222222222222"})
    execute = AsyncMock()

    @asynccontextmanager
    async def fake_tenant(_business_id):
        assert _business_id == business_id
        yield type("Conn", (), {"execute": execute})()

    adapter = type(
        "Adapter",
        (),
        {
            "setup_notifications": AsyncMock(
                return_value=WebhookSubscription(
                    subscription_id="watch-1",
                    endpoint_url="https://example.test/webhook",
                    expires_at="2026-09-17T20:10:14.486000+00:00",
                    renewal_interval_hours=24,
                )
            )
        },
    )()
    monkeypatch.setattr(connection_hub, "_exchange_oauth", AsyncMock(return_value={
        "access_token": "access", "refresh_token": "refresh", "expires_in": 3600
    }))
    monkeypatch.setattr(
        connection_hub,
        "_account_info",
        AsyncMock(return_value=("owner@example.com", "Owner")),
    )
    monkeypatch.setattr(connection_hub, "store_tokens", store)
    monkeypatch.setattr(connection_hub, "tenant_connection", fake_tenant)
    monkeypatch.setattr(connection_hub, "get_adapter", lambda _platform: adapter)

    hub_token = issue_hub_token(business_id)
    state = issue_hub_token(
        business_id,
        extra={"platform": "gmail", "hub_token": hub_token},
    )
    response = await connection_hub.oauth_callback("code", state)

    assert response.status_code == 307
    assert f"/connect/{business_id}" in response.headers["location"]
    store.assert_awaited_once()
    adapter.setup_notifications.assert_awaited_once_with(business_id, "access")
    execute.assert_awaited_once()
    # asyncpg binds timestamptz as datetime; an ISO string raises DataError.
    assert execute.await_args.args[6] == datetime(
        2026, 9, 17, 20, 10, 14, 486000, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_disconnect_removes_row_when_provider_teardown_fails(monkeypatch):
    monkeypatch.setenv("CONNECTION_HUB_SECRET", "test-secret")
    from channel_framework import connection_hub
    from channel_framework.hub_tokens import issue_hub_token

    adapter = type(
        "Adapter",
        (),
        {"teardown_notifications": AsyncMock(side_effect=RuntimeError("provider down"))},
    )()
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(connection_hub, "get_adapter", lambda _platform: adapter)
    monkeypatch.setattr(connection_hub, "delete_account", delete)

    result = await connection_hub.disconnect(
        "business-a", "gmail", issue_hub_token("business-a")
    )
    assert result["status"] == "disconnected"
    adapter.teardown_notifications.assert_awaited_once_with("business-a")
    delete.assert_awaited_once_with("business-a", "gmail")


def test_production_python_has_no_nango_client_import():
    root = Path(__file__).resolve().parents[1]
    production_dirs = [
        "admin", "agent", "api", "channel_framework", "identity",
        "mcp_servers", "onboarding", "shared", "voice", "workflow_builder",
    ]
    offenders = []
    for directory in production_dirs:
        for path in (root / directory).rglob("*.py"):
            if "integrations.nango_client" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_nango_runtime_removed_from_compose():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    assert "nango-server" not in compose
    assert "nango-db" not in compose
