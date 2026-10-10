"""Per-business daily LLM token budget (MVP Stage 3 §1.12)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.settings import get_agent_settings
from shared.redis_keys import token_budget_key

logger = logging.getLogger(__name__)

DEFAULT_DAILY_BUDGET_TOKENS = 500_000
BUDGET_EXHAUSTED_MESSAGE = (
    "Daily usage limit reached. Service resumes tomorrow."
)
_TTL_SECONDS = 86400 * 2


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def daily_budget_tokens() -> int:
    try:
        return int(get_agent_settings().token_daily_budget)
    except Exception:
        return DEFAULT_DAILY_BUDGET_TOKENS


def _redis():
    from shared.redis_client import get_redis

    return get_redis()


async def check_budget(
    business_id: str,
    *,
    daily_budget: int | None = None,
    redis: Any | None = None,
) -> bool:
    """True if the business is still under today's token cap."""
    budget = daily_budget if daily_budget is not None else daily_budget_tokens()
    if budget <= 0:
        return False
    try:
        client = redis if redis is not None else _redis()
        raw = await client.get(token_budget_key(business_id, utc_day()))
        used = int(raw or 0)
    except Exception as exc:
        logger.warning("Token budget check failed (fail-open): %s", exc)
        return True
    return used < budget


async def record_usage(
    business_id: str,
    tokens: int,
    *,
    daily_budget: int | None = None,
    redis: Any | None = None,
) -> int:
    """INCRBY today's counter. Returns new total (0 if Redis unavailable or tokens<=0)."""
    if tokens <= 0:
        return 0
    key = token_budget_key(business_id, utc_day())
    try:
        client = redis if redis is not None else _redis()
        used = int(await client.incrby(key, int(tokens)))
        await client.expire(key, _TTL_SECONDS)
    except Exception as exc:
        logger.warning("Token budget record failed: %s", exc)
        return 0

    budget = daily_budget if daily_budget is not None else daily_budget_tokens()
    if budget > 0 and used >= int(budget * 0.8):
        logger.warning(
            "Token budget 80%% business=%s used=%s budget=%s",
            business_id,
            used,
            budget,
        )
    return used


async def reset_usage(business_id: str, *, redis: Any | None = None) -> None:
    """Clear today's counter (T3.6)."""
    try:
        client = redis if redis is not None else _redis()
        await client.delete(token_budget_key(business_id, utc_day()))
    except Exception as exc:
        logger.warning("Token budget reset failed: %s", exc)
