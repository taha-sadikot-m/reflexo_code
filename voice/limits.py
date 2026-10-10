"""Redis concurrent-call caps."""

from __future__ import annotations

import logging

from voice.config import get_voice_settings
from voice.errors import VoiceCapacityError

logger = logging.getLogger(__name__)

_GLOBAL_KEY = "voice:active:global"
_BIZ_KEY = "voice:active:biz:{business_id}"


async def acquire_call_slot(business_id: str) -> None:
    settings = get_voice_settings()
    from shared.redis_client import get_redis

    redis = get_redis()
    global_n = int(await redis.incr(_GLOBAL_KEY) or 0)
    await redis.expire(_GLOBAL_KEY, 3600)
    biz_key = _BIZ_KEY.format(business_id=business_id)
    biz_n = int(await redis.incr(biz_key) or 0)
    await redis.expire(biz_key, 3600)
    if global_n > settings.voice_max_calls_global or biz_n > settings.voice_max_calls_per_business:
        await redis.decr(_GLOBAL_KEY)
        await redis.decr(biz_key)
        logger.warning(
            "VOICE_CAPACITY denied business=%s global=%s biz=%s",
            business_id,
            global_n,
            biz_n,
        )
        raise VoiceCapacityError("concurrent call limit reached")


async def release_call_slot(business_id: str) -> None:
    from shared.redis_client import get_redis

    redis = get_redis()
    try:
        val = int(await redis.decr(_GLOBAL_KEY) or 0)
        if val < 0:
            await redis.set(_GLOBAL_KEY, 0)
    except Exception:
        logger.debug("voice global slot release failed", exc_info=True)
    try:
        biz_key = _BIZ_KEY.format(business_id=business_id)
        val = int(await redis.decr(biz_key) or 0)
        if val < 0:
            await redis.set(biz_key, 0)
    except Exception:
        logger.debug("voice business slot release failed", exc_info=True)
