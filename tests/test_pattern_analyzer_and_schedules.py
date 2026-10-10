"""Stage T4: pattern suggestions and Temporal schedules."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from workflow_builder.models import StepType, WorkflowDefinition, WorkflowStep

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _suggestion_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Answer delivery questions",
        trigger_type="whatsapp_message",
        steps=[
            WorkflowStep(
                name="reply",
                type=StepType.CALL_TOOL,
                tool="send_whatsapp_message",
                params={"to": "{{external_sender_id}}", "text": "Delivery is 2 days"},
            )
        ],
    )


@pytest.mark.asyncio
async def test_repeated_pattern_creates_inactive_temporal_draft() -> None:
    from workflow_builder.pattern_analyzer import analyze_business_patterns

    definition = _suggestion_definition()
    with (
        patch(
            "workflow_builder.pattern_analyzer.find_repeated_patterns",
            new=AsyncMock(
                return_value=[
                    {
                        "channel": "whatsapp",
                        "normalized_text": "when will my order arrive",
                        "occurrence_count": 4,
                    }
                ]
            ),
        ),
        patch(
            "workflow_builder.pattern_analyzer.pattern_suggestion_exists",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "workflow_builder.pattern_analyzer.decompose_workflow",
            new=AsyncMock(return_value=definition),
        ),
        patch(
            "workflow_builder.pattern_analyzer.deploy_workflow",
            new=AsyncMock(
                return_value={
                    "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "name": definition.name,
                    "is_active": False,
                }
            ),
        ) as deploy,
        patch(
            "workflow_builder.pattern_analyzer.mark_pattern_suggestion",
            new=AsyncMock(),
        ) as mark,
        patch(
            "workflow_builder.pattern_analyzer._notify_owner",
            new=AsyncMock(),
        ),
    ):
        suggestions = await analyze_business_patterns(BUSINESS)
    assert len(suggestions) == 1
    assert suggestions[0]["is_active"] is False
    persisted_definition = deploy.await_args.args[1]
    assert persisted_definition.trigger_type == "whatsapp_message"
    assert persisted_definition.conditions[0]["operator"] == "contains"
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_below_pattern_threshold_creates_nothing() -> None:
    from workflow_builder.pattern_analyzer import analyze_business_patterns

    with (
        patch(
            "workflow_builder.pattern_analyzer.find_repeated_patterns",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "workflow_builder.pattern_analyzer.deploy_workflow",
            new=AsyncMock(),
        ) as deploy,
    ):
        suggestions = await analyze_business_patterns(
            BUSINESS,
            min_occurrences=3,
        )
    assert suggestions == []
    deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_temporal_schedule_fires_dynamic_workflow() -> None:
    from workflow_builder.deployer import (
        ensure_temporal_schedule,
    )
    from workflow_builder.interpreter import DynamicBusinessWorkflow

    fired = asyncio.Event()

    @activity.defn(name="execute_tool_activity")
    async def execute_tool(*_args) -> dict:
        fired.set()
        return {"status": "sent"}

    @activity.defn(name="record_workflow_step")
    async def record_step(*_args) -> None:
        return None

    @activity.defn(name="send_notification_activity")
    async def send_notification(*_args) -> dict:
        return {"status": "sent"}

    @activity.defn(name="request_workflow_confirmation_activity")
    async def request_confirmation(*_args) -> dict:
        return {"status": "pending"}

    @activity.defn(name="clear_workflow_confirmation_activity")
    async def clear_confirmation(*_args) -> None:
        return None

    @activity.defn(name="start_scheduled_run_activity")
    async def start_scheduled_run(*_args) -> str:
        return "scheduled-run"

    @activity.defn(name="finish_workflow_run_activity")
    async def finish_workflow_run(*_args) -> None:
        return None

    queue = f"t4-{uuid.uuid4()}"
    workflow = {
        "id": str(uuid.uuid4()),
        "name": "Scheduled reminder",
        "description": "Run on schedule",
        "trigger_type": "schedule",
        "trigger_config": {"interval_seconds": 1},
        "steps": [
            {
                "name": "notify",
                "type": "call_tool",
                "tool": "send_whatsapp_message",
                "params": {"to": "+919000000001", "text": "Reminder"},
            }
        ],
        "max_duration_seconds": 30,
    }
    schedule_client = MagicMock()
    schedule_handle = MagicMock()
    schedule_handle.delete = AsyncMock()
    schedule_client.get_schedule_handle.return_value = schedule_handle
    schedule_client.create_schedule = AsyncMock()
    with (
        patch(
            "workflow_builder.deployer._client",
            new=AsyncMock(return_value=schedule_client),
        ),
        patch(
            "workflow_builder.deployer._settings",
            return_value=("test", "default", queue),
        ),
    ):
        await ensure_temporal_schedule(BUSINESS, workflow)
    schedule_client.create_schedule.assert_awaited_once()

    # The lightweight Temporal test server does not implement CreateSchedule.
    # Execute the exact action payload captured above to prove a scheduled start
    # creates a run and reaches the dynamic worker.
    scheduled = schedule_client.create_schedule.await_args.args[1]
    assert scheduled.spec.time_zone_name == "Asia/Kolkata"
    action_input = scheduled.action.args[0]
    environment = await WorkflowEnvironment.start_time_skipping()
    async with environment:
        async with Worker(
            environment.client,
            task_queue=queue,
            workflows=[DynamicBusinessWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                execute_tool,
                record_step,
                send_notification,
                request_confirmation,
                clear_confirmation,
                start_scheduled_run,
                finish_workflow_run,
            ],
        ):
            handle = await environment.client.start_workflow(
                DynamicBusinessWorkflow.run,
                action_input,
                id=f"scheduled-test-{uuid.uuid4()}",
                task_queue=queue,
            )
            await handle.result()
            await asyncio.wait_for(fired.wait(), timeout=20)
    assert fired.is_set()


@pytest.mark.asyncio
async def test_pause_deletes_schedule_before_marking_inactive() -> None:
    from workflow_builder.activation import pause_workflow_execution

    workflow = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "trigger_type": "schedule",
    }
    calls: list[str] = []

    async def delete(_workflow_id: str) -> None:
        calls.append("delete")

    async def deactivate(*_args, **_kwargs) -> dict:
        calls.append("deactivate")
        return {**workflow, "is_active": False}

    with (
        patch(
            "workflow_builder.activation.get_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "workflow_builder.deployer.delete_temporal_schedule",
            new=delete,
        ),
        patch(
            "workflow_builder.activation.set_active",
            new=deactivate,
        ),
    ):
        result = await pause_workflow_execution(
            BUSINESS,
            str(workflow["id"]),
        )
    assert result["is_active"] is False
    assert calls == ["delete", "deactivate"]


@pytest.mark.asyncio
async def test_resume_recreates_schedule_with_existing_shadow_settings() -> None:
    from workflow_builder.activation import resume_workflow_execution

    workflow = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "trigger_type": "schedule",
        "shadow_mode": True,
        "shadow_allowlist": ["owner-test"],
    }
    activate = AsyncMock(return_value={**workflow, "is_active": True})
    with (
        patch(
            "workflow_builder.activation.get_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "workflow_builder.activation.activate_tested_workflow",
            activate,
        ),
    ):
        await resume_workflow_execution(BUSINESS, str(workflow["id"]))
    activate.assert_awaited_once_with(
        BUSINESS,
        workflow["id"],
        shadow_mode=True,
        shadow_allowlist=["owner-test"],
    )


@pytest.mark.asyncio
async def test_schedule_activation_failure_rolls_back_active_flag() -> None:
    from workflow_builder.activation import activate_tested_workflow

    workflow = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "name": "Reminder",
        "trigger_type": "schedule",
        "is_tested": True,
        "metadata": {"test_results": {"all_passed": True}},
    }
    set_active = AsyncMock(
        side_effect=[
            {**workflow, "is_active": True},
            {**workflow, "is_active": False},
        ]
    )
    with (
        patch(
            "workflow_builder.activation.get_workflow",
            new=AsyncMock(return_value=workflow),
        ),
        patch(
            "workflow_builder.activation.set_active",
            set_active,
        ),
        patch(
            "workflow_builder.deployer.ensure_temporal_schedule",
            new=AsyncMock(side_effect=RuntimeError("Temporal unavailable")),
        ),
    ):
        with pytest.raises(RuntimeError, match="Temporal unavailable"):
            await activate_tested_workflow(BUSINESS, str(workflow["id"]))
    assert set_active.await_count == 2
    assert set_active.await_args_list[1].args == (
        BUSINESS,
        workflow["id"],
        False,
    )


@pytest.mark.asyncio
async def test_schedule_delete_failure_is_not_silenced() -> None:
    from workflow_builder.deployer import delete_temporal_schedule

    handle = MagicMock()
    handle.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client = MagicMock()
    client.get_schedule_handle.return_value = handle
    with patch(
        "workflow_builder.deployer._client",
        new=AsyncMock(return_value=client),
    ):
        with pytest.raises(RuntimeError, match="delete failed"):
            await delete_temporal_schedule("workflow-id")
