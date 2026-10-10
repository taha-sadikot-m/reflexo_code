"""Stage 5 comment capabilities, catalog-on-comment, and reconnect URL."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from agent.specialist_prompts import build_social_prompt
from integrations.capabilities import (
    debug_token,
    derive_feature_status,
    fetch_app_permission_status,
    fetch_app_subscriptions,
    fetch_page_subscribed_fields,
)
from shared.logging_filters import RedactHttpCredentials
from tests.test_admin_setup_links import BIZ_A, _user


def test_missing_instagram_manage_comments_blocks_replies():
    features = {row["id"]: row for row in derive_feature_status(
        integration="instagram",
        granted_scopes=["instagram_manage_messages", "instagram_basic"],
        page_fields=["messages"],
        app_subscriptions={"instagram": ["comments"]},
    )}
    assert features["instagram_dm"]["live"] is True
    assert features["instagram_comment"]["blocked"] is True
    assert "instagram_manage_comments" in features["instagram_comment"]["missing_scopes"]


def test_missing_page_feed_blocks_facebook_comment_replies():
    features = {row["id"]: row for row in derive_feature_status(
        integration="facebook",
        granted_scopes=["pages_messaging", "pages_read_engagement", "pages_manage_engagement"],
        page_fields=["messages"],
        app_subscriptions={},
    )}
    assert features["facebook_messenger"]["live"] is True
    assert features["facebook_comment"]["blocked"] is True
    assert "feed" in features["facebook_comment"]["missing_page_fields"]


def test_token_scope_does_not_override_unapproved_app_permission():
    features = {row["id"]: row for row in derive_feature_status(
        integration="instagram",
        granted_scopes=["instagram_manage_messages", "instagram_basic"],
        page_fields=["messages"],
        app_subscriptions={},
        app_permissions={"instagram_manage_messages": "unapproved"},
    )}
    dm = features["instagram_dm"]
    assert dm["missing_token_scopes"] == []
    assert dm["unapproved_app_permissions"] == ["instagram_manage_messages"]
    assert dm["blocked"] is True
    assert "app permissions not approved" in dm["reason"]


def test_checked_empty_app_permissions_are_not_reported_live():
    features = derive_feature_status(
        integration="instagram",
        granted_scopes=["instagram_manage_messages"],
        page_fields=["messages"],
        app_subscriptions={},
        app_permissions={},
    )
    dm = next(row for row in features if row["id"] == "instagram_dm")
    assert dm["blocked"] is True
    assert dm["unapproved_app_permissions"] == ["instagram_manage_messages"]


class _GraphResponse:
    status_code = 200
    text = "{}"
    content = b"{}"

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _GraphClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/debug_token"):
            return _GraphResponse({"data": {"is_valid": True, "scopes": []}})
        if url.endswith("/subscriptions"):
            return _GraphResponse({"data": []})
        if url.endswith("/permissions"):
            return _GraphResponse(
                {
                    "data": [
                        {
                            "permission": "instagram_manage_messages",
                            "status": "unapproved",
                        }
                    ]
                }
            )
        return _GraphResponse(
            {
                "data": [
                    {
                        "id": "",
                        "subscribed_fields": ["messages", "feed"],
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_capability_graph_credentials_are_not_in_urls():
    client = _GraphClient()
    await debug_token(client, "user-secret", "app-id", "app-secret")
    await fetch_app_subscriptions(client, "app-id", "app-secret")
    statuses = await fetch_app_permission_status(client, "app-id", "app-secret")
    fields = await fetch_page_subscribed_fields(client, "page-id", "page-secret")

    assert statuses["instagram_manage_messages"] == "unapproved"
    assert fields == ["messages", "feed"]
    for url, kwargs in client.calls:
        assert "user-secret" not in url
        assert "app-secret" not in url
        assert "page-secret" not in url
        assert "access_token" not in url
        if url.endswith("/debug_token"):
            assert kwargs["params"] == {"input_token": "user-secret"}
            assert kwargs["headers"]["Authorization"] == "Bearer app-id|app-secret"


def test_httpx_log_filter_redacts_required_debug_token_query():
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        (
            "HTTP Request: GET https://graph.facebook.com/debug_token?"
            "input_token=user-secret&access_token=app-id%7Capp-secret"
        ),
        (),
        None,
    )
    assert RedactHttpCredentials().filter(record) is True
    rendered = record.getMessage()
    assert "user-secret" not in rendered
    assert "app-secret" not in rendered
    assert "input_token=[REDACTED]" in rendered


@pytest.mark.parametrize(
    "url,secrets",
    [
        (
            "https://graph.facebook.com/oauth/access_token?"
            "client_secret=secret-1&code=oauth-code&fb_exchange_token=user-token",
            ("secret-1", "oauth-code", "user-token"),
        ),
        (
            "https://graph.facebook.com/me?"
            "access_token=page-token&appsecret_proof=proof-value",
            ("page-token", "proof-value"),
        ),
        (
            "https://example.test/redirect?"
            "fb_exchange_token%3Dencoded-token%26input_token%3Ddebug-token",
            ("encoded-token", "debug-token"),
        ),
    ],
)
def test_httpx_rendered_request_redacts_all_meta_credentials(url, secrets):
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 200, "OK"),
        None,
    )
    assert RedactHttpCredentials().filter(record) is True
    rendered = record.getMessage()
    for secret in secrets:
        assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_connected_meta_ui_offers_repair_and_fresh_reconnect():
    source = (
        Path(__file__).resolve().parents[1]
        / "admin-panel"
        / "app"
        / "(app)"
        / "integrations"
        / "page.tsx"
    ).read_text(encoding="utf-8")
    assert 'mode: item.connected ? "rerequest" : "normal"' in source
    assert 'item.connected ? "Repair permissions" : "Open connect"' in source
    assert 'mode: "fresh"' in source
    assert "Fresh reconnect" in source
    assert "reconnecting cannot" in source
    assert "unapprovedAppPermissions.map" in source
    assert "app review required" in source


def test_social_prompt_uses_catalog_before_gap():
    prompt = build_social_prompt({}, None, "instagram_comment", False, [], [])
    assert "list_entities" in prompt
    assert "Never log_gap when a catalog row answers" in prompt
    assert "Public catalog names and prices ARE allowed" in prompt


def test_capabilities_endpoint_never_returns_tokens(monkeypatch):
    from admin.routes.setup_links import router

    payload = {
        "business_id": BIZ_A,
        "app_subscriptions": {"instagram": ["comments"]},
        "instagram_comments_subscribed": True,
        "items": [
            {
                "channel": "instagram_dm",
                "account": "holoncode",
                "granted_scopes": ["instagram_manage_messages"],
                "token_valid": True,
                "page_fields": ["messages"],
                "features": [],
            }
        ],
    }
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: _user("admin")
    monkeypatch.setattr(
        "admin.routes.setup_links.require_business",
        AsyncMock(return_value={"id": BIZ_A}),
    )
    monkeypatch.setattr(
        "integrations.capabilities.inspect_business_capabilities",
        AsyncMock(return_value=payload),
    )
    client = TestClient(app)
    res = client.get(f"/api/admin/integrations/{BIZ_A}/capabilities")
    assert res.status_code == 200
    body = res.json()
    dumped = str(body).lower()
    assert "access_token" not in dumped
    assert body["instagram_comments_subscribed"] is True


def test_admin_cannot_subscribe_instagram_comments():
    from admin.routes.setup_links import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: _user("admin")
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(f"/api/admin/integrations/{BIZ_A}/subscribe-instagram-comments")
    assert res.status_code == 403


def test_admin_oauth_url_fresh_mode(monkeypatch):
    from admin.routes.setup_links import router

    app = FastAPI()
    app.include_router(router)
    user: AdminUser = _user("admin")
    app.dependency_overrides[get_current_admin] = lambda: user
    monkeypatch.setattr(
        "admin.routes.setup_links.require_business",
        AsyncMock(return_value={"id": BIZ_A}),
    )
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    mint = AsyncMock(
        return_value={
            "integration_id": "facebook",
            "oauth_url": (
                "https://facebook.com/dialog/oauth?"
                "auth_type=reauthenticate%2Crerequest"
            ),
            "mode": "fresh",
        }
    )
    monkeypatch.setattr(
        "admin.routes.setup_links.mint_oauth_url",
        mint,
    )
    client = TestClient(app)
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/oauth-url",
        json={"integration_id": "facebook", "mode": "fresh"},
    )
    assert res.status_code == 200
    assert "reauthenticate" in res.json()["oauth_url"]
    mint.assert_awaited_once_with(BIZ_A, "facebook", mode="fresh")


# ── webhook subscription controls ──────────────────────────────────────────


class _SubscribeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.text = str(payload)

    def json(self):
        return self.payload


class _SubscribeClient:
    """Async-context httpx stand-in that records POSTs."""

    def __init__(self, response=None):
        self.posts: list[tuple[str, dict]] = []
        self.response = response or _SubscribeResponse({"success": True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


def _meta_env(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app-id")
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("META_WEBHOOK_URL", "https://example.test/webhook")


@pytest.mark.asyncio
async def test_subscribe_app_object_fields_sends_comma_joined_fields(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    client = _SubscribeClient()
    monkeypatch.setattr(capabilities.httpx, "AsyncClient", lambda **_kw: client)

    result = await capabilities.subscribe_app_object_fields(
        "instagram", ["messages", "comments"]
    )

    assert result["fields"] == ["messages", "comments"]
    url, kwargs = client.posts[0]
    assert url.endswith("/app-id/subscriptions")
    assert kwargs["data"]["object"] == "instagram"
    assert kwargs["data"]["fields"] == "messages,comments"
    assert kwargs["data"]["verify_token"] == "verify-me"
    assert kwargs["data"]["callback_url"] == "https://example.test/webhook"


@pytest.mark.asyncio
async def test_subscribe_app_object_fields_surfaces_graph_error(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    failure = _SubscribeResponse(
        {"error": {"message": "Object instagram is not available"}}, status_code=400
    )
    monkeypatch.setattr(
        capabilities.httpx, "AsyncClient", lambda **_kw: _SubscribeClient(failure)
    )

    with pytest.raises(RuntimeError, match="Object instagram is not available"):
        await capabilities.subscribe_app_object_fields("instagram", ["comments"])


@pytest.mark.asyncio
async def test_subscribe_app_object_fields_rejects_empty_fields(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    with pytest.raises(RuntimeError, match="At least one webhook field"):
        await capabilities.subscribe_app_object_fields("instagram", [" "])


@pytest.mark.asyncio
async def test_instagram_comments_wrapper_still_subscribes_comments(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    client = _SubscribeClient()
    monkeypatch.setattr(capabilities.httpx, "AsyncClient", lambda **_kw: client)

    result = await capabilities.subscribe_instagram_app_comments()

    assert result["object"] == "instagram"
    assert result["fields"] == ["comments"]
    assert client.posts[0][1]["data"]["fields"] == "comments"


@pytest.mark.asyncio
async def test_subscribe_page_webhook_fields_posts_subscribed_fields(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    client = _SubscribeClient()
    monkeypatch.setattr(capabilities.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        capabilities,
        "_load_channel_token",
        AsyncMock(return_value=("page-123", "user-token")),
    )
    monkeypatch.setattr(
        capabilities,
        "resolve_page_for_channel",
        AsyncMock(return_value=("page-123", "page-token")),
    )
    monkeypatch.setattr(
        capabilities,
        "fetch_page_subscribed_fields",
        AsyncMock(return_value=["messages", "feed"]),
    )

    result = await capabilities.subscribe_page_webhook_fields(
        BIZ_A, "facebook_messenger", ["feed", "messages"]
    )

    assert result["page_id"] == "page-123"
    assert result["subscribed_fields"] == ["messages", "feed"]
    url, kwargs = client.posts[0]
    assert url.endswith("/page-123/subscribed_apps")
    assert kwargs["data"]["subscribed_fields"] == "feed,messages"
    assert kwargs["headers"]["Authorization"] == "Bearer page-token"
    assert "page-token" not in url


@pytest.mark.asyncio
async def test_subscribe_page_webhook_fields_requires_resolvable_page(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    monkeypatch.setattr(
        capabilities.httpx, "AsyncClient", lambda **_kw: _SubscribeClient()
    )
    monkeypatch.setattr(
        capabilities,
        "_load_channel_token",
        AsyncMock(return_value=("ig-1", "user-token")),
    )
    monkeypatch.setattr(
        capabilities,
        "resolve_page_for_channel",
        AsyncMock(return_value=(None, None)),
    )

    with pytest.raises(RuntimeError, match="Could not resolve a Facebook Page"):
        await capabilities.subscribe_page_webhook_fields(
            BIZ_A, "facebook_messenger", ["feed"]
        )


@pytest.mark.asyncio
async def test_inspect_reports_instagram_messages_subscription(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    monkeypatch.setattr(
        capabilities.httpx, "AsyncClient", lambda **_kw: _SubscribeClient()
    )
    monkeypatch.setattr(capabilities, "_fetch_channel_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        capabilities,
        "fetch_app_subscriptions",
        AsyncMock(return_value={"instagram": ["messages", "comments"]}),
    )
    monkeypatch.setattr(
        capabilities, "fetch_app_permission_status", AsyncMock(return_value={})
    )

    result = await capabilities.inspect_business_capabilities(BIZ_A)

    assert result["instagram_messages_subscribed"] is True
    assert result["instagram_comments_subscribed"] is True


@pytest.mark.asyncio
async def test_inspect_reports_missing_instagram_messages_subscription(monkeypatch):
    from integrations import capabilities

    _meta_env(monkeypatch)
    monkeypatch.setattr(
        capabilities.httpx, "AsyncClient", lambda **_kw: _SubscribeClient()
    )
    monkeypatch.setattr(capabilities, "_fetch_channel_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        capabilities,
        "fetch_app_subscriptions",
        AsyncMock(return_value={"instagram": ["comments"]}),
    )
    monkeypatch.setattr(
        capabilities, "fetch_app_permission_status", AsyncMock(return_value={})
    )

    result = await capabilities.inspect_business_capabilities(BIZ_A)

    assert result["instagram_messages_subscribed"] is False
    assert result["instagram_comments_subscribed"] is True


def _subscribe_app(role: str) -> FastAPI:
    from admin.routes.setup_links import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: _user(role)
    return app


def test_admin_cannot_subscribe_page_fields_or_instagram_webhooks():
    client = TestClient(_subscribe_app("admin"), raise_server_exceptions=False)
    assert client.post(
        f"/api/admin/integrations/{BIZ_A}/subscribe-page-fields", json={}
    ).status_code == 403
    assert client.post(
        f"/api/admin/integrations/{BIZ_A}/subscribe-instagram-webhooks", json={}
    ).status_code == 403


def test_super_admin_subscribes_page_feed_by_default(monkeypatch):
    monkeypatch.setattr(
        "admin.routes.setup_links.require_business",
        AsyncMock(return_value={"id": BIZ_A}),
    )
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    subscribe = AsyncMock(
        return_value={"ok": True, "page_id": "page-123", "subscribed_fields": ["feed"]}
    )
    monkeypatch.setattr(
        "integrations.capabilities.subscribe_page_webhook_fields", subscribe
    )

    client = TestClient(_subscribe_app("super_admin"))
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/subscribe-page-fields", json={}
    )

    assert res.status_code == 200
    assert res.json()["page_id"] == "page-123"
    subscribe.assert_awaited_once_with(BIZ_A, "facebook_messenger", ["feed", "messages"])


def test_super_admin_subscribes_instagram_messages_and_comments(monkeypatch):
    monkeypatch.setattr(
        "admin.routes.setup_links.require_business",
        AsyncMock(return_value={"id": BIZ_A}),
    )
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    subscribe = AsyncMock(return_value={"ok": True, "fields": ["messages", "comments"]})
    monkeypatch.setattr(
        "integrations.capabilities.subscribe_app_object_fields", subscribe
    )

    client = TestClient(_subscribe_app("super_admin"))
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/subscribe-instagram-webhooks", json={}
    )

    assert res.status_code == 200
    subscribe.assert_awaited_once_with("instagram", ["messages", "comments"])


def test_page_subscribe_graph_failure_is_502(monkeypatch):
    monkeypatch.setattr(
        "admin.routes.setup_links.require_business",
        AsyncMock(return_value={"id": BIZ_A}),
    )
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    monkeypatch.setattr(
        "integrations.capabilities.subscribe_page_webhook_fields",
        AsyncMock(side_effect=RuntimeError("page subscribed_apps HTTP 400: nope")),
    )

    client = TestClient(_subscribe_app("super_admin"), raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/subscribe-page-fields", json={}
    )

    assert res.status_code == 502
    assert "subscribed_apps" in res.json()["detail"]


def test_integrations_ui_exposes_subscribe_controls():
    source = (
        Path(__file__).resolve().parents[1]
        / "admin-panel"
        / "app"
        / "(app)"
        / "integrations"
        / "page.tsx"
    ).read_text(encoding="utf-8")
    assert "subscribe-page-fields" in source
    assert "subscribe-instagram-webhooks" in source
    assert "Subscribe Page feed" in source
    assert "Page feed webhook:" in source
    assert "Instagram messages webhook:" in source
