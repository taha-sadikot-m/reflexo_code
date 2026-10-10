"""Admin Test Simulator lab: skip_send, scenarios, Temporal fire, auth."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from admin.routes.test_simulator import router as lab_router
from admin.test_simulator_lab import (
    build_scenario_events,
    process_webhook_safe,
    run_e2e,
)
from shared.simulator_inbound import build_simulator_inbound_event

BIZ = "00000000-0000-0000-0000-000000000001"


def _user() -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email="admin@reflexo.ai",
        name="Admin",
        role="admin",
        is_active=True,
    )


def _app(*, auth: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(lab_router)
    if auth:
        app.dependency_overrides[get_current_admin] = lambda: _user()
    return app


def test_unauthenticated_401() -> None:
    client = TestClient(_app(auth=False), raise_server_exceptions=False)
    res = client.post(
        "/api/admin/test-simulator/message",
        json={"business_id": BIZ, "text": "hi"},
    )
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_builder_and_webhook_presets_skip_send() -> None:
    event = build_simulator_inbound_event(
        business_id=BIZ, text="hi", channel="whatsapp", session_id="s1"
    )
    assert event["skip_send"] is True

    async def _publish(_ch: str, ev: dict) -> None:
        return None

    async def _resolve(*_a, **_k) -> str:
        return BIZ

    with (
        patch("mcp_servers.meta_mcp.webhooks.whatsapp.publish_event", new=_publish),
        patch(
            "mcp_servers.meta_mcp.webhooks.whatsapp.resolve_business_from_channel",
            new=_resolve,
        ),
        patch("mcp_servers.meta_mcp.webhooks.instagram.publish_event", new=_publish),
        patch(
            "mcp_servers.meta_mcp.webhooks.instagram.resolve_business_from_channel",
            new=_resolve,
        ),
        patch("mcp_servers.meta_mcp.webhooks.facebook.publish_event", new=_publish),
        patch(
            "mcp_servers.meta_mcp.webhooks.facebook.resolve_business_from_channel",
            new=_resolve,
        ),
    ):
        result = await process_webhook_safe(
            business_id=BIZ,
            payload={
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "WABA_1",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": "PNID_1"},
                                    "messages": [
                                        {
                                            "from": "919876543210",
                                            "id": "wamid.abc",
                                            "timestamp": "1721300000",
                                            "type": "text",
                                            "text": {"body": "Hi"},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
            validate_only=True,
        )
    assert result["skip_send"] is True
    assert result["queued_count"] >= 1
    assert all(q["skip_send"] is True for q in result["queued"])
    assert all(e.get("skip_send") is True for e in result["events"])


def test_scenario_giveaway_event_counts() -> None:
    events = build_scenario_events(
        business_id=BIZ,
        preset_id="comment_giveaway",
        user_count=3,
        edge={
            "duplicate_commenter": True,
            "over_limit": False,
            "non_matching": True,
        },
    )
    assert len(events) == 5
    assert all(e["skip_send"] is True for e in events)
    assert all(e["channel"] == "instagram_comment" for e in events)
    texts = [(e.get("content") or {}).get("text") for e in events]
    assert texts.count("buy") == 4
    assert texts.count("love this!") == 1
    senders = [e["sender_id"] for e in events]
    assert senders[0] == senders[3]


def test_mention_preset_maps_to_instagram_comment() -> None:
    events = build_scenario_events(
        business_id=BIZ, preset_id="mention_auto_reply", user_count=2
    )
    assert all(e["channel"] == "instagram_comment" for e in events)
    assert all("@business" in (e.get("content") or {}).get("text", "") for e in events)
    assert all(e["skip_send"] is True for e in events)


def test_workflow_fire_400_without_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.test_simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("workflow_builder.store.list_workflows", _empty)
    client = TestClient(_app())
    res = client.post(
        "/api/admin/test-simulator/workflow/fire",
        json={"business_id": BIZ},
    )
    assert res.status_code == 400


def test_workflow_fire_200_mocked_temporal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.test_simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )

    async def _one(*_a, **_k):
        return [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "Welcome",
                "trigger_type": "whatsapp_message",
                "is_active": True,
            }
        ]

    async def _route(*_a, **_k):
        return [
            {
                "workflow_id": "22222222-2222-2222-2222-222222222222",
                "temporal_instance_id": "reflexo:test:one",
                "action": "started",
            }
        ]

    async def _state(*_a, **_k):
        return {"status": "success", "events_received": 1}

    async def _terminate(*_a, **_k):
        return None

    async def _msg(**_k):
        return {
            "skip_send": True,
            "workflow_matched": False,
            "trace": {"steps": []},
            "visual": {},
            "error": None,
        }

    monkeypatch.setattr("workflow_builder.store.list_workflows", _one)
    monkeypatch.setattr(
        "workflow_builder.deployer.route_event_to_workflows_detailed",
        _route,
    )
    monkeypatch.setattr("workflow_builder.deployer.query_workflow_state", _state)
    monkeypatch.setattr(
        "workflow_builder.deployer.terminate_test_workflow",
        _terminate,
    )
    monkeypatch.setattr("admin.test_simulator_lab.run_synthetic_message", _msg)
    client = TestClient(_app())
    res = client.post(
        "/api/admin/test-simulator/workflow/fire",
        json={"business_id": BIZ},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["skip_send"] is True
    assert body["matched"] is True
    assert body["temporal_error"] is None


@pytest.mark.asyncio
async def test_e2e_does_not_call_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    graph_calls: list[str] = []

    async def _msg(**kwargs):
        if kwargs.get("skip_send") is False:
            graph_calls.append("send")
        return {
            "skip_send": True,
            "error": None,
            "workflow_matched": False,
            "specialist": "social_agent",
            "route_method": "rule",
        }

    async def _fire(**_k):
        return {"skip_send": True, "matched": True, "temporal_error": None}

    async def _bstart(**_k):
        return {"skip_send": True, "whatsapp_sent": False, "call_id": "vs-x"}

    async def _brecv(**_k):
        return {"skip_send": True, "intercepted": True, "meta_sent": False}

    class _Redis:
        def __init__(self) -> None:
            self.rows: list[str] = []

        async def lpush(self, _k: str, v: str) -> None:
            self.rows.insert(0, v)

        async def ltrim(self, *_a) -> None:
            return None

        async def expire(self, *_a) -> None:
            return None

        async def lrange(self, *_a) -> list[str]:
            return list(self.rows)

    fake = _Redis()
    monkeypatch.setattr("admin.test_simulator_lab.run_synthetic_message", _msg)
    monkeypatch.setattr("admin.test_simulator_lab.fire_workflow", _fire)
    monkeypatch.setattr("admin.test_simulator_lab.bridge_start", _bstart)
    monkeypatch.setattr("admin.test_simulator_lab.bridge_receive", _brecv)
    monkeypatch.setattr("shared.redis_client.get_redis", lambda: fake)

    out = await run_e2e(business_id=BIZ, process=False)
    assert out["skip_send"] is True
    assert graph_calls == []
    assert all(r.get("skip_send") is True for r in out["results"])
    assert out["passed"] >= 8


def test_scenario_route_skip_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.test_simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )
    client = TestClient(_app())
    res = client.post(
        "/api/admin/test-simulator/scenario",
        json={
            "business_id": BIZ,
            "preset": "comment_giveaway",
            "user_count": 3,
            "duplicate_commenter": True,
            "over_limit": False,
            "non_matching": True,
            "process": False,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_events"] == 5
    assert body["skip_send"] is True
    assert all(r["skip_send"] is True for r in body["results"])


def test_other_routes_require_auth() -> None:
    client = TestClient(_app(auth=False), raise_server_exceptions=False)
    for path in (
        "/api/admin/test-simulator/webhook",
        "/api/admin/test-simulator/e2e",
        "/api/admin/test-simulator/scenario",
    ):
        res = client.post(path, json={"business_id": BIZ, "preset": "comment_giveaway"})
        assert res.status_code in (401, 403), path
