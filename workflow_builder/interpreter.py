"""Generic Temporal interpreter for any WorkflowDefinition."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from workflow_builder.activities import (
        clear_workflow_confirmation_activity,
        execute_tool_activity,
        finish_workflow_run_activity,
        record_workflow_step,
        request_workflow_confirmation_activity,
        send_notification_activity,
        start_scheduled_run_activity,
    )


def _nested(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


@workflow.defn(name="DynamicBusinessWorkflow")
class DynamicBusinessWorkflow:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.step_results: dict[str, dict[str, Any]] = {}
        self.definition: dict[str, Any] = {}
        self.business_id: str = ""
        self.run_id: str = ""
        self.status: str = "initializing"
        self.current_step: str | None = None
        self.failed_step: str | None = None
        self.error: str | None = None

    @workflow.signal
    async def on_event(self, event: dict[str, Any]) -> None:
        self.events.append(event or {})

    @workflow.query
    def get_state(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "events_received": len(self.events),
            "completed_steps": list(self.step_results.keys()),
            "current_step": self.current_step,
            "failed_step": self.failed_step,
            "error": self.error,
            "step_results": self.step_results,
            "latest_event": self.events[-1] if self.events else None,
            "event_types": [
                str(event.get("type") or event.get("trigger_type") or "")
                for event in self.events
            ],
        }

    @workflow.run
    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.definition = input_data.get("definition") or {}
        self.business_id = str(input_data.get("business_id") or "")
        self.run_id = str(input_data.get("run_id") or "")
        trigger = input_data.get("trigger_event") or {}
        self.events.append(trigger)
        self.status = "running"
        if (
            not self.run_id
            and str(trigger.get("type") or trigger.get("trigger_type")) == "schedule"
        ):
            self.run_id = await workflow.execute_activity(
                start_scheduled_run_activity,
                args=[
                    self.business_id,
                    str(self.definition.get("workflow_id") or ""),
                    trigger,
                    workflow.info().workflow_id,
                ],
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        if self.definition.get("requires_owner_confirmation"):
            simulated = bool(
                trigger.get("skip_send")
                or trigger.get("simulate_outbound")
                or trigger.get("simulator")
            )
            temporal_id = workflow.info().workflow_id
            initial_confirmations = self._confirmation_count()
            await workflow.execute_activity(
                request_workflow_confirmation_activity,
                args=[
                    self.business_id,
                    str(self.definition.get("workflow_id") or ""),
                    temporal_id,
                    str(
                        self.definition.get("description")
                        or self.definition.get("name")
                        or "Workflow action"
                    ),
                    simulated,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not simulated:
                try:
                    await workflow.wait_condition(
                        lambda: self._confirmation_count() > initial_confirmations,
                        timeout=timedelta(minutes=15),
                    )
                    approval = self._latest_confirmation()
                    if not approval:
                        self.status = "cancelled"
                        await workflow.execute_activity(
                            finish_workflow_run_activity,
                            args=[
                                self.business_id,
                                self.run_id,
                                "cancelled",
                                "Owner declined workflow execution",
                            ],
                            start_to_close_timeout=timedelta(seconds=15),
                        )
                        return {
                            "status": "cancelled",
                            "reason": "Owner declined workflow execution",
                        }
                except TimeoutError:
                    await workflow.execute_activity(
                        clear_workflow_confirmation_activity,
                        args=[self.business_id, temporal_id],
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    # Fail closed: an expired/lost approval must never execute
                    # customer-facing or financial actions.
                    self.status = "cancelled"
                    await workflow.execute_activity(
                        finish_workflow_run_activity,
                        args=[
                            self.business_id,
                            self.run_id,
                            "cancelled",
                            "Owner confirmation timed out",
                        ],
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    return {
                        "status": "cancelled",
                        "reason": "Owner confirmation timed out",
                    }

        steps = list(self.definition.get("steps") or [])
        completed: set[str] = set()
        skipped: set[str] = set()

        while len(completed) + len(skipped) < len(steps):
            ready = [
                step
                for step in steps
                if step.get("name") not in completed
                and step.get("name") not in skipped
                and all(dep in completed or dep in skipped for dep in (step.get("depends_on") or []))
            ]
            if not ready:
                break
            for step in ready:
                try:
                    self.current_step = str(step.get("name") or "step")
                    await self._execute_step(step, trigger)
                    completed.add(str(step.get("name")))
                    branch = (self.step_results.get(str(step.get("name"))) or {}).get("skip_steps")
                    if isinstance(branch, list):
                        skipped.update(str(n) for n in branch)
                except Exception as exc:
                    self.status = "failed"
                    self.failed_step = str(step.get("name") or "step")
                    self.error = str(exc)
                    self.current_step = None
                    await workflow.execute_activity(
                        record_workflow_step,
                        args=[
                            self.business_id,
                            self.run_id,
                            str(step.get("name")),
                            str(step.get("type")),
                            "failed",
                            {"error": str(exc)},
                            0,
                        ],
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    return {
                        "status": "failed",
                        "failed_step": step.get("name"),
                        "error": str(exc),
                        "completed_steps": list(completed),
                    }
        self.status = "success"
        self.current_step = None
        await workflow.execute_activity(
            finish_workflow_run_activity,
            args=[self.business_id, self.run_id, "success", None],
            start_to_close_timeout=timedelta(seconds=15),
        )
        return {"status": "success", "completed_steps": list(completed), "step_results": self.step_results}

    async def _execute_step(self, step: dict[str, Any], trigger: dict[str, Any]) -> None:
        started = workflow.time()
        name = str(step.get("name") or "step")
        kind = str(step.get("type") or "")
        result: dict[str, Any] = {}

        if kind == "call_tool":
            params = self._resolve_params(step.get("params") or {}, trigger)
            if trigger.get("skip_send") or trigger.get("simulate_outbound") or trigger.get("simulator"):
                params["simulate_outbound"] = True
            result = await workflow.execute_activity(
                execute_tool_activity,
                args=[str(step.get("tool") or ""), params, self.business_id],
                start_to_close_timeout=timedelta(seconds=int(step.get("timeout_seconds") or 30)),
                retry_policy=RetryPolicy(
                    maximum_attempts=int(step.get("retry_count") or 2) + 1,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=30),
                ),
            )
        elif kind == "aggregate_events":
            threshold = int(step.get("threshold") or 1)
            window = int(step.get("window_seconds") or 86400)
            filt = step.get("event_filter") or {}
            try:
                await workflow.wait_condition(
                    lambda: self._count_matching(filt) >= threshold,
                    timeout=timedelta(seconds=window),
                )
                result = {
                    "aggregate_count": self._count_matching(filt),
                    "status": "threshold_reached",
                }
            except TimeoutError:
                result = {
                    "aggregate_count": self._count_matching(filt),
                    "status": "window_expired",
                }
        elif kind == "delay":
            delay = int(step.get("delay_seconds") or 0)
            if delay <= 0:
                delay = 1
            if trigger.get("simulate_time"):
                result = {
                    "status": "delay_simulated",
                    "delay_seconds": delay,
                }
            else:
                await workflow.sleep(delay)
                result = {"status": "delay_completed", "delay_seconds": delay}
        elif kind == "wait_for_event":
            wait_type = str(step.get("wait_event_type") or "")
            initial = self._count_type(wait_type)
            timeout = step.get("wait_timeout_seconds")
            if trigger.get("simulate_time"):
                result = {
                    "status": "event_simulated",
                    "event": {"type": wait_type, "simulator": True},
                }
            else:
                try:
                    await workflow.wait_condition(
                        lambda: self._count_type(wait_type) > initial,
                        timeout=timedelta(seconds=int(timeout)) if timeout else None,
                    )
                    matching = [e for e in self.events if e.get("type") == wait_type]
                    result = {"status": "event_received", "event": matching[-1] if matching else {}}
                except TimeoutError:
                    result = {"status": "timeout"}
        elif kind == "condition":
            truthy = self._evaluate(str(step.get("expression") or ""), trigger)
            result = {
                "evaluated": truthy,
                "branch": step.get("on_true_step") if truthy else step.get("on_false_step"),
            }
            skip = []
            chosen = result["branch"]
            other = step.get("on_false_step") if truthy else step.get("on_true_step")
            if other and other != chosen:
                skip.append(other)
            if skip:
                result["skip_steps"] = skip
        elif kind == "parallel":
            children = [
                s
                for s in (self.definition.get("steps") or [])
                if s.get("name") in (step.get("children") or [])
            ]
            await asyncio.gather(*[self._execute_step(child, trigger) for child in children])
            result = {"status": "all_children_completed"}
        elif kind == "send_notification":
            params = self._resolve_params(step.get("params") or {}, trigger)
            result = await workflow.execute_activity(
                send_notification_activity,
                args=[
                    self.business_id,
                    str(params.get("channel") or "whatsapp"),
                    str(params.get("to") or params.get("phone") or ""),
                    str(params.get("message") or params.get("text") or ""),
                    bool(
                        trigger.get("skip_send")
                        or trigger.get("simulate_outbound")
                        or trigger.get("simulator")
                    ),
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        else:
            result = {"status": "skipped", "reason": f"unknown type {kind}"}

        self.step_results[name] = result if isinstance(result, dict) else {"result": result}
        elapsed = int((workflow.time() - started) * 1000)
        await workflow.execute_activity(
            record_workflow_step,
            args=[self.business_id, self.run_id, name, kind, "success", self.step_results[name], elapsed],
            start_to_close_timeout=timedelta(seconds=15),
        )

    def _count_matching(self, event_filter: dict[str, Any]) -> int:
        wanted = str(event_filter.get("type") or event_filter.get("event_type") or "")
        count = 0
        for event in self.events:
            etype = str(event.get("type") or event.get("trigger_type") or "")
            if wanted and etype != wanted:
                continue
            count += 1
        return count

    def _count_type(self, event_type: str) -> int:
        return sum(1 for e in self.events if e.get("type") == event_type)

    def _confirmation_count(self) -> int:
        return self._count_type("workflow_confirmation")

    def _latest_confirmation(self) -> bool:
        matches = [
            event
            for event in self.events
            if event.get("type") == "workflow_confirmation"
        ]
        return bool(matches and matches[-1].get("approved"))

    def _resolve_params(self, params: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
        return {k: self._resolve_template(v, trigger) for k, v in params.items()}

    def _resolve_template(self, value: Any, trigger: dict[str, Any]) -> Any:
        if not isinstance(value, str) or "{{" not in value:
            return value

        def replacer(match: re.Match[str]) -> str:
            var = match.group(1).strip()
            if var == "aggregate_count":
                last = next(reversed(self.step_results.values()), {})
                return str(last.get("aggregate_count") or 0)
            if var.startswith("result."):
                parts = var.split(".")
                if len(parts) >= 3:
                    payload = self.step_results.get(parts[1]) or {}
                    return str(payload.get(parts[2], ""))
            if var.startswith("event."):
                latest = self.events[-1] if self.events else {}
                found = _nested(latest, var[len("event.") :])
                return "" if found is None else str(found)
            found = _nested(trigger, var)
            if found is None:
                found = _nested({"trigger_event": trigger, **trigger}, var)
            return "" if found is None else str(found)

        return re.sub(r"\{\{([^}]+)\}\}", replacer, value)

    def _evaluate(self, expression: str, trigger: dict[str, Any]) -> bool:
        text = self._resolve_template(expression, trigger)
        env = {
            "aggregate_count": int(
                (next(reversed(self.step_results.values()), {}) or {}).get("aggregate_count") or 0
            ),
            "true": True,
            "false": False,
        }
        try:
            return bool(eval(text, {"__builtins__": {}}, env))  # noqa: S307
        except Exception:
            return bool(text) and text.lower() not in {"false", "0", "none", ""}
