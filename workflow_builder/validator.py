"""Pre-deploy checks for AI-built workflow definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from workflow_builder.models import (
    TEMPLATE_PREFIXES,
    VALID_TRIGGERS,
    StepType,
    WorkflowDefinition,
)


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")


async def validate_workflow(
    definition: WorkflowDefinition,
    available_tools: list[dict],
    business_id: str = "",
) -> ValidationResult:
    del business_id
    result = ValidationResult()
    tool_names = {t["name"] for t in available_tools}
    step_names = {s.name for s in definition.steps}

    if definition.trigger_type not in VALID_TRIGGERS:
        result.errors.append(f"Invalid trigger_type '{definition.trigger_type}'")
    if not definition.steps:
        result.errors.append("Workflow has no steps")

    for step in definition.steps:
        if step.type == StepType.CALL_TOOL:
            if not step.tool:
                result.errors.append(f"Step '{step.name}' is CALL_TOOL but has no tool specified")
            elif step.tool not in tool_names:
                result.errors.append(
                    f"Step '{step.name}' references unknown tool '{step.tool}'. "
                    f"Available: {', '.join(sorted(tool_names)[:40])}"
                )
        if step.type == StepType.AGGREGATE_EVENTS:
            if not step.threshold or step.threshold < 1:
                result.errors.append(f"Step '{step.name}' is AGGREGATE_EVENTS but has no threshold")
            if not step.window_seconds:
                result.errors.append(
                    f"Step '{step.name}' is AGGREGATE_EVENTS but has no window_seconds"
                )
            if step.window_seconds and step.window_seconds > 30 * 86400:
                result.warnings.append(
                    f"Step '{step.name}' has {step.window_seconds}s aggregation window (>30 days)"
                )
            if not step.event_filter:
                result.warnings.append(f"Step '{step.name}' has no event_filter — will match all events")
        if step.type == StepType.DELAY:
            if not step.delay_seconds and not step.delay_until:
                result.errors.append(
                    f"Step '{step.name}' is DELAY but has no delay_seconds or delay_until"
                )
        if step.type == StepType.WAIT_FOR_EVENT:
            if not step.wait_event_type:
                result.errors.append(
                    f"Step '{step.name}' is WAIT_FOR_EVENT but has no wait_event_type"
                )
        if step.type == StepType.CONDITION:
            if not step.expression:
                result.errors.append(f"Step '{step.name}' is CONDITION but has no expression")
            if step.on_true_step and step.on_true_step not in step_names:
                result.errors.append(
                    f"Step '{step.name}' on_true_step '{step.on_true_step}' not found"
                )
            if step.on_false_step and step.on_false_step not in step_names:
                result.errors.append(
                    f"Step '{step.name}' on_false_step '{step.on_false_step}' not found"
                )
        if step.type == StepType.PARALLEL:
            for child in step.children:
                if child not in step_names:
                    result.errors.append(f"Step '{step.name}' parallel child '{child}' not found")
        for dep in step.depends_on:
            if dep not in step_names:
                result.errors.append(f"Step '{step.name}' depends_on '{dep}' which doesn't exist")
        if step.type == StepType.CALL_TOOL and step.timeout_seconds > 300:
            result.warnings.append(f"Step '{step.name}' has {step.timeout_seconds}s timeout (>5 min)")
        if step.retry_count > 5:
            result.warnings.append(f"Step '{step.name}' has {step.retry_count} retries (>5)")
        _check_templates(step, result)

    if _has_cycle({s.name: set(s.depends_on) for s in definition.steps}):
        result.errors.append("Step dependency graph contains a cycle")

    has_aggregation = any(s.type == StepType.AGGREGATE_EVENTS for s in definition.steps)
    if has_aggregation and not definition.instance_key:
        result.errors.append(
            "Workflow uses AGGREGATE_EVENTS but has no instance_key. "
            "Set instance_key (e.g., '{{customer.id}}') to scope event aggregation."
        )
    if definition.max_duration_seconds > 90 * 86400:
        result.warnings.append(
            f"Workflow max_duration is {definition.max_duration_seconds}s (>90 days)"
        )
    may_run_outside_customer_window = (
        definition.trigger_type == "schedule"
        or any(
            step.type in {StepType.DELAY, StepType.WAIT_FOR_EVENT}
            for step in definition.steps
        )
    )
    window_limited_whatsapp_tools = {
        "send_whatsapp_message",
        "send_whatsapp_interactive",
        "send_whatsapp_media",
        "send_whatsapp_location",
        "send_whatsapp_catalog",
        "send_whatsapp_flow",
    }
    if may_run_outside_customer_window:
        for step in definition.steps:
            if (
                step.type == StepType.CALL_TOOL
                and step.tool in window_limited_whatsapp_tools
            ):
                result.errors.append(
                    f"Step '{step.name}' may run outside WhatsApp's 24-hour "
                    "window; use send_whatsapp_template or "
                    "send_whatsapp_carousel"
                )

    result.is_valid = len(result.errors) == 0
    return result


def _check_templates(step, result: ValidationResult) -> None:
    blobs = [str(v) for v in (step.params or {}).values()]
    if step.delay_until:
        blobs.append(step.delay_until)
    if step.expression:
        blobs.append(step.expression)
    for blob in blobs:
        for match in _VAR_RE.finditer(blob):
            var = match.group(1).strip()
            if not any(var.startswith(p) or var == p.rstrip(".") for p in TEMPLATE_PREFIXES):
                result.warnings.append(
                    f"Step '{step.name}' uses unknown variable '{{{{{var}}}}}'"
                )


def _has_cycle(dep_graph: dict[str, set[str]]) -> bool:
    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        stack.add(node)
        for dep in dep_graph.get(node, set()):
            if dfs(dep):
                return True
        stack.remove(node)
        visited.add(node)
        return False

    return any(dfs(node) for node in dep_graph)
