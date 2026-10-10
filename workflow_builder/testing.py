"""Stateful scenario tests for AI-built Temporal workflows."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from shared.simulator_inbound import build_simulator_inbound_event
from workflow_builder.deployer import (
    query_workflow_state,
    route_event_to_workflows_detailed,
    terminate_test_workflow,
)
from workflow_builder.models import CHANNEL_TO_TRIGGER
from workflow_builder.store import finish_run, get_workflow

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

TRIGGER_TO_CHANNEL = {
    trigger: channel for channel, trigger in CHANNEL_TO_TRIGGER.items()
}
TRIGGER_TO_CHANNEL.update(
    {
        "facebook_message": "facebook_messenger",
        "email_received": "web",
        "voice_call": "whatsapp",
        "manual": "web",
        "payment_received": "web",
        "payment_failed": "web",
        "order_created": "web",
        "schedule": "web",
    }
)


def auto_select_scenario(definition: dict[str, Any]) -> dict[str, Any]:
    """Choose scenario shape from trigger and step semantics."""
    trigger = str(definition.get("trigger_type") or "manual")
    steps = definition.get("steps") or []
    has_aggregation = any(step.get("type") == "aggregate_events" for step in steps)
    has_counter = has_aggregation or any(
        step.get("type") == "condition"
        and "counter" in str(step.get("expression") or "").lower()
        for step in steps
    )
    has_delay = any(step.get("type") == "delay" for step in steps)
    trigger_channel = TRIGGER_TO_CHANNEL.get(trigger, "web")
    outbound_channels = {
        _tool_channel(str(step.get("tool") or ""))
        for step in steps
        if step.get("type") == "call_tool"
    }
    has_cross_channel = any(
        channel and channel != trigger_channel for channel in outbound_channels
    )
    threshold = max(
        [
            int(step.get("threshold") or 1)
            for step in steps
            if step.get("type") == "aggregate_events"
        ]
        or [1]
    )

    if trigger in {"instagram_comment", "facebook_comment"} and has_counter:
        return {
            "type": "giveaway",
            "preset_id": "comment_giveaway",
            "events": min(max(threshold, 3), 25),
            "edge_cases": ["duplicate", "over_limit", "non_matching"],
            "simulate_time": True,
            "same_actor": has_aggregation
            and "customer" in str(definition.get("instance_key") or ""),
            "description": "Comment aggregation/counter workflow",
        }
    if trigger in {"instagram_comment", "facebook_comment"} and has_cross_channel:
        return {
            "type": "comment_to_dm_funnel",
            "preset_id": "comment_to_dm_funnel",
            "events": 5,
            "edge_cases": ["duplicate", "non_matching"],
            "simulate_time": True,
            "description": "Comment to cross-channel action",
        }
    if "mention" in trigger:
        return {
            "type": "mention_response",
            "preset_id": "mention_auto_reply",
            "events": 5,
            "edge_cases": ["non_follower"],
            "simulate_time": True,
            "description": "Mention-triggered response",
        }
    if has_delay and has_aggregation:
        scenario_type = "multi_step_sequence"
        edges = ["partial_progress", "early_completion"]
    elif has_delay:
        scenario_type = "delayed_followup"
        edges = ["reply_before_delay", "no_reply"]
    else:
        scenario_type = "generic"
        edges = ["duplicate", "malformed_input"]
    return {
        "type": scenario_type,
        "preset_id": None,
        "events": 5,
        "edge_cases": edges,
        "simulate_time": has_delay,
        "description": f"{trigger.replace('_', ' ').title()} workflow",
    }


def _tool_channel(tool: str) -> str | None:
    lowered = tool.lower()
    if "instagram" in lowered:
        return "instagram_dm"
    if "facebook" in lowered or "messenger" in lowered:
        return "facebook_messenger"
    if "whatsapp" in lowered or "sms" in lowered:
        return "whatsapp"
    if "email" in lowered:
        return "email"
    return None


def _keyword(definition: dict[str, Any]) -> str:
    config = definition.get("trigger_config") or {}
    if isinstance(config, dict):
        if config.get("keyword"):
            return str(config["keyword"])
        keywords = config.get("keywords")
        if isinstance(keywords, list) and keywords:
            return str(keywords[0])
    return "test"


def generate_scenario_events(
    business_id: str,
    definition: dict[str, Any],
    scenario: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate production-shaped, always-safe events for a selected scenario."""
    trigger = str(definition.get("trigger_type") or "manual")
    channel = TRIGGER_TO_CHANNEL.get(trigger, "web")
    count = int(scenario.get("events") or 5)
    keyword = _keyword(definition)
    shared_post = f"sim-post-{uuid.uuid4().hex[:10]}"
    shared_actor = f"sim-actor-{uuid.uuid4().hex[:10]}"
    instance_template = str(definition.get("instance_key") or "")
    events: list[dict[str, Any]] = []

    for index in range(count):
        sender = (
            shared_actor
            if scenario.get("same_actor")
            else shared_actor
            if "post_id" in instance_template
            or "business" in instance_template
            or not instance_template
            else f"sim-user-{index + 1}-{uuid.uuid4().hex[:6]}"
        )
        text = (
            f"@business {keyword}"
            if "mention" in trigger
            else keyword
        )
        event = build_simulator_inbound_event(
            business_id=business_id,
            text=text,
            channel=channel,
            mode="customer",
            session_id=f"sim-wft-{uuid.uuid4().hex[:10]}",
            customer_name=f"Test user {index + 1}",
            sender_id=sender,
            post_id=shared_post,
        )
        events.append(event)

    edges = set(scenario.get("edge_cases") or [])
    if "duplicate" in edges and events:
        duplicate = dict(events[0])
        duplicate["session_id"] = f"sim-wft-{uuid.uuid4().hex[:10]}"
        duplicate["message_id"] = f"sim-msg-{uuid.uuid4().hex[:16]}"
        if duplicate.get("comment_id"):
            duplicate["comment_id"] = f"sim-comment-{uuid.uuid4().hex[:12]}"
        events.append(duplicate)
    if "over_limit" in edges:
        extra = build_simulator_inbound_event(
            business_id=business_id,
            text=keyword,
            channel=channel,
            mode="customer",
            session_id=f"sim-wft-{uuid.uuid4().hex[:10]}",
            customer_name="Over limit user",
            sender_id=f"sim-over-{uuid.uuid4().hex[:6]}",
            post_id=shared_post,
        )
        events.append(extra)
    if "non_matching" in edges:
        nonmatch = build_simulator_inbound_event(
            business_id=business_id,
            text="a deliberately unrelated message",
            channel=channel,
            mode="customer",
            session_id=f"sim-wft-{uuid.uuid4().hex[:10]}",
            customer_name="Non matching user",
            sender_id=f"sim-nomatch-{uuid.uuid4().hex[:6]}",
            post_id=shared_post,
        )
        events.append(nonmatch)
    return events


def _temporal_event(
    event: dict[str, Any],
    *,
    simulate_time: bool,
) -> dict[str, Any]:
    content = event.get("content") or {}
    sender = str(event.get("sender_id") or "")
    simulator = event.get("simulator") or {}
    return {
        "type": event.get("event_type"),
        "trigger_type": CHANNEL_TO_TRIGGER.get(
            str(event.get("channel") or ""),
            str(event.get("event_type") or ""),
        ),
        "channel": event.get("channel"),
        "business_id": event.get("business_id"),
        "customer_id": sender,
        "customer": {
            "id": sender,
            "name": simulator.get("customer_name"),
        },
        "message": {
            "text": content.get("text") if isinstance(content, dict) else "",
        },
        "comment_id": event.get("comment_id"),
        "post_id": event.get("post_id"),
        "skip_send": True,
        "simulate_outbound": True,
        "simulate_time": simulate_time,
        "simulator": True,
    }


async def _query_with_retry(temporal_id: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(10):
        try:
            return await query_workflow_state(temporal_id)
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    return {"status": "query_error", "error": str(last_error or "unknown query error")}


async def _report(progress: ProgressCallback | None, update: dict[str, Any]) -> None:
    """Progress reporting must never break a test run."""
    if progress is None:
        return
    try:
        await progress(update)
    except Exception:
        logger.debug("test progress update skipped", exc_info=True)


async def test_workflow_with_scenario(
    *,
    business_id: str,
    workflow_id: str,
    run_agent_pipeline: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run an isolated stateful Temporal scenario and snapshot each instance."""
    from admin.test_simulator_lab import run_synthetic_message

    workflow_row = await get_workflow(business_id, workflow_id)
    if not workflow_row:
        raise ValueError("Workflow not found")
    scenario = auto_select_scenario(workflow_row)
    events = generate_scenario_events(business_id, workflow_row, scenario)
    await _report(
        progress,
        {
            "stage": "running",
            "events_total": len(events),
            "events_processed": 0,
            "scenario": scenario,
            "workflow_name": workflow_row.get("name"),
        },
    )
    test_run_id = uuid.uuid4().hex[:16]
    per_event: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    matched_count = 0
    temporal_instances: set[str] = set()
    test_run_rows: set[str] = set()

    for index, event in enumerate(events):
        content = event.get("content") or {}
        text = str(content.get("text") or "") if isinstance(content, dict) else ""
        pipeline = await run_synthetic_message(
            business_id=business_id,
            text=text,
            channel=str(event.get("channel") or "web"),
            session_id=str(event.get("session_id") or ""),
            customer_name=str((event.get("simulator") or {}).get("customer_name") or "Test user"),
            sender_id=str(event.get("sender_id") or ""),
            comment_id=event.get("comment_id"),
            post_id=event.get("post_id"),
            process=run_agent_pipeline,
            skip_workflows=True,
        )
        temporal_event = _temporal_event(
            event,
            simulate_time=bool(scenario.get("simulate_time")),
        )
        temporal_event["type"] = workflow_row.get("trigger_type")
        temporal_event["trigger_type"] = workflow_row.get("trigger_type")
        routing: list[dict[str, Any]] = []
        state: dict[str, Any] | None = None
        error = pipeline.get("error")
        try:
            routing = await route_event_to_workflows_detailed(
                temporal_event,
                business_id,
                target_workflow_id=workflow_id,
                test_run_id=test_run_id,
            )
            if routing:
                matched_count += 1
                temporal_instances.update(
                    str(item["temporal_instance_id"]) for item in routing
                )
                test_run_rows.update(
                    str(item["run_id"])
                    for item in routing
                    if item.get("run_id")
                )
                state = await _query_with_retry(
                    str(routing[0]["temporal_instance_id"])
                )
                if state.get("status") in {"failed", "query_error"}:
                    error = state.get("error") or state.get("status")
        except Exception as exc:
            error = str(exc)
        outcome = (
            "error"
            if error
            else "no_match"
            if not routing
            else str((state or {}).get("status") or routing[0].get("action"))
        )
        item = {
            "event_index": index + 1,
            "event": temporal_event,
            "workflow_matched": bool(routing),
            "routing": routing,
            "state": state,
            "trace": pipeline.get("trace"),
            "visual": pipeline.get("visual"),
            "actions": (state or {}).get("step_results") or {},
            "outcome": outcome,
            "error": error,
            "skip_send": True,
        }
        per_event.append(item)
        if error:
            failures.append(item)
        await _report(
            progress,
            {
                "stage": "running",
                "events_total": len(events),
                "events_processed": len(per_event),
                "last_event": {
                    "event": index + 1,
                    "matched": bool(routing),
                    "workflow_status": (state or {}).get("status"),
                    "events_received": (state or {}).get("events_received"),
                    "completed_steps": (state or {}).get("completed_steps") or [],
                    "outcome": outcome,
                    "error": error,
                },
                "failures_so_far": len(failures),
            },
        )

    all_passed = not failures and matched_count > 0
    await _report(progress, {"stage": "cleanup", "events_processed": len(per_event)})
    for temporal_id in temporal_instances:
        try:
            await terminate_test_workflow(temporal_id)
        except Exception:
            # Completed workflows and unavailable cleanup are both safe here;
            # test instance IDs are isolated and never receive production events.
            pass
    for run_id in test_run_rows:
        try:
            await finish_run(
                business_id,
                run_id,
                status="success" if all_passed else "failed",
                error_message=None if all_passed else "Stateful scenario test failed",
                count_metrics=False,
            )
        except Exception:
            pass
    return {
        "test_run_id": test_run_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_row.get("name"),
        "scenario": scenario,
        "status": "passed" if all_passed else "failed",
        "all_passed": all_passed,
        "events_processed": len(per_event),
        "workflow_triggered": matched_count,
        "state_progression": [item.get("state") for item in per_event],
        "per_event_traces": per_event,
        "failures": failures,
        "skip_send": True,
    }
