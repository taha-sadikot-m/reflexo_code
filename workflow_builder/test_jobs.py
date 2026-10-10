"""Background stateful test jobs with owner-visible progress.

The stateful scenario test runs many synthetic events through Temporal, which
takes far longer than a browser request should wait. Jobs run detached and
report progress on `ai_workflows.metadata.test_progress` so both the admin
panel and the owner Studio can poll the same state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from workflow_builder.store import load_test_progress, save_test_progress

logger = logging.getLogger(__name__)

ACTIVE_STAGES = {"queued", "checking_temporal", "running", "cleanup"}
# Progress is written every few seconds, so a longer silence means the process
# running the job died (an API restart, say) and must not block new tests.
STALE_AFTER_SECONDS = 180

# Detached tasks are garbage collected while pending unless referenced.
_TASKS: set[asyncio.Task[None]] = set()


class TestJobRunning(RuntimeError):
    """Raised when a test is already in flight for a workflow."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(job: dict[str, Any]) -> float:
    """Seconds since the last progress write (the job's heartbeat)."""
    stamp = job.get("updated_at") or job.get("started_at")
    if not stamp:
        return STALE_AFTER_SECONDS + 1
    try:
        started = datetime.fromisoformat(str(stamp))
    except ValueError:
        return STALE_AFTER_SECONDS + 1
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def is_active(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    if str(job.get("stage")) not in ACTIVE_STAGES:
        return False
    return _age_seconds(job) <= STALE_AFTER_SECONDS


async def get_test_job(
    business_id: str,
    workflow_id: str,
    *,
    job_id: str | None = None,
) -> dict[str, Any] | None:
    job = await load_test_progress(business_id, workflow_id)
    if not job:
        return None
    if job_id and str(job.get("job_id")) != job_id:
        return None
    if str(job.get("stage")) in ACTIVE_STAGES and not is_active(job):
        return {
            **job,
            "stage": "failed",
            "error": "Test job stopped unexpectedly",
            "hint": "Run the test again",
        }
    return job


async def start_test_job(
    business_id: str,
    workflow_id: str,
    *,
    run_agent_pipeline: bool = False,
) -> dict[str, Any]:
    """Claim the workflow's test slot and run the scenario detached."""
    existing = await load_test_progress(business_id, workflow_id)
    if is_active(existing):
        raise TestJobRunning("A test is already running for this workflow")

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "stage": "queued",
        "started_at": _now(),
        "updated_at": _now(),
        "events_total": 0,
        "events_processed": 0,
        "events": [],
    }
    await save_test_progress(business_id, workflow_id, job)
    task = asyncio.create_task(
        _run_job(
            business_id,
            workflow_id,
            job_id,
            run_agent_pipeline=run_agent_pipeline,
        )
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job


async def _run_job(
    business_id: str,
    workflow_id: str,
    job_id: str,
    *,
    run_agent_pipeline: bool,
) -> None:
    from workflow_builder.deployer import check_temporal_ready
    from workflow_builder.owner_test import run_owner_facing_test

    state: dict[str, Any] = {
        "job_id": job_id,
        "stage": "checking_temporal",
        "started_at": _now(),
        "events_total": 0,
        "events_processed": 0,
        "events": [],
    }

    async def persist() -> None:
        state["updated_at"] = _now()
        await save_test_progress(
            business_id,
            workflow_id,
            state,
            expected_job_id=job_id,
        )

    await persist()
    readiness = await check_temporal_ready()
    if not readiness.get("ready"):
        state["stage"] = "failed"
        state["error"] = str(readiness.get("reason") or "Temporal is not ready")
        state["hint"] = readiness.get("hint")
        await persist()
        return

    async def progress(update: dict[str, Any]) -> None:
        stage = str(update.get("stage") or state.get("stage"))
        state["stage"] = stage
        if update.get("events_total") is not None:
            state["events_total"] = int(update["events_total"])
        if update.get("events_processed") is not None:
            state["events_processed"] = int(update["events_processed"])
        if update.get("scenario"):
            state["scenario"] = update["scenario"]
        if update.get("workflow_name"):
            state["workflow_name"] = update["workflow_name"]
        if update.get("last_event"):
            events = list(state.get("events") or [])
            events.append(update["last_event"])
            state["events"] = events[-50:]
        if update.get("failures_so_far") is not None:
            state["failures_so_far"] = int(update["failures_so_far"])
        await persist()

    try:
        results = await run_owner_facing_test(
            business_id=business_id,
            workflow_id=workflow_id,
            run_agent_pipeline=run_agent_pipeline,
            progress=progress,
        )
    except Exception as exc:
        logger.warning(
            "stateful test job failed business=%s workflow=%s",
            business_id,
            workflow_id,
            exc_info=True,
        )
        state["stage"] = "failed"
        state["error"] = str(exc)
        await persist()
        return

    state["stage"] = "completed"
    state["all_passed"] = bool(results.get("all_passed"))
    state["results"] = results
    state["finished_at"] = _now()
    await persist()
