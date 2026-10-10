"""Deploy definitions and route inbound events to Temporal instances."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from workflow_builder.models import CHANNEL_TO_TRIGGER, WorkflowDefinition
from workflow_builder.store import (
    finish_run,
    get_workflow,
    insert_run,
    insert_workflow,
    mark_deployed,
    mark_prepared,
    matching_active,
)

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")


def _settings() -> tuple[str, str, str]:
    try:
        from agent.settings import get_agent_settings

        s = get_agent_settings()
        return s.temporal_address, s.temporal_namespace, s.temporal_task_queue
    except Exception:
        return "localhost:7233", "default", "reflexo-workflows"


CONNECT_TIMEOUT_SECONDS = 5.0
# Temporal keeps reporting pollers for minutes after a worker exits, so only
# recently seen pollers prove a worker is actually available.
POLLER_FRESH_SECONDS = 60.0
WORKER_HINT = "Start the workflow worker: python -m workflow_builder.worker"


async def _client():
    from temporalio.client import Client

    address, namespace, _queue = _settings()
    # Without a timeout an unreachable Temporal host hangs the caller's request.
    return await asyncio.wait_for(
        Client.connect(address, namespace=namespace),
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


async def check_temporal_ready() -> dict[str, Any]:
    """Report whether Temporal is reachable and a worker polls the task queue."""
    address, _namespace, task_queue = _settings()
    try:
        client = await _client()
    except (TimeoutError, asyncio.TimeoutError):
        return {
            "ready": False,
            "reason": f"Temporal did not respond at {address}",
            "hint": "Start Temporal (docker compose up temporal-server)",
        }
    except Exception as exc:
        return {
            "ready": False,
            "reason": f"Temporal unavailable at {address}: {exc}",
            "hint": "Start Temporal (docker compose up temporal-server)",
        }
    try:
        info = await asyncio.wait_for(
            client.service_client.workflow_service.describe_task_queue(
                _describe_task_queue_request(task_queue)
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        pollers = [
            poller
            for poller in list(getattr(info, "pollers", []) or [])
            if _poller_is_fresh(poller)
        ]
    except Exception as exc:
        logger.debug("task queue describe failed", exc_info=True)
        return {
            "ready": False,
            "reason": f"Could not inspect task queue {task_queue}: {exc}",
            "hint": WORKER_HINT,
        }
    if not pollers:
        return {
            "ready": False,
            "reason": f"No worker is polling task queue '{task_queue}'",
            "hint": WORKER_HINT,
        }
    return {"ready": True, "task_queue": task_queue, "pollers": len(pollers)}


def _poller_is_fresh(poller: Any) -> bool:
    last_access = getattr(poller, "last_access_time", None)
    if last_access is None:
        return True
    try:
        seen = last_access.ToDatetime().replace(tzinfo=timezone.utc)
    except AttributeError:
        return True
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    return age <= POLLER_FRESH_SECONDS


def _describe_task_queue_request(task_queue: str):
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

    _address, namespace, _queue = _settings()
    return DescribeTaskQueueRequest(
        namespace=namespace,
        task_queue=TaskQueue(name=task_queue),
        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
    )


def _uuid_or_none(value: Any) -> str | None:
    """Instance keys may be platform-native ids; UUID columns must not choke on them."""
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def resolve_instance_key(template: str | None, event: dict[str, Any]) -> str:
    if not template:
        return "singleton"

    def replacer(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        cur: Any = event
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return "unknown"
        return str(cur or "unknown")

    return _VAR_RE.sub(replacer, template)


def event_matches_workflow(workflow_row: dict[str, Any], event: dict[str, Any]) -> bool:
    """Apply deterministic trigger filters before starting/signalling Temporal."""
    config = workflow_row.get("trigger_config") or {}
    if not isinstance(config, dict):
        return True
    message = event.get("message") or {}
    text = str(
        message.get("text")
        if isinstance(message, dict)
        else event.get("text") or ""
    ).casefold()
    keyword = config.get("keyword")
    if keyword and str(keyword).casefold() not in text:
        return False
    keywords = config.get("keywords")
    if isinstance(keywords, list) and keywords:
        mode = str(config.get("keyword_mode") or "any").lower()
        checks = [str(item).casefold() in text for item in keywords if str(item).strip()]
        if checks:
            matched = all(checks) if mode == "all" else any(checks)
            if not matched:
                return False
    configured_post = config.get("post_id")
    if configured_post and str(configured_post).lower() != "any":
        if str(event.get("post_id") or "") != str(configured_post):
            return False
    conditions = workflow_row.get("conditions") or []
    if isinstance(conditions, list):
        for condition in conditions:
            if isinstance(condition, dict) and not _condition_matches(condition, event):
                return False
    return True


def _event_field(event: dict[str, Any], field: str) -> Any:
    aliases = {
        "message_text": "message.text",
        "text": "message.text",
        "customer_id": "customer.id",
    }
    path = aliases.get(field, field)
    current: Any = event
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _condition_matches(condition: dict[str, Any], event: dict[str, Any]) -> bool:
    """Evaluate the deterministic condition subset supported by Temporal routing."""
    field = str(condition.get("field") or "").strip()
    operator = str(condition.get("operator") or "equals").strip().lower()
    expected = condition.get("value")
    actual = _event_field(event, field)

    if operator in {"exists", "not_exists"}:
        exists = actual is not None and actual != ""
        return exists if operator == "exists" else not exists
    if operator in {"in", "not_in"}:
        values = expected if isinstance(expected, list) else [expected]
        matched = actual in values or str(actual).casefold() in {
            str(item).casefold() for item in values
        }
        return matched if operator == "in" else not matched

    actual_text = str(actual or "")
    expected_text = str(expected or "")
    left, right = actual_text.casefold(), expected_text.casefold()
    if operator in {"equals", "eq", "=="}:
        return left == right
    if operator in {"not_equals", "ne", "!="}:
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "starts_with":
        return left.startswith(right)
    if operator == "ends_with":
        return left.endswith(right)
    # Unknown operators must fail closed; otherwise a typo consumes all traffic.
    return False


def _shadow_allowed(workflow_row: dict[str, Any], event: dict[str, Any]) -> bool:
    if not workflow_row.get("shadow_mode"):
        return True
    raw = workflow_row.get("shadow_allowlist") or []
    allowlist = {
        str(value).strip().casefold()
        for value in raw
        if str(value).strip()
    } if isinstance(raw, list) else set()
    customer = event.get("customer") or {}
    candidates = {
        str(event.get("external_sender_id") or "").casefold(),
        str(event.get("sender_id") or "").casefold(),
        str(event.get("customer_id") or "").casefold(),
        str(customer.get("id") if isinstance(customer, dict) else "").casefold(),
    }
    candidates.discard("")
    return bool(allowlist.intersection(candidates))


def _definition_dict(wf: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_id": str(wf.get("id") or ""),
        "name": wf.get("name"),
        "description": wf.get("description") or "",
        "trigger_type": wf.get("trigger_type"),
        "trigger_config": wf.get("trigger_config") or {},
        "instance_key": wf.get("instance_key"),
        "conditions": wf.get("conditions") or [],
        "steps": wf.get("steps") or [],
        "response_template": wf.get("response_template") or "",
        "max_duration_seconds": wf.get("max_duration_seconds") or 604800,
        "continue_as_new": wf.get("continue_as_new") or False,
        "requires_owner_confirmation": bool(
            wf.get("requires_owner_confirmation")
        ),
        "max_executions_per_day": int(
            wf.get("max_executions_per_day") or 1000
        ),
    }


def temporal_instance_id(
    workflow_row: dict[str, Any],
    event: dict[str, Any],
    *,
    test_run_id: str | None = None,
) -> str:
    instance_key = resolve_instance_key(workflow_row.get("instance_key"), event)
    base_id = workflow_row.get("temporal_workflow_id") or f"reflexo_wf_{workflow_row['id']}"
    if test_run_id:
        return f"{base_id}:test:{test_run_id}:{instance_key}"
    return f"{base_id}:{instance_key}"


async def query_workflow_state(temporal_id: str) -> dict[str, Any]:
    """Query a DynamicBusinessWorkflow instance without hiding Temporal errors."""
    from workflow_builder.interpreter import DynamicBusinessWorkflow

    client = await _client()
    handle = client.get_workflow_handle(temporal_id)
    state = await handle.query(DynamicBusinessWorkflow.get_state)
    return state if isinstance(state, dict) else {"state": state}


async def terminate_test_workflow(temporal_id: str) -> None:
    """Best-effort cleanup for an isolated simulator workflow instance."""
    client = await _client()
    handle = client.get_workflow_handle(temporal_id)
    await handle.terminate(reason="Stateful workflow scenario test completed")


async def deploy_workflow(
    business_id: str,
    definition: WorkflowDefinition,
    *,
    nl_description: str = "",
    simulation: dict[str, Any] | None = None,
    existing_id: str | None = None,
) -> dict[str, Any]:
    """Persist a draft; activate only when a stateful scenario report passed."""
    all_passed = bool((simulation or {}).get("all_passed"))
    if existing_id:
        marker = mark_deployed if all_passed else mark_prepared
        kwargs: dict[str, Any] = {
            "temporal_workflow_id": f"reflexo_wf_{existing_id}",
            "metadata": {"simulation_result": simulation} if simulation else None,
        }
        if all_passed:
            kwargs["is_tested"] = True
        row = await marker(business_id, existing_id, **kwargs)
        if not row:
            raise ValueError("Workflow not found")
        return row
    row = await insert_workflow(
        business_id,
        definition,
        nl_description=nl_description or definition.nl_description,
        is_tested=all_passed,
        is_active=all_passed,
        metadata={"simulation_result": simulation} if simulation else {},
    )
    temporal_id = f"reflexo_wf_{row['id']}"
    if all_passed:
        updated = await mark_deployed(
            business_id,
            str(row["id"]),
            temporal_workflow_id=temporal_id,
            is_tested=True,
            metadata={"test_results": simulation},
        )
    else:
        updated = await mark_prepared(
            business_id,
            str(row["id"]),
            temporal_workflow_id=temporal_id,
            metadata={"activation_required": "stateful_scenario_test"},
        )
    return updated or {**row, "temporal_workflow_id": temporal_id}


def temporal_schedule_id(workflow_id: str) -> str:
    return f"reflexo_schedule_{workflow_id}"


async def ensure_temporal_schedule(
    business_id: str,
    workflow_row: dict[str, Any],
) -> str:
    """Create or replace the Temporal Schedule for a tested schedule workflow."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
    )

    from workflow_builder.interpreter import DynamicBusinessWorkflow

    if str(workflow_row.get("trigger_type")) != "schedule":
        raise ValueError("Workflow trigger_type is not schedule")
    config = workflow_row.get("trigger_config") or {}
    cron = str(config.get("cron") or config.get("cron_expression") or "").strip()
    interval_seconds = int(config.get("interval_seconds") or 0)
    if not cron and interval_seconds <= 0:
        raise ValueError("Scheduled workflow requires cron or interval_seconds")
    timezone_name = str(config.get("timezone") or "Asia/Kolkata")
    spec = (
        ScheduleSpec(
            cron_expressions=[cron],
            time_zone_name=timezone_name,
        )
        if cron
        else ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(seconds=interval_seconds))
            ],
            time_zone_name=timezone_name,
        )
    )
    _, _, task_queue = _settings()
    workflow_id = str(workflow_row["id"])
    schedule_id = temporal_schedule_id(workflow_id)
    client = await _client()
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.delete()
    except Exception:
        pass
    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                DynamicBusinessWorkflow.run,
                {
                    "definition": _definition_dict(workflow_row),
                    "trigger_event": {
                        "type": "schedule",
                        "trigger_type": "schedule",
                        "business_id": business_id,
                        "scheduled": True,
                    },
                    "business_id": business_id,
                    "run_id": "",
                },
                id=f"reflexo_scheduled_workflow_{workflow_id}",
                task_queue=task_queue,
                execution_timeout=timedelta(
                    seconds=int(
                        workflow_row.get("max_duration_seconds") or 604800
                    )
                ),
            ),
            spec=spec,
        ),
    )
    return schedule_id


async def delete_temporal_schedule(workflow_id: str) -> None:
    from temporalio.service import RPCError, RPCStatusCode

    client = await _client()
    try:
        await client.get_schedule_handle(
            temporal_schedule_id(workflow_id)
        ).delete()
    except RPCError as exc:
        if getattr(exc, "status", None) == RPCStatusCode.NOT_FOUND:
            return
        raise


async def route_event_to_workflows_detailed(
    event: dict[str, Any],
    business_id: str,
    *,
    target_workflow_id: str | None = None,
    test_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Start/signal workflows and return instance IDs/actions for testing and traces."""
    trigger = str(event.get("trigger_type") or event.get("type") or "")
    trigger = CHANNEL_TO_TRIGGER.get(trigger, trigger)
    if not business_id or not trigger:
        return []
    if (
        event.get("smart_number_mode")
        and str(event.get("contact_type") or "").lower() == "personal"
    ):
        logger.info(
            "WF_ROUTE skipped personal Smart Number contact business=%s",
            business_id,
        )
        return []
    try:
        if target_workflow_id:
            target = await get_workflow(business_id, target_workflow_id)
            workflows = [target] if target and str(target.get("trigger_type")) == trigger else []
        else:
            workflows = await matching_active(business_id, trigger)
    except Exception:
        logger.warning("WF_ROUTE list failed business=%s", business_id, exc_info=True)
        return []
    workflows = [wf for wf in workflows if wf and event_matches_workflow(wf, event)]
    if not test_run_id:
        from workflow_builder.store import runs_last_24h

        eligible: list[dict[str, Any]] = []
        for workflow_row in workflows:
            cap = int(workflow_row.get("max_executions_per_day") or 1000)
            # Most workflows have lifetime totals below the daily cap, so avoid
            # an extra query on that hot path.
            if int(workflow_row.get("total_runs") or 0) < cap:
                eligible.append(workflow_row)
                continue
            recent = await runs_last_24h(business_id, str(workflow_row["id"]))
            if len(recent) >= cap:
                logger.warning(
                    "WF_ROUTE daily cap business=%s workflow=%s cap=%s",
                    business_id,
                    workflow_row.get("id"),
                    cap,
                )
                continue
            eligible.append(workflow_row)
        workflows = eligible
    if not workflows:
        return []

    shadow_results: list[dict[str, Any]] = []
    if not test_run_id:
        executable: list[dict[str, Any]] = []
        for workflow_row in workflows:
            if _shadow_allowed(workflow_row, event):
                executable.append(workflow_row)
                continue
            temporal_id = temporal_instance_id(workflow_row, event)
            shadow_event = {**event, "shadow_mode": True}
            run = await insert_run(
                business_id,
                str(workflow_row["id"]),
                trigger_event=shadow_event,
                conversation_id=_uuid_or_none(event.get("conversation_id")),
                customer_id=_uuid_or_none(
                    (event.get("customer") or {}).get("id")
                    or event.get("customer_id")
                ),
                temporal_instance_id=temporal_id,
                status="success",
                is_shadow=True,
                count_metrics=False,
            )
            await finish_run(
                business_id,
                str(run.get("id") or ""),
                status="success",
                count_metrics=False,
            )
            shadow_results.append(
                {
                    "workflow_id": str(workflow_row["id"]),
                    "workflow_name": workflow_row.get("name"),
                    "temporal_instance_id": temporal_id,
                    "action": "shadow_logged",
                    "run_id": str(run.get("id") or ""),
                }
            )
        workflows = executable
    if not workflows:
        return shadow_results

    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    from workflow_builder.interpreter import DynamicBusinessWorkflow

    _, _, task_queue = _settings()
    try:
        client = await _client()
    except Exception as exc:
        logger.warning("WF_ROUTE temporal connect failed", exc_info=True)
        if test_run_id:
            raise RuntimeError(f"Temporal unavailable: {exc}") from exc
        return []

    results: list[dict[str, Any]] = list(shadow_results)
    for wf in workflows:
        temporal_id = temporal_instance_id(wf, event, test_run_id=test_run_id)
        try:
            handle = client.get_workflow_handle(temporal_id)
            desc = await handle.describe()
            status = getattr(desc, "status", None)
            running = status in {
                WorkflowExecutionStatus.RUNNING,
                WorkflowExecutionStatus.CONTINUED_AS_NEW,
            } if status is not None else True
            if running:
                await handle.signal(DynamicBusinessWorkflow.on_event, event)
                logger.info("WF_SIGNAL id=%s", temporal_id)
                results.append(
                    {
                        "workflow_id": str(wf["id"]),
                        "workflow_name": wf.get("name"),
                        "temporal_instance_id": temporal_id,
                        "action": "signaled",
                    }
                )
                continue
        except RPCError as exc:
            if getattr(exc, "status", None) not in {RPCStatusCode.NOT_FOUND, None}:
                logger.warning("WF_SIGNAL fail id=%s", temporal_id, exc_info=True)
        except Exception:
            logger.debug("WF_SIGNAL miss id=%s", temporal_id, exc_info=True)

        try:
            run = await insert_run(
                business_id,
                str(wf["id"]),
                trigger_event=event,
                conversation_id=_uuid_or_none(event.get("conversation_id")),
                customer_id=_uuid_or_none(
                    (event.get("customer") or {}).get("id") or event.get("customer_id")
                ),
                temporal_instance_id=temporal_id,
                status="running",
                count_metrics=not bool(test_run_id),
            )
            await client.start_workflow(
                DynamicBusinessWorkflow.run,
                {
                    "definition": _definition_dict(wf),
                    "trigger_event": event,
                    "business_id": business_id,
                    "run_id": str(run.get("id") or ""),
                },
                id=temporal_id,
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=int(wf.get("max_duration_seconds") or 604800)),
            )
            logger.info("WF_START id=%s", temporal_id)
            results.append(
                {
                    "workflow_id": str(wf["id"]),
                    "workflow_name": wf.get("name"),
                    "temporal_instance_id": temporal_id,
                    "action": "started",
                    "run_id": str(run.get("id") or ""),
                }
            )
        except Exception as exc:
            logger.warning("WF_START fail id=%s", temporal_id, exc_info=True)
            if test_run_id:
                raise RuntimeError(f"Temporal start failed for {temporal_id}: {exc}") from exc
    return results


async def route_event_to_workflows(event: dict[str, Any], business_id: str) -> bool:
    """Return True only when Temporal consumes the turn.

    Shadow-only matches are logged but deliberately fall through to the normal
    agent, so non-allowlisted customers are not left without a response.
    """
    results = await route_event_to_workflows_detailed(event, business_id)
    return any(result.get("action") != "shadow_logged" for result in results)
