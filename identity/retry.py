"""Exponential backoff retry helper."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    retryable: Callable[[Exception], bool] | None = None,
) -> T:
    """Run async fn with exponential backoff on retryable errors."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if retryable and not retryable(e):
                raise
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("Retry attempt %s after error: %s (sleep %.3fs)", attempt, e, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
