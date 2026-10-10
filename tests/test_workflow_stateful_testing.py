"""Stateful Temporal workflow test selection, reporting, and activation gates."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from workflow_builder.owner_test import format_for_owner
from workflow_builder.studio import generate_workflow_visual
from workflow_builder.testing import (
    auto_select_scenario,
    generate_scenario_events,
    test_workflow_with_scenario as run_stateful_scenario,
)

BIZ = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WF = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _aggregate_workflow() -> dict:
    return {
        "id": WF,
        "name": "Three comments coupon",
        "trigger_type": "instagram_comment",
        "trigger_config": {"keyword": "buy"},
        "instance_key": "{{customer.id}}",
        "steps": [
            {
                "name": "wait_for_comments",
                "type": "aggregate_events",
                "threshold": 3,
                "event_filter": {"type": "instagram_comment"},
            },
            {
                "name": "send_coupon",
                "type": "call_tool",
                "tool": "send_whatsapp_message",
                "depends_on": ["wait_for_comments"],
            },
        ],
    }


def test_auto_selects_stateful_comment_scenario() -> None:
    scenario = auto_select_scenario(_aggregate_workflow())
    assert scenario["type"] == "giveaway"
    assert scenario["events"] == 3
    assert scenario["same_actor"] is True
    assert "duplicate" in scenario["edge_cases"]


def test_generated_events_share_customer_instance_and_never_send() -> None:
    definition = _aggregate_workflow()
    scenario = auto_select_scenario(definition)
    events = generate_scenario_events(BIZ, definition, scenario)
    base = events[:3]
    assert len({event["sender_id"] for event in base}) == 1
    assert all(event["skip_send"] is True for event in events)
    assert all((event["content"] or {})["text"] == "buy" for event in base)


@pytest.mark.asyncio
async def test_stateful_runner_snapshots_temporal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _aggregate_workflow()
    monkeypatch.setattr(
        "workflow_builder.testing.get_workflow",
        AsyncMock(return_value=workflow),
    )
    monkeypatch.setattr(
        "admin.test_simulator_lab.run_synthetic_message",
        AsyncMock(
            return_value={
                "error": None,
                "trace": {"steps": []},
                "visual": {"nodes": [], "edges": []},
                "skip_send": True,
            }
        ),
    )
    route = AsyncMock(
        side_effect=[
            [
                {
                    "workflow_id": WF,
                    "temporal_instance_id": "wf:test:x:user",
                    "action": "started",
                    "run_id": "run-1",
                }
            ],
            [
                {
                    "workflow_id": WF,
                    "temporal_instance_id": "wf:test:x:user",
                    "action": "signaled",
                }
            ],
            [
                {
                    "workflow_id": WF,
                    "temporal_instance_id": "wf:test:x:user",
                    "action": "signaled",
                }
            ],
            [
                {
                    "workflow_id": WF,
                    "temporal_instance_id": "wf:test:x:user",
                    "action": "signaled",
                }
            ],
            [
                {
                    "workflow_id": WF,
                    "temporal_instance_id": "wf:test:x:over",
                    "action": "started",
                    "run_id": "run-2",
                }
            ],
            [],
        ]
    )
    monkeypatch.setattr(
        "workflow_builder.testing.route_event_to_workflows_detailed",
        route,
    )
    states = [
        {"status": "running", "events_received": 1, "completed_steps": []},
        {"status": "running", "events_received": 2, "completed_steps": []},
        {
            "status": "success",
            "events_received": 3,
            "completed_steps": ["wait_for_comments", "send_coupon"],
            "step_results": {"send_coupon": {"status": "simulated"}},
        },
        {
            "status": "success",
            "events_received": 4,
            "completed_steps": ["wait_for_comments", "send_coupon"],
        },
        {"status": "running", "events_received": 1, "completed_steps": []},
    ]
    monkeypatch.setattr(
        "workflow_builder.testing._query_with_retry",
        AsyncMock(side_effect=states),
    )
    monkeypatch.setattr(
        "workflow_builder.testing.terminate_test_workflow",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "workflow_builder.testing.finish_run",
        AsyncMock(),
    )

    result = await run_stateful_scenario(
        business_id=BIZ,
        workflow_id=WF,
        run_agent_pipeline=False,
    )

    assert result["all_passed"] is True
    assert result["workflow_triggered"] == 5
    assert result["state_progression"][2]["events_received"] == 3
    assert all(item["skip_send"] is True for item in result["per_event_traces"])
    route.assert_awaited()


@pytest.mark.asyncio
async def test_pipeline_initializes_store_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API process runs the pipeline without the worker's init_store()."""
    from agent import infrastructure

    monkeypatch.setattr(infrastructure, "_store", None)
    sentinel = object()
    init = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(infrastructure, "init_store", init)

    assert await infrastructure.ensure_store() is sentinel
    init.assert_awaited_once()

    monkeypatch.setattr(infrastructure, "_store", sentinel)
    init.reset_mock()
    assert await infrastructure.ensure_store() is sentinel
    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_reports_progress_for_every_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workflow_builder.testing.get_workflow",
        AsyncMock(return_value=_aggregate_workflow()),
    )
    monkeypatch.setattr(
        "admin.test_simulator_lab.run_synthetic_message",
        AsyncMock(return_value={"error": None, "trace": {}, "visual": {}}),
    )
    monkeypatch.setattr(
        "workflow_builder.testing.route_event_to_workflows_detailed",
        AsyncMock(
            return_value=[
                {"workflow_id": WF, "temporal_instance_id": "wf:test:x", "action": "started"}
            ]
        ),
    )
    monkeypatch.setattr(
        "workflow_builder.testing._query_with_retry",
        AsyncMock(return_value={"status": "success", "events_received": 1}),
    )
    monkeypatch.setattr("workflow_builder.testing.terminate_test_workflow", AsyncMock())
    monkeypatch.setattr("workflow_builder.testing.finish_run", AsyncMock())

    updates: list[dict] = []

    async def progress(update: dict) -> None:
        updates.append(update)

    result = await run_stateful_scenario(
        business_id=BIZ,
        workflow_id=WF,
        run_agent_pipeline=False,
        progress=progress,
    )

    total = result["events_processed"]
    assert updates[0]["events_total"] == total
    per_event = [u for u in updates if u.get("last_event")]
    assert len(per_event) == total
    assert [u["events_processed"] for u in per_event] == list(range(1, total + 1))
    assert updates[-1]["stage"] == "cleanup"


@pytest.mark.asyncio
async def test_progress_errors_never_break_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workflow_builder.testing.get_workflow",
        AsyncMock(return_value=_aggregate_workflow()),
    )
    monkeypatch.setattr(
        "admin.test_simulator_lab.run_synthetic_message",
        AsyncMock(return_value={"error": None}),
    )
    monkeypatch.setattr(
        "workflow_builder.testing.route_event_to_workflows_detailed",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("workflow_builder.testing.terminate_test_workflow", AsyncMock())
    monkeypatch.setattr("workflow_builder.testing.finish_run", AsyncMock())

    async def broken(_update: dict) -> None:
        raise RuntimeError("progress sink down")

    result = await run_stateful_scenario(
        business_id=BIZ,
        workflow_id=WF,
        run_agent_pipeline=False,
        progress=broken,
    )
    assert result["events_processed"] > 0


def test_owner_report_is_plain_language_and_gates_on_failures() -> None:
    report = format_for_owner(
        {
            "all_passed": False,
            "events_processed": 2,
            "workflow_triggered": 1,
            "scenario": {"type": "generic", "edge_cases": ["duplicate"]},
            "per_event_traces": [
                {
                    "event_index": 1,
                    "workflow_matched": True,
                    "state": {"status": "running", "events_received": 1},
                    "outcome": "running",
                },
                {
                    "event_index": 2,
                    "workflow_matched": False,
                    "state": None,
                    "outcome": "error",
                    "error": "Temporal unavailable",
                },
            ],
            "failures": [
                {
                    "event_index": 2,
                    "outcome": "error",
                    "error": "Temporal unavailable",
                }
            ],
        }
    )
    assert report["all_passed"] is False
    assert report["status"] == "failed"
    assert "Activation remains blocked" in report["summary"][-1]


def test_studio_visual_contains_trigger_and_dependencies() -> None:
    visual = generate_workflow_visual(_aggregate_workflow())
    assert visual["nodes"][0]["id"] == "trigger"
    assert len(visual["nodes"]) == 3
    assert any(
        edge["source"] == "step-0" and edge["target"] == "step-1"
        for edge in visual["edges"]
    )
