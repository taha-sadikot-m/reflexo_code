"""Stage T1: Temporal is the only workflow engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.test_duplicate_replies import (  # noqa: F401
    FakeRedis,
    as_customer,
    redis,
    session_env,
)
from workflow_builder.legacy_rules import (
    compile_rule_definition,
    create_temporal_rule,
)
from workflow_builder.models import StepType

BUSINESS = "918750d7-2254-4cfd-bd02-c22c3ab708ec"


@pytest.mark.asyncio
async def test_customer_temporal_match_consumes_turn_before_agent(
    redis: FakeRedis,
    session_env: AsyncMock,
    as_customer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.session import handle_incoming_message

    as_customer()
    route = AsyncMock(return_value=True)
    log_inbound = AsyncMock()
    create_agent = AsyncMock(return_value=object())
    monkeypatch.setattr("agent.pipeline.emit_ai_workflow_event", route)
    monkeypatch.setattr("agent.session.log_inbound_message", log_inbound)
    monkeypatch.setattr("agent.session.create_agent_for_session", create_agent)

    result = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919000000001",
        message_text="#123456",
        receiving_account_id="pnid",
        external_message_id="wamid.temporal",
    )

    assert result["status"] == "workflow_consumed"
    assert result["workflow_matched"] is True
    assert result["route_method"] == "temporal"
    assert result["outbound"] == "temporal_workflow"
    log_inbound.assert_awaited_once()
    create_agent.assert_not_awaited()
    session_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_temporal_match_continues_to_agent(
    redis: FakeRedis,
    session_env: AsyncMock,
    as_customer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.session import handle_incoming_message
    from tests.test_duplicate_replies import _invoke_returns

    as_customer()
    monkeypatch.setattr(
        "agent.pipeline.emit_ai_workflow_event", AsyncMock(return_value=False)
    )
    _invoke_returns(monkeypatch, "Normal specialist reply")

    result = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919000000001",
        message_text="What are your hours?",
        receiving_account_id="pnid",
        external_message_id="wamid.normal",
        skip_send=True,
    )

    assert result["response_text"] == "Normal specialist reply"
    assert result["workflow_matched"] is False


@pytest.mark.asyncio
async def test_owner_event_never_routes_to_temporal(
    redis: FakeRedis,
    session_env: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.session import handle_incoming_message
    from tests.test_duplicate_replies import _invoke_returns

    route = AsyncMock(return_value=False)
    monkeypatch.setattr("workflow_builder.deployer.route_event_to_workflows", route)
    _invoke_returns(monkeypatch, "Owner reply")

    result = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919000000002",
        message_text="Create a workflow",
        receiving_account_id="pnid",
        external_message_id="wamid.owner",
        skip_send=True,
    )

    assert result["response_text"] == "Owner reply"
    route.assert_not_awaited()


def test_legacy_rule_compiles_to_sequential_call_tool_definition() -> None:
    definition = compile_rule_definition(
        name="Discount code",
        description="Reply when the discount code is sent",
        trigger_type="whatsapp_message",
        conditions=[
            {"field": "message.text", "operator": "contains", "value": "#123456"}
        ],
        action_sequence=[
            {"tool": "search_knowledge", "params": {"query": "{{message.text}}"}},
            {
                "tool": "send_whatsapp_message",
                "params": {"text": "Here is your 10% discount"},
            },
        ],
    )

    assert all(step.type == StepType.CALL_TOOL for step in definition.steps)
    assert definition.steps[1].depends_on == [definition.steps[0].name]
    assert definition.conditions[0]["value"] == "#123456"
    assert definition.instance_key == "{{customer.id}}"
    assert definition.steps[1].params["to"] == "{{external_sender_id}}"


def test_conditions_fail_closed_and_do_not_consume_general_chat() -> None:
    from workflow_builder.deployer import event_matches_workflow

    workflow = {
        "conditions": [
            {"field": "message.text", "operator": "contains", "value": "#123456"},
            {"field": "channel", "operator": "equals", "value": "whatsapp"},
        ]
    }
    assert event_matches_workflow(
        workflow,
        {"message": {"text": "code #123456"}, "channel": "whatsapp"},
    )
    assert not event_matches_workflow(
        workflow,
        {"message": {"text": "What are your hours?"}, "channel": "whatsapp"},
    )
    assert not event_matches_workflow(
        {"conditions": [{"field": "channel", "operator": "typo", "value": "x"}]},
        {"channel": "x"},
    )


@pytest.mark.asyncio
async def test_legacy_create_writes_temporal_draft_not_workflow_rules() -> None:
    saved = {"id": "temporal-1", "name": "Discount", "is_active": False}
    with (
        patch(
            "workflow_builder.legacy_rules.available_tools_for_business",
            new=AsyncMock(
                return_value=[
                    {"name": "send_whatsapp_message", "description": "send"}
                ]
            ),
        ),
        patch(
            "workflow_builder.legacy_rules.deploy_workflow",
            new=AsyncMock(return_value=saved),
        ) as deploy,
    ):
        result = await create_temporal_rule(
            BUSINESS,
            name="Discount",
            trigger_type="whatsapp_message",
            action_sequence=[
                {"tool": "send_whatsapp_message", "params": {"text": "10% off"}}
            ],
        )

    assert result["status"] == "draft_created"
    assert result["engine"] == "temporal"
    assert result["is_active"] is False
    deploy.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_executes_with_compiled_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.activities import execute_tool_activity

    call = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr("agent.tools.call_mcp_tool", call)
    monkeypatch.setattr(
        "workflow_builder.activities._is_personal_recipient",
        AsyncMock(return_value=False),
    )
    definition = compile_rule_definition(
        name="Discount",
        trigger_type="whatsapp_message",
        action_sequence=[
            {"tool": "send_whatsapp_message", "params": {"text": "10% off"}}
        ],
    )
    params = {
        key: (
            "+919000000001"
            if value == "{{external_sender_id}}"
            else value
        )
        for key, value in definition.steps[0].params.items()
    }

    await execute_tool_activity(
        "send_whatsapp_message",
        params,
        BUSINESS,
    )
    call.assert_awaited_once_with(
        "meta",
        "send_whatsapp_message",
        {
            "business_id": BUSINESS,
            "text": "10% off",
            "to": "+919000000001",
        },
    )


@pytest.mark.asyncio
async def test_simulated_outbound_never_calls_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.activities import execute_tool_activity

    call = AsyncMock()
    monkeypatch.setattr("agent.tools.call_mcp_tool", call)
    result = await execute_tool_activity(
        "send_whatsapp_message",
        {
            "to": "+919000000001",
            "text": "10% off",
            "simulate_outbound": True,
        },
        BUSINESS,
    )
    assert result["status"] == "simulated"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_execution_cap_stops_temporal_routing() -> None:
    from workflow_builder.deployer import route_event_to_workflows

    workflow = {
        "id": "wf-cap",
        "trigger_type": "whatsapp_message",
        "trigger_config": {},
        "conditions": [],
        "max_executions_per_day": 1,
        "total_runs": 5,
    }
    with (
        patch(
            "workflow_builder.deployer.matching_active",
            new=AsyncMock(return_value=[workflow]),
        ),
        patch(
            "workflow_builder.store.runs_last_24h",
            new=AsyncMock(return_value=[{"status": "success"}]),
        ),
        patch("workflow_builder.deployer._client", new=AsyncMock()) as client,
    ):
        consumed = await route_event_to_workflows(
            {
                "type": "whatsapp_message",
                "message": {"text": "hello"},
                "channel": "whatsapp",
            },
            BUSINESS,
        )
    assert consumed is False
    client.assert_not_awaited()


def test_owner_confirmation_parser() -> None:
    from workflow_builder.confirmation import parse_confirmation

    assert parse_confirmation("YES") is True
    assert parse_confirmation("haan") is True
    assert parse_confirmation("NO") is False
    assert parse_confirmation("maybe") is None
    assert parse_confirmation("y that is fine, also change hours") is None
    assert parse_confirmation("ha update the menu") is None


def test_temporal_payload_keeps_confirmation_and_daily_cap() -> None:
    from workflow_builder.deployer import _definition_dict

    payload = _definition_dict(
        {
            "id": "wf-confirm",
            "name": "Confirmed discount",
            "trigger_type": "whatsapp_message",
            "steps": [],
            "requires_owner_confirmation": True,
            "max_executions_per_day": 7,
        }
    )
    assert payload["workflow_id"] == "wf-confirm"
    assert payload["requires_owner_confirmation"] is True
    assert payload["max_executions_per_day"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval", "expected_status", "expected_calls"),
    [
        (True, "success", ["send_whatsapp_message"]),
        (False, "cancelled", []),
        (None, "cancelled", []),
    ],
)
async def test_temporal_confirmation_is_fail_closed(
    approval: bool | None,
    expected_status: str,
    expected_calls: list[str],
) -> None:
    import asyncio
    import uuid

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from workflow_builder.interpreter import DynamicBusinessWorkflow

    confirmation_requested = asyncio.Event()
    tool_calls: list[str] = []

    @activity.defn(name="request_workflow_confirmation_activity")
    async def request_confirmation(*_args) -> dict:
        confirmation_requested.set()
        return {"status": "pending"}

    @activity.defn(name="clear_workflow_confirmation_activity")
    async def clear_confirmation(*_args) -> None:
        return None

    @activity.defn(name="execute_tool_activity")
    async def execute_tool(tool: str, _params: dict, _business_id: str) -> dict:
        tool_calls.append(tool)
        return {"status": "sent"}

    @activity.defn(name="record_workflow_step")
    async def record_step(*_args) -> None:
        return None

    @activity.defn(name="send_notification_activity")
    async def send_notification(*_args) -> dict:
        return {"status": "sent"}

    @activity.defn(name="start_scheduled_run_activity")
    async def start_scheduled_run(*_args) -> str:
        return "scheduled-run"

    @activity.defn(name="finish_workflow_run_activity")
    async def finish_workflow_run(*_args) -> None:
        return None

    environment = await WorkflowEnvironment.start_time_skipping()
    async with environment:
        queue = f"t1-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=queue,
            workflows=[DynamicBusinessWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                request_confirmation,
                clear_confirmation,
                execute_tool,
                record_step,
                send_notification,
                start_scheduled_run,
                finish_workflow_run,
            ],
        ):
            handle = await environment.client.start_workflow(
                DynamicBusinessWorkflow.run,
                {
                    "business_id": BUSINESS,
                    "run_id": "probe",
                    "definition": {
                        "workflow_id": "wf-confirm",
                        "name": "Confirmed send",
                        "description": "Send a discount",
                        "requires_owner_confirmation": True,
                        "steps": [
                            {
                                "name": "send",
                                "type": "call_tool",
                                "tool": "send_whatsapp_message",
                                "params": {
                                    "to": "{{external_sender_id}}",
                                    "text": "10% off",
                                },
                            }
                        ],
                    },
                    "trigger_event": {
                        "type": "whatsapp_message",
                        "external_sender_id": "+919000000001",
                    },
                },
                id=f"t1-confirm-{uuid.uuid4()}",
                task_queue=queue,
            )
            await asyncio.wait_for(confirmation_requested.wait(), timeout=10)
            assert tool_calls == []
            if approval is not None:
                await handle.signal(
                    DynamicBusinessWorkflow.on_event,
                    {"type": "workflow_confirmation", "approved": approval},
                )
            result = await handle.result()
            assert result["status"] == expected_status
            assert tool_calls == expected_calls
