"""Redis progress events for typing / broker status / HITL / simulator SSE."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)


def progress_channel(business_id: str, session_id: str) -> str:
    return f"progress:{business_id}:{session_id}"


async def publish_progress(
    business_id: str,
    session_id: str,
    event_type: str,
    **fields: Any,
) -> None:
    if not business_id or not session_id:
        return
    payload = {
        "type": event_type,
        "business_id": business_id,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    try:
        await get_redis().publish(
            progress_channel(business_id, session_id),
            json.dumps(payload, default=str),
        )
    except Exception as exc:
        logger.debug("progress publish skipped: %s", exc)
