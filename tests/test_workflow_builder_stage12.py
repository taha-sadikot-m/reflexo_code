"""Stage 12 — AI workflow builder (mocked LLM + Temporal)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workflow_builder.models import StepType, WorkflowDefinition, WorkflowStep
from workflow_builder.simulator import simulate_workflow
from workflow_builder.validator import validate_workflow

BIZ = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _price_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Instagram price reply",
        description="Search products and reply with prices",
        trigger_type="instagram_dm",
        steps=[
            WorkflowStep(name="search", type=StepType.CALL_TOOL, tool="search_entities", params={"query": "{{message.text}}"}),
            WorkflowStep(
                name="reply",
                type=StepType.CALL_TOOL,
                tool="send_instagram_dm",
                params={"text": "{{result.search.summary}}"},
                depends_on=["search"],
            ),
        ],
        nl_description="When someone asks about prices on Instagram, search products and reply with prices",
    )


def _coupon_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Three comments coupon",
        description="Coupon after 3 comments",
        trigger_type="instagram_comment",
        instance_key="{{customer.id}}",
        steps=[
            WorkflowStep(
                name="wait_for_comments",
                type=StepType.AGGREGATE_EVENTS,
                event_filter={"type": "instagram_comment"},
                threshold=3,
                window_seconds=604800,
            ),
            WorkflowStep(
                name="send_coupon",
                type=StepType.CALL_TOOL,
                tool="send_whatsapp_message",
                params={"text": "Thanks! Here is 10% off."},
                depends_on=["wait_for_comments"],
            ),
        ],
        nl_description="If someone comments on 3 of my posts, send them a coupon",
    )


def _tools() -> list[dict[str, str]]:
    from workflow_builder.catalog import core_tool_catalog

    return core_tool_catalog()


@pytest.mark.asyncio
async def test_t12_1_price_prompt_decomposes_two_call_tools() -> None:
    definition = _price_definition()
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=definition)
    with patch("workflow_builder.decomposer.ChatGoogleGenerativeAI", return_value=llm):
        from workflow_builder.decomposer import decompose_workflow

        result = await decompose_workflow(
            "When someone asks about prices on Instagram, search products and reply with prices",
            available_tools=_tools(),
        )
    assert result.trigger_type == "instagram_dm"
    call_tools = [s for s in result.steps if s.type == StepType.CALL_TOOL]
    assert len(call_tools) == 2


@pytest.mark.asyncio
async def test_t12_2_coupon_prompt_uses_aggregate_and_instance_key() -> None:
    definition = _coupon_definition()
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=definition)
    with patch("workflow_builder.decomposer.ChatGoogleGenerativeAI", return_value=llm):
        from workflow_builder.decomposer import decompose_workflow

        result = await decompose_workflow(
            "If someone comments on 3 of my posts, send them a coupon",
            available_tools=_tools(),
        )
    assert result.instance_key == "{{customer.id}}"
    assert any(s.type == StepType.AGGREGATE_EVENTS and s.threshold == 3 for s in result.steps)
    assert any(s.type == StepType.CALL_TOOL for s in result.steps)


@pytest.mark.asyncio
async def test_t12_3_validator_rejects_unknown_tool() -> None:
    definition = _price_definition()
    definition.steps[0].tool = "not_a_real_tool"
    result = await validate_workflow(definition, _tools())
    assert result.is_valid is False
    assert any("unknown tool" in err for err in result.errors)


@pytest.mark.asyncio
async def test_t12_4_validator_requires_instance_key_for_aggregation() -> None:
    definition = _coupon_definition()
    definition.instance_key = None
    result = await validate_workflow(definition, _tools())
    assert result.is_valid is False
    assert any("instance_key" in err for err in result.errors)


@pytest.mark.asyncio
async def test_t12_5_simulator_has_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def boom(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("MCP must not run during simulation")

    monkeypatch.setattr("agent.tools.call_mcp_tool", boom)
    definition = _coupon_definition()
    definition.steps.insert(
        1,
        WorkflowStep(name="wait", type=StepType.DELAY, delay_seconds=7200, depends_on=["wait_for_comments"]),
    )
    definition.steps[2].depends_on = ["wait"]
    trace = await simulate_workflow(definition, business_id=BIZ)
    assert called is False
    assert trace["side_effects"] is False
    statuses = " ".join(str(s.get("status")) for s in trace["steps"])
    assert "would wait for 3 events" in statuses
    assert "would sleep" in statuses


@pytest.mark.asyncio
async def test_t12_6_deploy_persists_row() -> None:
    row = {"id": "wf-1", "name": "Instagram price reply", "is_active": True}
    with (
        patch("workflow_builder.deployer.insert_workflow", new=AsyncMock(return_value=row)),
        patch(
            "workflow_builder.deployer.mark_prepared",
            new=AsyncMock(
                return_value={
                    **row,
                    "is_active": False,
                    "is_tested": False,
                    "temporal_workflow_id": "reflexo_wf_wf-1",
                }
            ),
        ) as mark,
    ):
        from workflow_builder.deployer import deploy_workflow

        saved = await deploy_workflow(BIZ, _price_definition())
    assert saved["temporal_workflow_id"] == "reflexo_wf_wf-1"
    assert saved["is_active"] is False
    mark.assert_awaited()


@pytest.mark.asyncio
async def test_t12_7_and_t12_8_start_then_signal_same_instance() -> None:
    wf = {
        "id": "wf-agg",
        "temporal_workflow_id": "reflexo_wf_wf-agg",
        "instance_key": "{{customer.id}}",
        "trigger_type": "instagram_comment",
        "steps": [],
        "max_duration_seconds": 86400,
        "name": "coupon",
    }
    event = {
        "type": "instagram_comment",
        "trigger_type": "instagram_comment",
        "customer": {"id": "c1"},
        "customer_id": "c1",
    }
    handle = MagicMock()
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    client.start_workflow = AsyncMock()
    handle.signal = AsyncMock()

    with (
        patch("workflow_builder.deployer.matching_active", new=AsyncMock(return_value=[wf])),
        patch("workflow_builder.deployer.insert_run", new=AsyncMock(return_value={"id": "run-1"})),
        patch("workflow_builder.deployer._client", new=AsyncMock(return_value=client)),
    ):
        from workflow_builder.deployer import route_event_to_workflows

        handle.describe = AsyncMock(side_effect=RuntimeError("not found"))
        started = await route_event_to_workflows(event, BIZ)
        assert started is True
        client.start_workflow.assert_awaited()
        start_id = client.start_workflow.await_args.kwargs["id"]
        assert start_id.endswith(":c1")

        handle.describe = AsyncMock(
            return_value=SimpleNamespace(status=MagicMock())
        )
        from temporalio.client import WorkflowExecutionStatus

        handle.describe.return_value.status = WorkflowExecutionStatus.RUNNING
        client.start_workflow.reset_mock()
        signaled = await route_event_to_workflows(event, BIZ)
        assert signaled is True
        handle.signal.assert_awaited()
        client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_t12_9_pause_stops_routing() -> None:
    with patch("workflow_builder.store.set_active", new=AsyncMock(return_value={"id": "wf-1", "is_active": False})) as pause:
        from workflow_builder.store import set_active

        row = await set_active(BIZ, "wf-1", False)
    assert row["is_active"] is False
    pause.assert_awaited_once_with(BIZ, "wf-1", False)

    with (
        patch("workflow_builder.deployer.matching_active", new=AsyncMock(return_value=[])),
        patch("workflow_builder.deployer._client", new=AsyncMock()) as client,
    ):
        from workflow_builder.deployer import route_event_to_workflows

        handled = await route_event_to_workflows(
            {"type": "instagram_comment", "trigger_type": "instagram_comment"}, BIZ
        )
    assert handled is False
    client.assert_not_awaited()


@pytest.mark.asyncio
async def test_t12_10_monitor_auto_pauses_high_failure_rate() -> None:
    rows = [{"status": "failed"}] * 3 + [{"status": "success"}]
    with (
        patch("workflow_builder.monitor.list_workflows", new=AsyncMock(return_value=[{"id": "wf-1", "name": "coupon"}])),
        patch("workflow_builder.monitor.runs_last_24h", new=AsyncMock(return_value=rows)),
        patch("workflow_builder.monitor.auto_pause", new=AsyncMock()) as pause,
        patch("workflow_builder.monitor._notify_owner", new=AsyncMock()),
    ):
        from workflow_builder.monitor import scan_and_auto_pause

        paused = await scan_and_auto_pause(BIZ)
    assert paused == ["wf-1"]
    pause.assert_awaited()
