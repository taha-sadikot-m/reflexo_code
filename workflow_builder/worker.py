"""Temporal worker for DynamicBusinessWorkflow."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from shared.logging_filters import install_httpx_credential_filter

install_httpx_credential_filter()

from shared.db import close_pool, init_pool
from shared.redis_client import close_redis, init_redis
from workflow_builder.activities import (
    clear_workflow_confirmation_activity,
    execute_tool_activity,
    finish_workflow_run_activity,
    record_workflow_step,
    request_workflow_confirmation_activity,
    send_notification_activity,
    start_scheduled_run_activity,
)
from workflow_builder.interpreter import DynamicBusinessWorkflow

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    await init_pool(settings.database_url, application_name="reflexo-wf-worker")
    try:
        await init_redis(settings.redis_url)
    except Exception:
        logger.warning("Redis init skipped for workflow worker", exc_info=True)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[DynamicBusinessWorkflow],
        activities=[
            execute_tool_activity,
            send_notification_activity,
            request_workflow_confirmation_activity,
            clear_workflow_confirmation_activity,
            start_scheduled_run_activity,
            finish_workflow_run_activity,
            record_workflow_step,
        ],
    )
    logger.info(
        "WF_WORKER listening address=%s queue=%s",
        settings.temporal_address,
        settings.temporal_task_queue,
    )
    try:
        await worker.run()
    finally:
        await close_pool()
        await close_redis()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
