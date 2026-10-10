"""Owner-readable wrapper around stateful Temporal scenario testing."""

from __future__ import annotations

from typing import Any

from workflow_builder.store import save_test_results
from workflow_builder.testing import ProgressCallback, test_workflow_with_scenario


def format_for_owner(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert technical traces into concise, truthful pass/fail results."""
    scenario = raw.get("scenario") or {}
    processed = int(raw.get("events_processed") or 0)
    triggered = int(raw.get("workflow_triggered") or 0)
    failures = raw.get("failures") or []
    all_passed = bool(raw.get("all_passed"))
    lines = [
        f"Tested {processed} synthetic events using the "
        f"{scenario.get('description') or scenario.get('type') or 'automatic'} scenario.",
        f"The workflow matched {triggered} event{'s' if triggered != 1 else ''}.",
        "All outbound messages were simulated; nothing was sent to customers.",
    ]
    if all_passed:
        lines.append("No pipeline or Temporal state errors were found.")
    else:
        lines.append(
            f"{len(failures)} event{'s' if len(failures) != 1 else ''} failed. "
            "Activation remains blocked."
        )
    state_checks = []
    for item in raw.get("per_event_traces") or []:
        state = item.get("state") or {}
        state_checks.append(
            {
                "event": item.get("event_index"),
                "matched": bool(item.get("workflow_matched")),
                "workflow_status": state.get("status"),
                "events_received": state.get("events_received"),
                "completed_steps": state.get("completed_steps") or [],
                "outcome": item.get("outcome"),
            }
        )
    return {
        "all_passed": all_passed,
        "status": "passed" if all_passed else "failed",
        "title": "Workflow test passed" if all_passed else "Workflow test needs attention",
        "summary": lines,
        "scenario": scenario,
        "events_processed": processed,
        "workflow_triggered": triggered,
        "edge_cases_tested": scenario.get("edge_cases") or [],
        "state_checks": state_checks,
        "failures": [
            {
                "event": item.get("event_index"),
                "error": item.get("error"),
                "outcome": item.get("outcome"),
            }
            for item in failures
        ],
        "test_run_id": raw.get("test_run_id"),
        "skip_send": True,
    }


async def run_owner_facing_test(
    *,
    business_id: str,
    workflow_id: str,
    run_agent_pipeline: bool = True,
    include_technical: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    raw = await test_workflow_with_scenario(
        business_id=business_id,
        workflow_id=workflow_id,
        run_agent_pipeline=run_agent_pipeline,
        progress=progress,
    )
    owner_results = format_for_owner(raw)
    await save_test_results(business_id, workflow_id, owner_results)
    if include_technical:
        return {**owner_results, "technical": raw}
    return owner_results
