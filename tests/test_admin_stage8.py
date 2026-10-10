"""Stage 8 admin JWT/RBAC, tenant isolation, conversation metadata, widget channel."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import (
    AdminUser,
    create_token,
    get_current_admin,
    require_min_role,
    require_role,
)
from admin.routes import ADMIN_ROUTERS, widget_router
from admin.routes.conversations import _enrich_meta
from identity.models import Channel


BIZ_A = "00000000-0000-0000-0000-000000000001"
BIZ_B = "00000000-0000-0000-0000-000000000002"


def _user(role: str) -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email=f"{role}@reflexo.ai",
        name=role.title(),
        role=role,
        is_active=True,
    )


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.businesses = {BIZ_A: {"id": BIZ_A, "name": "FOREEQUALS", "plan": "pro"}}
        self.knowledge: list[dict[str, Any]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        q = " ".join(sql.split()).lower()
        if "from businesses where id" in q:
            row = self.businesses.get(str(args[0]))
            return dict(row) if row else None
        if "from conversations" in q and "where c.id" in q:
            if str(args[1]) != BIZ_A:
                return None
            return {
                "id": str(args[0]),
                "business_id": BIZ_A,
                "channel": "web_widget",
                "customer_name": "Visitor",
            }
        if "from admin_users" in q:
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "email": "taha@reflexo.ai",
                "name": "Taha",
                "role": "super_admin",
                "is_active": True,
                "password_hash": "x",
            }
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        q = " ".join(sql.split()).lower()
        if "count(*)" in q and "knowledge_entries" in q:
            return len(self.knowledge)
        if "count(*)" in q:
            return 0
        return 0

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        q = " ".join(sql.split()).lower()
        if "from messages" in q and "conversation_id" in q:
            if str(args[1]) != BIZ_A:
                return []
            return [
                {
                    "id": "m1",
                    "role": "ai",
                    "content": "Hello from FAQ",
                    "content_type": "text",
                    "media_url": None,
                    "channel": "web_widget",
                    "ai_metadata": {
                        "specialist": "faq",
                        "route_method": "keyword",
                        "route_confidence": 0.91,
                        "handoff_chain": ["router", "faq"],
                        "guardrails": [],
                        "memories_injected": 2,
                        "cache_hit": False,
                        "latency_ms": 120,
                        "tokens": 40,
                        "estimated_cost_usd": 0.001,
                    },
                    "status": "sent",
                    "error_message": None,
                    "created_at": None,
                }
            ]
        if "from conversations c" in q:
            return []
        if "from knowledge_entries" in q:
            return list(self.knowledge)
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "UPDATE 1"

    def acquire(self):  # pragma: no cover - knowledge search-test
        raise RuntimeError("not used in these tests")


def _app(role: str, pool: FakePool | None = None) -> FastAPI:
    app = FastAPI()
    for r in ADMIN_ROUTERS:
        app.include_router(r)
    app.include_router(widget_router)
    user = _user(role)
    pool = pool or FakePool()

    async def _admin():
        return user

    app.dependency_overrides[get_current_admin] = _admin
    app.dependency_overrides[require_min_role("admin")] = _admin
    app.dependency_overrides[require_role("super_admin")] = _admin

    # Re-bind AdminPlus / SuperAdmin: override the inner callables used by Depends
    from admin import auth as auth_mod

    app.dependency_overrides[auth_mod.require_min_role("admin")] = _admin
    app.dependency_overrides[auth_mod.require_role("super_admin")] = _admin

    async def _pool():
        return pool

    with_patch = pool
    app.state.pool = with_patch
    return app, pool, user


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


def test_t8_2_enrich_meta_exposes_specialist_and_route():
    meta = _enrich_meta(
        {
            "specialist": "faq",
            "route_method": "semantic",
            "route_confidence": 0.77,
            "handoff_chain": ["router"],
            "latency_ms": 90,
        }
    )
    assert meta["specialist"] == "faq"
    assert meta["route_method"] == "semantic"
    assert meta["route_confidence"] == 0.77
    assert meta["latency_ms"] == 90


def test_t8_2_enrich_meta_accepts_asyncpg_json_string():
    meta = _enrich_meta(
        '{"specialist":"sales","route_method":"broker","confidence":0.84}'
    )
    assert meta["specialist"] == "sales"
    assert meta["route_method"] == "broker"
    assert meta["route_confidence"] == 0.84


def test_t8_2_enrich_meta_drops_non_finite_metrics():
    meta = _enrich_meta('{"confidence":"NaN","latency_ms":"Infinity"}')
    assert meta["route_confidence"] is None
    assert meta["latency_ms"] is None


def test_web_widget_channel_enum():
    assert Channel.WEB_WIDGET.value == "web_widget"


def test_jwt_roundtrip(monkeypatch):
    monkeypatch.setattr("admin.auth._secret", lambda: "unit-test-secret-not-for-prod-ok32")
    token = create_token(_user("admin"))
    payload = jwt.decode(token, "unit-test-secret-not-for-prod-ok32", algorithms=["HS256"])
    assert payload["role"] == "admin"
    assert payload["email"] == "admin@reflexo.ai"


def test_t8_1_conversation_list_requires_auth():
    app = FastAPI()
    for r in ADMIN_ROUTERS:
        app.include_router(r)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get(f"/api/admin/conversations/{BIZ_A}")
    assert res.status_code == 401


def test_t8_1_t8_2_conversation_thread_enriched(monkeypatch, pool: FakePool):
    app, fake, user = _app("support", pool)
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr("admin.routes.conversations.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr("admin.routes.conversations.require_business", AsyncMock(return_value={"id": BIZ_A}))
    client = TestClient(app)
    client.app.dependency_overrides[get_current_admin] = lambda: user
    res = client.get(f"/api/admin/conversations/{BIZ_A}/cid-1/messages")
    assert res.status_code == 200
    body = res.json()
    assert body["messages"][0]["ai_metadata"]["specialist"] == "faq"
    assert body["messages"][0]["ai_metadata"]["route_method"] == "keyword"
    assert body["messages"][0]["ai_metadata"]["route_confidence"] == 0.91


def test_tenant_isolation_conversation_404(monkeypatch, pool: FakePool):
    app, fake, user = _app("admin", pool)
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr("admin.routes.conversations.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr(
        "admin.routes.conversations.require_business",
        AsyncMock(return_value={"id": BIZ_B}),
    )
    client = TestClient(app)
    client.app.dependency_overrides[get_current_admin] = lambda: user
    res = client.get(f"/api/admin/conversations/{BIZ_B}/cid-1/messages")
    assert res.status_code == 404


def test_support_cannot_mutate_knowledge(monkeypatch, pool: FakePool):
    app, fake, user = _app("support", pool)
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr("admin.routes.knowledge.get_admin_pool", AsyncMock(return_value=fake))
    monkeypatch.setattr("admin.routes.knowledge.require_business", AsyncMock(return_value={"id": BIZ_A}))
    client = TestClient(app)
    client.app.dependency_overrides[get_current_admin] = lambda: user
    # Support is not AdminPlus — FastAPI still runs require_min_role("admin")
    from admin.auth import require_min_role as rmr

    async def _forbid():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Rebuild a clean app with only support override
    app2 = FastAPI()
    from admin.routes.knowledge import router as kr

    app2.include_router(kr)
    app2.dependency_overrides[get_current_admin] = lambda: user
    client = TestClient(app2, raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/knowledge/{BIZ_A}",
        json={"answer": "We ship in 3 days", "question": "shipping"},
    )
    assert res.status_code == 403


def test_admin_cannot_change_plan(monkeypatch, pool: FakePool):
    app2 = FastAPI()
    from admin.routes.billing import router as br

    app2.include_router(br)
    app2.dependency_overrides[get_current_admin] = lambda: _user("admin")
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.billing.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.billing.require_business", AsyncMock(return_value={"id": BIZ_A}))
    client = TestClient(app2, raise_server_exceptions=False)
    res = client.post(f"/api/admin/billing/change-plan/{BIZ_A}", json={"plan": "enterprise"})
    assert res.status_code == 403


def test_support_cannot_reset_billing(monkeypatch, pool: FakePool):
    app2 = FastAPI()
    from admin.routes.billing import router as br

    app2.include_router(br)
    app2.dependency_overrides[get_current_admin] = lambda: _user("support")
    client = TestClient(app2, raise_server_exceptions=False)
    res = client.post(f"/api/admin/billing/reset-usage/{BIZ_A}")
    assert res.status_code == 403


def test_super_admin_can_reset_billing(monkeypatch, pool: FakePool):
    app2 = FastAPI()
    from admin.routes.billing import router as br

    app2.include_router(br)
    user = _user("super_admin")
    app2.dependency_overrides[get_current_admin] = lambda: user
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.billing.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.billing.require_business", AsyncMock(return_value={"id": BIZ_A}))
    client = TestClient(app2)
    res = client.post(f"/api/admin/billing/reset-usage/{BIZ_A}")
    assert res.status_code == 200
    assert res.json()["status"] == "reset"


def test_widget_send_404_unknown_business(monkeypatch, pool: FakePool):
    app = FastAPI()
    app.include_router(widget_router)
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.widget.require_business", AsyncMock(side_effect=__import__("fastapi").HTTPException(status_code=404, detail="Business not found")))
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        f"/api/widget/{BIZ_B}/send",
        json={"text": "hello", "visitor_id": "visitor-abc-1234"},
    )
    assert res.status_code == 404


def test_widget_send_queues(monkeypatch, pool: FakePool):
    app = FastAPI()
    app.include_router(widget_router)
    monkeypatch.setattr("admin.helpers.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.widget.require_business", AsyncMock(return_value={"id": BIZ_A}))
    monkeypatch.setattr("admin.routes.widget._ensure_channel", AsyncMock())
    monkeypatch.setattr("admin.routes.widget._rate_limit", AsyncMock())
    monkeypatch.setattr("admin.routes.widget.publish_event", AsyncMock())
    client = TestClient(app)
    res = client.post(
        f"/api/widget/{BIZ_A}/send",
        json={"text": "Do you ship to Pune?", "visitor_id": "visitor-abc-1234"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "queued"
    assert body["visitor_id"] == "visitor-abc-1234"
    assert body["session_id"].startswith("wg-")


def test_dashboard_sql_keys(monkeypatch, pool: FakePool):
    async def fetchrow(sql, *args):
        if "messages_today" in sql:
            return {"messages_today": 12, "unused": 0}
        if "is_active" in sql:
            return {"active": 3, "onboarding": 1}
        return {}

    pool.fetchrow = fetchrow  # type: ignore[method-assign]
    pool.fetchval = AsyncMock(side_effect=[4, 88.0, 12.5])  # yesterday, median, spend
    pool.fetch = AsyncMock(return_value=[])
    app = FastAPI()
    from admin.routes.dashboard import router as dr

    app.include_router(dr)
    app.dependency_overrides[get_current_admin] = lambda: _user("admin")
    monkeypatch.setattr("admin.routes.dashboard.get_admin_pool", AsyncMock(return_value=pool))
    client = TestClient(app)
    res = client.get("/api/admin/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert data["messages_today"] == 12
    assert data["active_businesses"] == 3
    assert data["median_reply_ms"] == 88.0


def test_progress_done_includes_response_text():
    """Contract for T8.5 / T8.7 — widget consumes done.response_text and typing."""
    import inspect
    from agent import session as session_mod

    src = inspect.getsource(session_mod.handle_incoming_message)
    assert 'response_text=response_text' in src
    assert '"typing"' in src or "'typing'" in src
