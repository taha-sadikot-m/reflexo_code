"""Celery task for asynchronous Stage 6 customer-memory extraction."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from onboarding.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="extract_customer_memories",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def extract_customer_memories_task(
    self: Any,
    messages: list[dict[str, str]],
    customer_id: str,
    business_id: str,
) -> dict[str, Any]:
    """Extract one completed customer turn without blocking channel delivery."""
    from agent.memory import extract_and_store_memories

    logger.info(
        "Extracting customer memories business=%s customer=%s turns=%s",
        business_id,
        customer_id,
        len(messages),
    )
    return asyncio.run(
        extract_and_store_memories(
            messages,
            customer_id,
            business_id,
            raise_on_error=True,
        )
    )


async def enqueue_memory_extraction(
    messages: list[dict[str, str]],
    customer_id: str,
    business_id: str,
) -> bool:
    """Publish extraction work off-thread; broker failure does not fail a turn."""

    def _delay() -> None:
        extract_customer_memories_task.delay(messages, customer_id, business_id)

    try:
        await asyncio.to_thread(_delay)
        return True
    except Exception as exc:
        logger.warning(
            "Memory extraction enqueue failed business=%s customer=%s error=%s",
            business_id,
            customer_id,
            type(exc).__name__,
        )
        return False
