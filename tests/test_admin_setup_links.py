"""Integration setup links: hashed tokens, public isolation, OAuth redirect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from admin.setup_links import (
    API_KEY_IDS,
    assemble_catalog,
    hash_token,
    lookup_active_link,
)
from onboarding.integrations import decode_oauth_state, encode_oauth_state

BIZ_A = "00000000-0000-0000-0000-000000000001"


def _user(role: str) -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email=f"{role}@reflexo.ai",
        name=role.title(),
        role=role,
        is_active=True,
    )


class SetupPool:
    def __init__(self) -> None:
        self.links: dict[str, dict[str, Any]] = {}
        self.channels: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []
        self.businesses = {BIZ_A: {"id": BIZ_A, "name": "FOREEQUALS"}}
        self.calls: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(sql.split()).lower()
        if "insert into integration_setup_links" in q:
            link_id = str(uuid4())
            row = {
                "id": link_id,
                "expires_at": args[3],
                "created_at": datetime.now(timezone.utc),
            }
            self.links[str(args[2])] = {
                "id": link_id,
                "business_id": str(args[0]),
                "token_hash": str(args[2]),
                "expires_at": args[3],
                "revoked_at": None,
                "business_name": "FOREEQUALS",
            }
            return row
        if "from integration_setup_links l" in q:
            row = self.links.get(str(args[0]))
            if not row:
                return None
            return {
                "id": row["id"],
                "business_id": row["business_id"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "last_used_at": None,
                "business_name": row["business_name"],
            }
        if "from businesses where id" in q:
            found = self.businesses.get(str(args[0]))
            return dict(found) if found else None
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(sql.split()).lower()
        if "from business_channels" in q:
            return list(self.channels)
        if "from connected_accounts" in q:
            return list(self.accounts)
        if "from integration_setup_links" in q:
            return []
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        q = " ".join(sql.split()).lower()
        self.calls.append(q)
        if "revoked_at = now()" in q and "id =" in q:
            for row in self.links.values():
                if row["id"] == str(args[0]):
                    row["revoked_at"] = datetime.now(timezone.utc)
                    return "UPDATE 1"
            return "UPDATE 0"
        if "last_used_at" in q:
            return "UPDATE 1"
        if "revoked_at = now()" in q:
            for row in self.links.values():
                if row["business_id"] == str(args[0]) and row["revoked_at"] is None:
                    row["revoked_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        return "UPDATE 1"


def test_hash_token_is_sha256_hex():
    digest = hash_token("secret-token-value")
    assert len(digest) == 64
    assert digest != hash_token("other-token-value")


def test_assemble_catalog_public_hides_account_id():
    items = assemble_catalog(
        channels=[
            {
                "channel": "whatsapp",
                "is_active": True,
                "channel_display_name": "Onboarding WhatsApp",
            }
        ],
        account_rows=[{"integration_id": "razorpay", "connected_account_id": "secret-id"}],
        public=True,
    )
    wa = next(i for i in items if i["id"] == "whatsapp")
    rz = next(i for i in items if i["id"] == "razorpay")
    assert wa["connected"] is True
    assert rz["connected"] is True
    assert "connected_account_id" not in rz


def test_lookup_rejects_expired_and_revoked():
    async def _run() -> None:
        pool = SetupPool()
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        pool.links["dead"] = {
            "id": "x",
            "business_id": BIZ_A,
            "expires_at": expired,
            "revoked_at": None,
            "business_name": "FOREEQUALS",
        }
        # hash of raw token won't match "dead"; simulate by putting hash_token(raw)
        raw = "a" * 24
        pool.links[hash_token(raw)] = pool.links.pop("dead")
        assert await lookup_active_link(pool, raw) is None

        raw2 = "b" * 24
        pool.links[hash_token(raw2)] = {
            "id": "y",
            "business_id": BIZ_A,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            "revoked_at": datetime.now(timezone.utc),
            "business_name": "FOREEQUALS",
        }
        assert await lookup_active_link(pool, raw2) is None

        raw3 = "c" * 24
        pool.links[hash_token(raw3)] = {
            "id": "z",
            "business_id": BIZ_A,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            "revoked_at": None,
            "business_name": "FOREEQUALS",
        }
        found = await lookup_active_link(pool, raw3)
        assert found is not None
        assert found["business_id"] == BIZ_A

    import asyncio

    asyncio.run(_run())


def test_oauth_state_roundtrip_setup_token():
    state = encode_oauth_state(
        {"business_id": BIZ_A, "integration": "instagram", "setup_token": "tok_abc"}
    )
    payload = decode_oauth_state(state)
    assert payload["setup_token"] == "tok_abc"
    assert payload["integration"] == "instagram"


def test_oauth_callback_redirects_to_setup_page(monkeypatch):
    from api.oauth_callback import router

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        "api.oauth_callback.get_api_settings",
        lambda: SimpleNamespace(
            admin_frontend_origin="http://localhost:3000",
            meta_oauth_error_url="https://example.com/error",
            meta_oauth_success_url="https://example.com/success",
        ),
    )
    monkeypatch.setattr(
        "api.oauth_callback.decode_oauth_state",
        lambda _s: {"setup_token": "owner-token-123456", "integration": "instagram"},
    )
    monkeypatch.setattr(
        "api.oauth_callback.complete_meta_oauth",
        AsyncMock(
            return_value={
                "ok": True,
                "integration": "instagram",
                "business_id": BIZ_A,
                "subscription_warnings": [],
            }
        ),
    )
    client = TestClient(app, follow_redirects=False)
    res = client.get("/api/oauth/callback", params={"code": "abc", "state": "signed"})
    assert res.status_code == 302
    loc = res.headers["location"]
    assert loc.startswith("http://localhost:3000/setup/owner-token-123456")
    assert "connected=instagram" in loc


def test_public_setup_unknown_token_is_404(monkeypatch):
    from admin.routes.public_setup import router

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr("admin.routes.public_setup._rate_limit", AsyncMock())
    monkeypatch.setattr("admin.routes.public_setup.lookup_active_link", AsyncMock(return_value=None))
    monkeypatch.setattr("admin.routes.public_setup.get_admin_pool", AsyncMock())
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/api/setup/not-a-real-token-at-all")
    assert res.status_code == 404


def test_public_oauth_url_rejects_api_key_ids(monkeypatch):
    from admin.routes.public_setup import router

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr("admin.routes.public_setup._rate_limit", AsyncMock())
    monkeypatch.setattr(
        "admin.routes.public_setup.lookup_active_link",
        AsyncMock(
            return_value={
                "id": "link-1",
                "business_id": BIZ_A,
                "business_name": "FOREEQUALS",
            }
        ),
    )
    monkeypatch.setattr("admin.routes.public_setup.touch_setup_link", AsyncMock())
    monkeypatch.setattr("admin.routes.public_setup.get_admin_pool", AsyncMock())
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/api/setup/token-token-token-token/oauth-url", json={"integration_id": "razorpay"})
    assert res.status_code == 400


def test_public_and_admin_api_keys_call_connector(monkeypatch):
    from admin.routes.public_setup import router as public_router
    from admin.routes.setup_links import router as admin_router

    connect = AsyncMock(return_value={"status": "connected", "platform": "fast2sms"})
    monkeypatch.setenv("CONNECTION_HUB_SECRET", "test-secret")
    monkeypatch.setattr("channel_framework.connection_hub.connect_api_key", connect)

    pub = FastAPI()
    pub.include_router(public_router)
    monkeypatch.setattr("admin.routes.public_setup._rate_limit", AsyncMock())
    monkeypatch.setattr(
        "admin.routes.public_setup.lookup_active_link",
        AsyncMock(return_value={"id": "l", "business_id": BIZ_A, "business_name": "X"}),
    )
    monkeypatch.setattr("admin.routes.public_setup.touch_setup_link", AsyncMock())
    monkeypatch.setattr("admin.routes.public_setup.get_admin_pool", AsyncMock())
    client = TestClient(pub)
    res = client.post(
        "/api/setup/token-token-token-token/api-keys",
        json={"integration_id": "fast2sms", "api_key": "abc-secret-key"},
    )
    assert res.status_code == 200
    connect.assert_awaited()

    admin_app = FastAPI()
    admin_app.include_router(admin_router)
    user = _user("admin")
    admin_app.dependency_overrides[get_current_admin] = lambda: user
    monkeypatch.setattr("admin.routes.setup_links.require_business", AsyncMock(return_value={"id": BIZ_A}))
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    client2 = TestClient(admin_app)
    res2 = client2.post(
        f"/api/admin/integrations/{BIZ_A}/api-keys",
        json={"integration_id": "fast2sms", "api_key": "abc-secret-key"},
    )
    assert res2.status_code == 200, res2.text
    assert connect.await_count == 2


def test_support_cannot_mint_setup_link():
    from admin.routes.setup_links import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: _user("support")
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/setup-links",
        json={"ttl_hours": 24},
    )
    assert res.status_code == 403


def test_admin_can_mint_setup_link(monkeypatch):
    from admin.routes.setup_links import router

    pool = SetupPool()
    app = FastAPI()
    app.include_router(router)
    user = _user("admin")
    app.dependency_overrides[get_current_admin] = lambda: user
    monkeypatch.setattr("admin.routes.setup_links.require_business", AsyncMock(return_value={"id": BIZ_A}))
    monkeypatch.setattr("admin.routes.setup_links.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    monkeypatch.setattr(
        "admin.setup_links.get_api_settings",
        lambda: SimpleNamespace(admin_frontend_origin="http://localhost:3000", port=8030),
    )
    client = TestClient(app)
    res = client.post(
        f"/api/admin/integrations/{BIZ_A}/setup-links",
        json={"ttl_hours": 24, "revoke_previous": False},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["token"]
    assert body["url"].startswith("http://localhost:3000/setup/")
    assert hash_token(body["token"]) in pool.links


def test_revoked_token_fails_public_get(monkeypatch):
    from admin.routes.public_setup import router
    from admin.routes.setup_links import router as admin_router

    pool = SetupPool()
    raw = "z" * 32
    pool.links[hash_token(raw)] = {
        "id": "link-rev",
        "business_id": BIZ_A,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=2),
        "revoked_at": None,
        "business_name": "FOREEQUALS",
    }

    admin_app = FastAPI()
    admin_app.include_router(admin_router)
    user = _user("admin")
    admin_app.dependency_overrides[get_current_admin] = lambda: user
    monkeypatch.setattr("admin.routes.setup_links.require_business", AsyncMock(return_value={"id": BIZ_A}))
    monkeypatch.setattr("admin.routes.setup_links.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.setup_links.audit", AsyncMock())
    admin_client = TestClient(admin_app)
    assert admin_client.post(f"/api/admin/integrations/{BIZ_A}/setup-links/link-rev/revoke").status_code == 200

    pub = FastAPI()
    pub.include_router(router)
    monkeypatch.setattr("admin.routes.public_setup._rate_limit", AsyncMock())
    monkeypatch.setattr("admin.routes.public_setup.get_admin_pool", AsyncMock(return_value=pool))
    pub_client = TestClient(pub, raise_server_exceptions=False)
    res = pub_client.get(f"/api/setup/{raw}")
    assert res.status_code == 404


def test_mint_oauth_only_allows_oauth_ids():
    from admin.setup_links import OAUTH_IDS

    assert "razorpay" not in OAUTH_IDS
    assert "instagram" in OAUTH_IDS
    assert {"gmail", "outlook", "slack"} <= OAUTH_IDS
    assert "fast2sms" in API_KEY_IDS
