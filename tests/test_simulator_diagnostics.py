"""The simulator must show what actually happened.

Locks down the three defects behind an owner turn that displayed "Turn completed"
with a 0ms trace: an inverted broker/specialist timeout, a swallowed error, and
per-stage timings that were never measured. Also pins that lab traffic can never
reach a live channel through the retry path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from admin.pipeline_trace import PipelineTrace
from admin.test_simulator_lab import _trace_from_result
from agent.config import MULTI_AGENT_CONFIG, broker_budget_s
from agent.error_handling import (
    HOLDING_MESSAGE,
    process_retry_queues,
    queue_for_retry,
    send_holding_message,
)
from agent.stage_timings import StageTimings, timings_map
from shared.redis_keys import retry_queue_key
from tests.test_duplicate_replies import BUSINESS, MESSAGE_ID  # noqa: F401
from tests.test_duplicate_replies import (  # noqa: F401
    FakeRedis,
    as_customer,
    redis,
    session_env,
)

SIMULATOR = {"business_id": BUSINESS, "mode": "owner", "customer_name": "Owner"}


# ── Timeout nesting ──────────────────────────────────────────────────────────


def test_broker_budget_outlasts_the_specialist_it_waits_on() -> None:
    """A budget <= the inner timeout always fires first and eats the reply."""
    inner = max(
        float(MULTI_AGENT_CONFIG["specialist_timeout_s"]),
        float(MULTI_AGENT_CONFIG["owner_specialist_timeout_s"]),
    )
    assert broker_budget_s() > inner
    # Room for the broker's own LLM turns and at least one cold tool hop.
    assert broker_budget_s() - inner >= 30


def test_broker_uses_the_derived_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.broker as broker

    assert "broker_timeout_s" not in MULTI_AGENT_CONFIG
    assert broker.broker_budget_s is broker_budget_s


# ── Stage timings ────────────────────────────────────────────────────────────


def test_stage_timings_record_order_and_duration() -> None:
    stages = StageTimings()
    stages.mark("identity")
    stages.mark("agent", status="error", detail="TimeoutError")
    recorded = stages.to_list()

    assert [stage["name"] for stage in recorded] == ["identity", "agent"]
    assert recorded[1]["status"] == "error"
    assert all(isinstance(stage["ms"], float) for stage in recorded)
    assert stages.total_ms >= 0


def test_timings_map_sums_repeated_stages() -> None:
    mapped = timings_map(
        [
            {"name": "agent", "ms": 10.5},
            {"name": "agent", "ms": 4.5},
            {"name": "identity", "ms": 1},
            {"name": "", "ms": 99},
        ]
    )
    assert mapped == {"agent": 15.0, "identity": 1.0}


def _event() -> dict[str, Any]:
    return {
        "business_id": BUSINESS,
        "sender_id": "sim-owner-1",
        "channel": "whatsapp",
        "event_type": "message",
        "simulator": SIMULATOR,
    }


def _steps(trace: PipelineTrace) -> dict[str, dict[str, Any]]:
    return {str(step["name"]): step for step in trace.steps}


def test_trace_reports_measured_milliseconds_not_zero() -> None:
    trace = PipelineTrace()
    _trace_from_result(
        trace,
        event=_event(),
        result={
            "status": "responded",
            "response_text": "done",
            "outbound": "skip_send",
            "specialist": "workflow_agent",
            "route_method": "broker",
            "tools_called": ["list_workflow_rules"],
            "workflow_matched": True,
            "stage_timings": [
                {"name": "identity", "ms": 3.5, "status": "ok"},
                {"name": "workflow", "ms": 120.0, "status": "ok"},
                {"name": "context", "ms": 45.0, "status": "ok"},
                {"name": "agent", "ms": 21500.0, "status": "ok"},
                {"name": "outbound", "ms": 2.0, "status": "ok"},
            ],
            "ai_metadata": {"thread_id": "t-1", "router_latency_ms": 900},
        },
        parse_ms=0.8,
    )
    steps = _steps(trace)
    assert steps["identity"]["ms"] == 3.5
    # The pipeline's "agent" stage is the trace's specialist step.
    assert steps["specialist"]["ms"] == 21500.0
    assert steps["workflow"]["ms"] == 120.0
    assert steps["context"]["ms"] == 45.0
    assert steps["router"]["ms"] == 900
    assert steps["outbound"]["ms"] == 2.0
    assert steps["parse"]["ms"] == 0.8


def test_trace_marks_the_failing_stage_and_keeps_the_reason() -> None:
    trace = PipelineTrace()
    _trace_from_result(
        trace,
        event=_event(),
        result={
            "status": "error",
            "error": "TimeoutError",
            "response_text": None,
            "stage_timings": [
                {"name": "identity", "ms": 2.0, "status": "ok"},
                {"name": "workflow", "ms": 10.0, "status": "skip"},
                {"name": "context", "ms": 30.0, "status": "ok"},
                {"name": "agent", "ms": 30000.0, "status": "error", "detail": "TimeoutError"},
            ],
        },
        error="TimeoutError",
    )
    steps = _steps(trace)
    assert steps["specialist"]["status"] == "error"
    assert "TimeoutError" in steps["specialist"]["detail"]
    assert steps["specialist"]["ms"] == 30000.0
    # No outbound stage: the turn never reached the send step.
    assert "outbound" not in steps


def test_unmeasured_stage_has_no_fake_timing() -> None:
    trace = PipelineTrace()
    trace.add("tools", detail="none")
    assert trace.steps[0]["ms"] is None


# ── Failures are surfaced, and lab traffic stays in the lab ──────────────────


@pytest.mark.asyncio
async def test_simulator_failure_returns_the_reason_and_stage_timings(
    redis: FakeRedis,
    session_env: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import session

    async def _boom(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr("agent.session._invoke_agent_with_llm_retries", _boom)
    monkeypatch.setattr(
        "agent.pipeline.emit_ai_workflow_event", AsyncMock(return_value=False)
    )
    notify = AsyncMock()
    monkeypatch.setattr("agent.session.notify_owner_of_outage", notify)

    result = await session.handle_incoming_message(
        channel="whatsapp",
        external_sender_id="sim-owner-1",
        message_text="i want to create the workflow",
        receiving_account_id=BUSINESS,
        skip_send=True,
        simulator=SIMULATOR,
    )

    assert result["status"] == "error"
    # A bare TimeoutError has no message; the type name must still surface.
    assert result["error"] == "TimeoutError"
    stages = {stage["name"]: stage for stage in result["stage_timings"]}
    assert stages["agent"]["status"] == "error"
    assert stages["agent"]["detail"] == "TimeoutError"
    assert stages["identity"]["ms"] >= 0
    # A lab turn must not page the owner or replay through the live path.
    notify.assert_not_awaited()
    assert redis.lists.get(retry_queue_key(BUSINESS)) in (None, [])
    session_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_simulator_error_reaches_the_lab_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from admin import test_simulator_lab

    monkeypatch.setattr(
        "agent.session.handle_incoming_message",
        AsyncMock(
            return_value={
                "status": "error",
                "error": "TimeoutError",
                "response_text": None,
                "stage_timings": [
                    {"name": "identity", "ms": 1.0, "status": "ok"},
                    {"name": "agent", "ms": 30000.0, "status": "error", "detail": "TimeoutError"},
                ],
            }
        ),
    )

    out = await test_simulator_lab.run_synthetic_message(
        business_id=BUSINESS,
        text="i want to create the workflow",
        channel="whatsapp",
        mode="owner",
    )

    assert out["error"] == "TimeoutError"
    assert out["response"] is None
    steps = {step["name"]: step for step in out["trace"]["steps"]}
    assert steps["specialist"]["status"] == "error"


@pytest.mark.asyncio
async def test_retry_entry_round_trips_simulator_flags(redis: FakeRedis) -> None:
    await queue_for_retry(
        business_id=BUSINESS,
        customer_id=None,
        channel="whatsapp",
        message_text="lab turn",
        error="TimeoutError",
        external_sender_id="sim-owner-1",
        receiving_account_id=BUSINESS,
        message_id=MESSAGE_ID,
        skip_send=True,
        simulator=SIMULATOR,
    )
    stored = json.loads(redis.lists[retry_queue_key(BUSINESS)][0])
    assert stored["skip_send"] is True
    assert stored["simulator"] == SIMULATOR

    seen: list[dict[str, Any]] = []

    async def _handler(event: dict[str, Any]) -> dict[str, Any]:
        seen.append(event)
        return {"status": "responded"}

    await process_retry_queues(_handler)
    assert seen[0]["skip_send"] is True
    assert seen[0]["simulator"] == SIMULATOR


@pytest.mark.asyncio
async def test_exhausted_simulator_retry_sends_no_holding_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock()
    monkeypatch.setattr("agent.pipeline.send_response", send)

    await send_holding_message(
        {
            "business_id": BUSINESS,
            "channel": "whatsapp",
            "external_sender_id": "sim-owner-1",
            "message_id": MESSAGE_ID,
            "skip_send": True,
            "simulator": SIMULATOR,
        }
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_turn_still_holds_after_the_cap(
    redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The simulator guard must not disarm the real holding message."""
    send = AsyncMock()
    monkeypatch.setattr("agent.pipeline.send_response", send)
    monkeypatch.setattr("agent.pipeline.mark_message_answered", AsyncMock())
    monkeypatch.setattr(
        "agent.pipeline.message_already_answered", AsyncMock(return_value=False)
    )

    await send_holding_message(
        {
            "business_id": BUSINESS,
            "channel": "whatsapp",
            "external_sender_id": "+919427030666",
            "message_id": MESSAGE_ID,
        }
    )
    send.assert_awaited_once()
    assert send.await_args.kwargs["text"] == HOLDING_MESSAGE
