"""Admin/API simulator inbound event shapes for social comments."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from admin.routes.simulator import router as simulator_router
from shared.simulator_inbound import (
    build_simulator_inbound_event,
    normalize_simulator_channel,
)

BIZ = "00000000-0000-0000-0000-000000000001"


def test_normalize_legacy_instagram_alias() -> None:
    assert normalize_simulator_channel("instagram") == "instagram_dm"


def test_normalize_rejects_unknown_channel() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        normalize_simulator_channel("tiktok")


def test_whatsapp_event_unchanged_shape() -> None:
    event = build_simulator_inbound_event(
        business_id=BIZ,
        text="hi",
        channel="whatsapp",
        session_id="sim-abc",
    )
    assert event["event_type"] == "message"
    assert event["channel"] == "whatsapp"
    assert event["skip_send"] is True
    assert "comment_id" not in event
    assert event["content"]["text"] == "hi"


def test_facebook_comment_event_has_ids_and_skip_send() -> None:
    event = build_simulator_inbound_event(
        business_id=BIZ,
        text="Is this still available?",
        channel="facebook_comment",
        session_id="sim-fb",
        comment_id="c1",
        post_id="p1",
        customer_name="Tester",
    )
    assert event["event_type"] == "facebook_comment"
    assert event["channel"] == "facebook_comment"
    assert event["comment_id"] == "c1"
    assert event["post_id"] == "p1"
    assert event["skip_send"] is True
    assert event["sender_name"] == "Tester"
    assert event["content"]["text"] == "Is this still available?"


def test_instagram_comment_generates_ids() -> None:
    event = build_simulator_inbound_event(
        business_id=BIZ,
        text="Nice post",
        channel="instagram_comment",
        session_id="sim-ig",
    )
    assert event["event_type"] == "instagram_comment"
    assert event["channel"] == "instagram_comment"
    assert event["comment_id"].startswith("sim-comment-")
    assert event["post_id"].startswith("sim-post-")
    assert event["skip_send"] is True
    assert event["ig_account_id"] == BIZ


def test_messenger_and_ig_dm_event_types() -> None:
    fb = build_simulator_inbound_event(
        business_id=BIZ, text="dm", channel="facebook_messenger", session_id="s1"
    )
    ig = build_simulator_inbound_event(
        business_id=BIZ, text="dm", channel="instagram_dm", session_id="s2"
    )
    assert fb["event_type"] == "facebook_message"
    assert fb["channel"] == "facebook_messenger"
    assert ig["event_type"] == "instagram_dm"
    assert ig["skip_send"] is True


def _user() -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email="admin@reflexo.ai",
        name="Admin",
        role="admin",
        is_active=True,
    )


def test_simulator_send_requires_auth() -> None:
    app = FastAPI()
    app.include_router(simulator_router)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/simulator/{BIZ}/send",
        json={"text": "hi", "channel": "facebook_comment"},
    )
    assert res.status_code in (401, 403)


def test_admin_simulator_queues_facebook_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, dict]] = []

    async def _publish(channel: str, event: dict) -> None:
        published.append((channel, event))

    monkeypatch.setattr("admin.routes.simulator.publish_event", _publish)
    monkeypatch.setattr(
        "admin.routes.simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )

    app = FastAPI()
    app.include_router(simulator_router)
    app.dependency_overrides[get_current_admin] = lambda: _user()
    client = TestClient(app)
    res = client.post(
        f"/api/admin/simulator/{BIZ}/send",
        json={
            "text": "Price?",
            "channel": "facebook_comment",
            "mode": "customer",
            "session_id": "sim-sess",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["event_type"] == "facebook_comment"
    assert body["skip_send"] is True
    assert body["comment_id"]
    assert len(published) == 1
    _ch, event = published[0]
    assert event["event_type"] == "facebook_comment"
    assert event["comment_id"]
    assert event["skip_send"] is True


def test_admin_simulator_queues_instagram_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[dict] = []

    async def _publish(_channel: str, event: dict) -> None:
        published.append(event)

    monkeypatch.setattr("admin.routes.simulator.publish_event", _publish)
    monkeypatch.setattr(
        "admin.routes.simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )

    app = FastAPI()
    app.include_router(simulator_router)
    app.dependency_overrides[get_current_admin] = lambda: _user()
    client = TestClient(app)
    res = client.post(
        f"/api/admin/simulator/{BIZ}/send",
        json={"text": "Love this", "channel": "instagram_comment", "mode": "customer"},
    )
    assert res.status_code == 200
    assert res.json()["event_type"] == "instagram_comment"
    assert published[0]["channel"] == "instagram_comment"
    assert published[0]["skip_send"] is True


def test_admin_simulator_whatsapp_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[dict] = []

    async def _publish(_channel: str, event: dict) -> None:
        published.append(event)

    monkeypatch.setattr("admin.routes.simulator.publish_event", _publish)
    monkeypatch.setattr(
        "admin.routes.simulator.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )

    app = FastAPI()
    app.include_router(simulator_router)
    app.dependency_overrides[get_current_admin] = lambda: _user()
    client = TestClient(app)
    res = client.post(
        f"/api/admin/simulator/{BIZ}/send",
        json={"text": "hello", "channel": "whatsapp", "mode": "customer"},
    )
    assert res.status_code == 200
    assert published[0]["event_type"] == "message"
    assert published[0]["channel"] == "whatsapp"
    assert "comment_id" not in published[0]
