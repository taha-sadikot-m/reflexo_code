#!/usr/bin/env python3
"""Stage 12 smoke test: deploy an aggregation workflow, start it, then signal it.

Usage:
    python scripts/smoke_stage12_temporal.py <business_id>

Requires: docker compose Temporal running and `python -m workflow_builder.worker`.
Sends no customer messages -- the workflow parks on an aggregate step.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agent.settings import get_agent_settings
from shared.db import close_pool, init_pool
from workflow_builder.deployer import deploy_workflow, route_event_to_workflows
from workflow_builder.models import StepType, WorkflowDefinition, WorkflowStep
from workflow_builder.store import list_workflows

SMOKE_NAME = "SMOKE Stage 12 aggregation"


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name=SMOKE_NAME,
        description="Smoke test: park on an aggregate step so nothing is sent.",
        trigger_type="instagram_comment",
        instance_key="{{customer.id}}",
        steps=[
            WorkflowStep(
                name="wait_for_comments",
                type=StepType.AGGREGATE_EVENTS,
                event_filter={"type": "instagram_comment"},
                threshold=3,
                window_seconds=3600,
            )
        ],
        nl_description="If someone comments on 3 of my posts, react",
    )


def _event(business_id: str) -> dict:
    return {
        "type": "instagram_comment",
        "trigger_type": "instagram_comment",
        "business_id": business_id,
        "customer": {"id": "smoke-customer-1"},
        "customer_id": None,
        "message": {"text": "smoke comment"},
    }


async def main(business_id: str) -> None:
    settings = get_agent_settings()
    await init_pool(settings.database_url, application_name="stage12-smoke")
    try:
        existing = [w for w in await list_workflows(business_id) if w.get("name") == SMOKE_NAME]
        if existing:
            workflow = existing[0]
            print(f"reusing workflow {workflow['id']}")
        else:
            workflow = await deploy_workflow(business_id, _definition())
            print(f"deployed workflow {workflow['id']}")

        event = _event(business_id)
        first = await route_event_to_workflows(event, business_id)
        print(f"event 1 routed (expect a START): {first}")

        await asyncio.sleep(3)

        second = await route_event_to_workflows(event, business_id)
        print(f"event 2 routed (expect a SIGNAL): {second}")

        print("\nOpen http://localhost:8088 -> Workflows.")
        print("Expect one Running execution ending in ':smoke-customer-1' with 2 events.")
    finally:
        await close_pool()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1]))
