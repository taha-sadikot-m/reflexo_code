"""Dry-run a workflow definition with no MCP or Temporal side effects."""

from __future__ import annotations

from typing import Any

from workflow_builder.models import StepType, WorkflowDefinition


def _generate_mock_event(trigger_type: str) -> dict[str, Any]:
    return {
        "type": trigger_type,
        "trigger_type": trigger_type,
        "channel": trigger_type,
        "message": {"text": "How much does the deluxe package cost?"},
        "customer": {"id": "cust-sim", "name": "Sim Customer", "phone": "+15555550100"},
        "business": {"name": "Demo Business"},
    }


def _resolve(template: Any, event: dict[str, Any], results: dict[str, Any]) -> Any:
    if not isinstance(template, str) or "{{" not in template:
        return template
    replacements = {
        "message.text": str((event.get("message") or {}).get("text") or ""),
        "customer.name": str((event.get("customer") or {}).get("name") or ""),
        "customer.id": str((event.get("customer") or {}).get("id") or ""),
        "customer.phone": str((event.get("customer") or {}).get("phone") or ""),
        "channel": str(event.get("channel") or event.get("type") or ""),
        "aggregate_count": "3",
        "now": "now",
        "today": "today",
    }
    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", value)
    for name, payload in results.items():
        if isinstance(payload, dict):
            for field, val in payload.items():
                out = out.replace("{{result." + name + "." + str(field) + "}}", str(val))
    return out


async def simulate_workflow(
    definition: WorkflowDefinition,
    mock_event: dict | None = None,
    business_id: str = "",
) -> dict[str, Any]:
    del business_id
    event = mock_event or _generate_mock_event(definition.trigger_type)
    trace: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    estimated_seconds = 0
    completed: set[str] = set()
    remaining = list(definition.steps)
    safety = 0
    while remaining and safety < 64:
        safety += 1
        progressed = False
        for step in list(remaining):
            if any(dep not in completed for dep in step.depends_on):
                continue
            progressed = True
            remaining.remove(step)
            entry: dict[str, Any] = {"name": step.name, "type": step.type.value}
            if step.type == StepType.CALL_TOOL:
                params = {
                    k: _resolve(v, event, results) for k, v in (step.params or {}).items()
                }
                entry["would_call"] = step.tool
                entry["params"] = params
                entry["status"] = "mocked"
                results[step.name] = {"ok": True, "mocked": True}
            elif step.type == StepType.AGGREGATE_EVENTS:
                window = int(step.window_seconds or 0)
                estimated_seconds += window
                entry["status"] = f"would wait for {step.threshold} events"
                entry["window_seconds"] = window
                results[step.name] = {
                    "aggregate_count": step.threshold,
                    "status": "threshold_reached",
                }
            elif step.type == StepType.DELAY:
                delay = int(step.delay_seconds or 3600)
                estimated_seconds += delay
                hours = delay / 3600
                entry["status"] = f"would sleep {hours:g}h"
            elif step.type == StepType.WAIT_FOR_EVENT:
                timeout = int(step.wait_timeout_seconds or 0)
                estimated_seconds += timeout
                entry["status"] = f"would wait for {step.wait_event_type}"
            elif step.type == StepType.CONDITION:
                entry["status"] = f"would evaluate {step.expression}"
                entry["branch"] = step.on_true_step
            elif step.type == StepType.PARALLEL:
                entry["status"] = f"would run in parallel: {', '.join(step.children)}"
            elif step.type == StepType.SEND_NOTIFICATION:
                entry["status"] = "would notify"
                entry["params"] = {
                    k: _resolve(v, event, results) for k, v in (step.params or {}).items()
                }
            else:
                entry["status"] = "skipped"
            trace.append(entry)
            completed.add(step.name)
        if not progressed:
            break

    response = _resolve(definition.response_template or "", event, results)
    return {
        "trigger": definition.trigger_type,
        "instance_key": definition.instance_key,
        "mock_event": event,
        "steps": trace,
        "final_response": response,
        "estimated_duration_seconds": estimated_seconds,
        "side_effects": False,
    }
