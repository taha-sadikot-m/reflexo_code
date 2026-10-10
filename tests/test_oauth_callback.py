"""The Meta OAuth callback must complete the round-trip and fail safely.

Before this route existed, Meta redirected the owner to /api/oauth/callback on
Meta MCP and got a 404: the login succeeded, the code was valid, and nothing
consumed it. These cover the endpoint and the orchestration behind it.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from mcp_servers.meta_mcp import server
from onboarding import integrations

BUSINESS = "918750d7-2254-4cfd-bd02-c22c3ab708ec"


def _state(integration: str = "instagram", business_id: str = BUSINESS) -> str:
    return integrations.encode_oauth_state(
        {"business_id": business_id, "integration": integration, "redirect": "onboarding"}
    )


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/oauth/callback",
            "query_string": query.encode("utf-8"),
            "headers": [],
        }
    )


async def _body(response) -> str:
    return response.body.decode("utf-8")


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch):
    """Replace every outbound Graph call and DB write with recorders."""
    calls: dict[str, list] = {
        "exchange": [],
        "store_ig": [],
        "store_fb": [],
        "subscribe": [],
        "notify": [],
        "progress": [],
    }

    async def exchange(code: str):
        calls["exchange"].append(code)
        return {"access_token": "long-lived-token", "expires_in": 5184000}

    async def store_ig(business_id, accounts, fallback_token, expires_in=None):
        calls["store_ig"].append((business_id, accounts, fallback_token, expires_in))

    async def store_fb(business_id, pages, fallback_token, expires_in=None):
        calls["store_fb"].append((business_id, pages, fallback_token, expires_in))

    async def subscribe(page_id, page_token, fields):
        calls["subscribe"].append((page_id, page_token, fields))

    async def notify(business_id, text):
        calls["notify"].append((business_id, text))

    async def progress(business_id, integration):
        calls["progress"].append((business_id, integration))

    monkeypatch.setattr(integrations, "exchange_meta_code_for_token", exchange)
    monkeypatch.setattr(integrations, "store_instagram_channels", store_ig)
    monkeypatch.setattr(integrations, "store_facebook_channels", store_fb)
    monkeypatch.setattr(integrations, "subscribe_page_webhooks", subscribe)
    monkeypatch.setattr(integrations, "notify_owner_text", notify)
    monkeypatch.setattr(integrations, "_mark_channel_connected", progress)
    return calls


def _ig_accounts(monkeypatch: pytest.MonkeyPatch, accounts: list[dict]) -> None:
    async def get_ig(token: str):
        return accounts

    monkeypatch.setattr(integrations, "get_instagram_accounts", get_ig)


def _fb_pages(monkeypatch: pytest.MonkeyPatch, pages: list[dict]) -> None:
    async def get_fb(token: str):
        return pages

    monkeypatch.setattr(integrations, "get_facebook_pages", get_fb)


# ── State signing ────────────────────────────────────────────────────────────


def test_state_round_trips() -> None:
    decoded = integrations.decode_oauth_state(_state("facebook"))
    assert decoded["business_id"] == BUSINESS
    assert decoded["integration"] == "facebook"


def test_tampered_state_is_rejected() -> None:
    body, sig = _state().rsplit(".", 1)
    with pytest.raises(ValueError, match="invalid_state_signature"):
        integrations.decode_oauth_state(f"{body}.{'0' * len(sig)}")


@pytest.mark.asyncio
async def test_tampered_state_performs_no_token_exchange(graph) -> None:
    body, sig = _state().rsplit(".", 1)
    with pytest.raises(ValueError):
        await integrations.complete_meta_oauth("code", f"{body}.{'0' * len(sig)}")
    assert graph["exchange"] == [], "a forged callback must never reach Graph"


# ── Orchestration ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instagram_happy_path_stores_and_subscribes(
    graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ig_accounts(
        monkeypatch,
        [
            {
                "id": "17841400000000000",
                "username": "holoncode",
                "page_id": "PAGE123",
                "page_access_token": "page-token",
            }
        ],
    )

    result = await integrations.complete_meta_oauth("the-code", _state("instagram"))

    assert result["ok"] is True
    assert result["accounts"] == ["holoncode"]
    assert result["subscription_warnings"] == []
    assert graph["exchange"] == ["the-code"]

    business_id, accounts, token, expires_in = graph["store_ig"][0]
    assert business_id == BUSINESS
    assert accounts[0]["id"] == "17841400000000000"
    assert token == "long-lived-token"
    assert expires_in == 5184000, "expiry must be persisted or refresh never fires"

    page_id, page_token, fields = graph["subscribe"][0]
    assert page_id == "PAGE123", "Instagram webhooks arrive via the linked Page"
    assert page_token == "page-token"
    assert fields == integrations.INSTAGRAM_WEBHOOK_FIELDS
    assert graph["store_fb"] == []

    assert graph["progress"] == [(BUSINESS, "instagram")]
    assert "holoncode" in graph["notify"][0][1]


@pytest.mark.asyncio
async def test_facebook_happy_path_uses_page_fields(
    graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fb_pages(monkeypatch, [{"id": "PAGE999", "name": "HolonCode", "access_token": "pt"}])

    result = await integrations.complete_meta_oauth("c", _state("facebook"))

    assert result["ok"] is True
    assert result["accounts"] == ["HolonCode"]
    assert graph["subscribe"][0] == ("PAGE999", "pt", integrations.FACEBOOK_WEBHOOK_FIELDS)
    assert graph["store_ig"] == []


@pytest.mark.asyncio
async def test_no_instagram_account_reports_reason(graph, monkeypatch) -> None:
    _ig_accounts(monkeypatch, [])
    result = await integrations.complete_meta_oauth("c", _state("instagram"))
    assert result == {
        "ok": False,
        "reason": "no_instagram_account",
        "integration": "instagram",
    }
    assert graph["store_ig"] == []
    assert graph["progress"] == [], "a failed connect must not mark onboarding done"
    assert graph["notify"], "the owner is told why it failed"


@pytest.mark.asyncio
async def test_no_facebook_page_reports_reason(graph, monkeypatch) -> None:
    _fb_pages(monkeypatch, [])
    result = await integrations.complete_meta_oauth("c", _state("facebook"))
    assert result["reason"] == "no_facebook_page"


@pytest.mark.asyncio
async def test_subscription_failure_keeps_the_connection(graph, monkeypatch) -> None:
    _ig_accounts(
        monkeypatch,
        [{"id": "IG1", "username": "shop", "page_id": "P1", "page_access_token": "t"}],
    )

    async def boom(page_id, page_token, fields):
        raise RuntimeError("insufficient permissions")

    monkeypatch.setattr(integrations, "subscribe_page_webhooks", boom)

    result = await integrations.complete_meta_oauth("c", _state("instagram"))

    assert result["ok"] is True, "a failed subscription must not discard the token"
    assert len(result["subscription_warnings"]) == 1
    assert "insufficient permissions" in result["subscription_warnings"][0]
    assert graph["store_ig"], "the channel row is still written"


@pytest.mark.asyncio
async def test_unknown_integration_is_refused(graph) -> None:
    result = await integrations.complete_meta_oauth("c", _state("tiktok"))
    assert result["reason"] == "unknown_integration"
    assert graph["exchange"] == []


# ── Expiry conversion ────────────────────────────────────────────────────────


def test_expiry_from_seconds_is_in_the_future() -> None:
    from datetime import datetime, timezone

    expires_at = integrations._expiry_from(3600)
    assert expires_at is not None
    delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta <= 3600


def test_expiry_from_none_stays_null() -> None:
    assert integrations._expiry_from(None) is None
    assert integrations._expiry_from(0) is None


# ── HTTP route ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_renders_success_page(graph, monkeypatch) -> None:
    _ig_accounts(
        monkeypatch,
        [{"id": "IG1", "username": "holoncode", "page_id": "P1", "page_access_token": "t"}],
    )

    response = await server.oauth_callback(_request(f"code=abc&state={_state('instagram')}"))

    assert response.status_code == 200
    assert response.media_type == "text/html"
    body = await _body(response)
    assert "Instagram connected" in body
    assert "holoncode" in body


@pytest.mark.asyncio
async def test_route_handles_user_cancellation(graph) -> None:
    response = await server.oauth_callback(
        _request("error=access_denied&error_reason=user_denied")
    )
    assert response.status_code == 200, "a cancelled login is not a server error"
    assert "Connection cancelled" in await _body(response)
    assert graph["exchange"] == []


@pytest.mark.asyncio
async def test_route_rejects_missing_code(graph) -> None:
    response = await server.oauth_callback(_request(f"state={_state()}"))
    assert response.status_code == 400
    assert graph["exchange"] == []


@pytest.mark.asyncio
async def test_route_rejects_forged_state(graph) -> None:
    body, sig = _state().rsplit(".", 1)
    response = await server.oauth_callback(
        _request(f"code=abc&state={body}.{'0' * len(sig)}")
    )
    assert response.status_code == 400
    assert "could not be verified" in await _body(response)
    assert graph["exchange"] == []


@pytest.mark.asyncio
async def test_route_explains_missing_instagram_account(graph, monkeypatch) -> None:
    _ig_accounts(monkeypatch, [])
    response = await server.oauth_callback(
        _request(f"code=abc&state={_state('instagram')}")
    )
    assert response.status_code == 400
    assert "professional account" in await _body(response)


@pytest.mark.asyncio
async def test_route_hides_stack_traces(graph, monkeypatch) -> None:
    async def boom(token: str):
        raise RuntimeError("graph exploded with secret detail")

    monkeypatch.setattr(integrations, "get_instagram_accounts", boom)

    response = await server.oauth_callback(
        _request(f"code=abc&state={_state('instagram')}")
    )

    body = await _body(response)
    assert response.status_code == 500
    assert "secret detail" not in body
    assert "Something went wrong" in body


@pytest.mark.asyncio
async def test_route_warns_when_subscription_failed(graph, monkeypatch) -> None:
    _ig_accounts(
        monkeypatch,
        [{"id": "IG1", "username": "shop", "page_id": "P1", "page_access_token": "t"}],
    )

    async def boom(page_id, page_token, fields):
        raise RuntimeError("nope")

    monkeypatch.setattr(integrations, "subscribe_page_webhooks", boom)

    response = await server.oauth_callback(
        _request(f"code=abc&state={_state('instagram')}")
    )

    assert response.status_code == 200
    assert "may not arrive" in await _body(response)


# ── Both entry points share one core ─────────────────────────────────────────


def _api_client():
    """Mount just the OAuth router so query parsing is exercised for real."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.oauth_callback import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


def test_api_server_route_delegates_to_the_same_core(graph, monkeypatch) -> None:
    """api.server and Meta MCP must not drift into two different exchanges."""
    from api import oauth_callback as api_route

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(api_route, "notify_owner_text", noop)
    _ig_accounts(
        monkeypatch,
        [{"id": "IG1", "username": "shop", "page_id": "P1", "page_access_token": "t"}],
    )

    response = _api_client().get(f"/api/oauth/callback?code=abc&state={_state('instagram')}")

    assert response.status_code == 302
    assert "integration=instagram" in response.headers["location"]
    assert graph["store_ig"], "the redirect route must run the shared exchange"
    assert graph["subscribe"], "and must subscribe webhooks like the HTML route"


def test_api_server_route_redirects_forged_state_to_error(graph) -> None:
    body, sig = _state().rsplit(".", 1)
    response = _api_client().get(f"/api/oauth/callback?code=abc&state={body}.{'0' * len(sig)}")

    assert response.status_code == 302
    assert "error=" in response.headers["location"]
    assert graph["exchange"] == []


def test_api_server_route_rejects_missing_code(graph) -> None:
    response = _api_client().get("/api/oauth/callback")
    assert response.status_code == 400
    assert graph["exchange"] == []
