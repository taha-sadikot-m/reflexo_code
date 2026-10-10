"""Stage T3: allowlist-based Temporal shadow activation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _workflow() -> dict:
    return {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "name": "Discount",
        "trigger_type": "whatsapp_message",
        "trigger_config": {},
        "conditions": [],
        "steps": [
            {
                "name": "send",
                "type": "call_tool",
                "tool": "send_whatsapp_message",
                "params": {"to": "{{external_sender_id}}", "text": "10% off"},
            }
        ],
        "instance_key": "{{customer.id}}",
        "max_duration_seconds": 3600,
        "shadow_mode": True,
        "shadow_allowlist": ["allowed-sender"],
        "total_runs": 0,
    }


def _event(sender: str) -> dict:
    return {
        "type": "whatsapp_message",
        "trigger_type": "whatsapp_message",
        "channel": "whatsapp",
        "external_sender_id": sender,
        "customer_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "customer": {"id": "cccccccc-cccc-cccc-cccc-cccccccccccc"},
        "message": {"text": "#123456"},
    }


@pytest.mark.asyncio
async def test_non_allowlisted_match_is_logged_without_temporal_or_consumption() -> None:
    from workflow_builder.deployer import (
        route_event_to_workflows,
        route_event_to_workflows_detailed,
    )

    with (
        patch(
            "workflow_builder.deployer.matching_active",
            new=AsyncMock(return_value=[_workflow()]),
        ),
        patch(
            "workflow_builder.deployer.insert_run",
            new=AsyncMock(return_value={"id": "shadow-run"}),
        ) as insert,
        patch(
            "workflow_builder.deployer.finish_run",
            new=AsyncMock(),
        ) as finish,
        patch("workflow_builder.deployer._client", new=AsyncMock()) as client,
    ):
        details = await route_event_to_workflows_detailed(
            _event("ordinary-sender"),
            BUSINESS,
        )
        consumed = await route_event_to_workflows(
            _event("ordinary-sender"),
            BUSINESS,
        )
    assert details[0]["action"] == "shadow_logged"
    assert consumed is False
    assert insert.await_args.kwargs["is_shadow"] is True
    assert insert.await_args.kwargs["status"] == "success"
    assert insert.await_args.kwargs["count_metrics"] is False
    finish.assert_awaited()
    assert finish.await_args.kwargs["count_metrics"] is False
    client.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlisted_sender_starts_real_temporal_workflow() -> None:
    from workflow_builder.deployer import route_event_to_workflows

    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RuntimeError("not found"))
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    client.start_workflow = AsyncMock()
    with (
        patch(
            "workflow_builder.deployer.matching_active",
            new=AsyncMock(return_value=[_workflow()]),
        ),
        patch(
            "workflow_builder.deployer.insert_run",
            new=AsyncMock(return_value={"id": "live-run"}),
        ),
        patch(
            "workflow_builder.deployer._client",
            new=AsyncMock(return_value=client),
        ),
    ):
        consumed = await route_event_to_workflows(
            _event("allowed-sender"),
            BUSINESS,
        )
    assert consumed is True
    client.start_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_activation_persists_shadow_configuration_and_notifies() -> None:
    from workflow_builder.activation import activate_tested_workflow

    workflow = {
        "id": _workflow()["id"],
        "name": "Discount",
        "is_tested": True,
        "metadata": {"test_results": {"all_passed": True}},
    }
    activated = {
        **workflow,
        "is_active": True,
        "shadow_mode": True,
        "shadow_allowlist": ["allowed-sender"],
    }
    with (
        patch(
            "workflow_builder.activation.get_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "workflow_builder.activation.set_active",
            new=AsyncMock(return_value=activated),
        ) as set_active,
        patch(
            "workflow_builder.activation._notify_owner_activation",
            new=AsyncMock(),
        ) as notify,
    ):
        result = await activate_tested_workflow(
            BUSINESS,
            str(workflow["id"]),
            shadow_mode=True,
            shadow_allowlist=["allowed-sender"],
        )
    assert result["shadow_mode"] is True
    set_active.assert_awaited_once_with(
        BUSINESS,
        workflow["id"],
        True,
        shadow_mode=True,
        shadow_allowlist=["allowed-sender"],
    )
    notify.assert_awaited_once_with(
        BUSINESS,
        "Discount",
        shadow_mode=True,
    )


@pytest.mark.asyncio
async def test_live_health_and_cap_queries_exclude_shadow_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.store import runs_last_24h

    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(
        "workflow_builder.store.tenant_connection",
        connection_context,
    )
    await runs_last_24h(BUSINESS, str(_workflow()["id"]))
    assert "is_shadow = false" in connection.fetch.await_args.args[0]
