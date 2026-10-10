"""Bridge owner YES/NO replies to waiting Temporal workflow instances."""

from __future__ import annotations

import json
from typing import Any

from shared.redis_client import get_redis
from shared.redis_keys import workflow_confirmation_key

_YES = {"yes", "approve", "approved", "haan", "हाँ"}
_NO = {"no", "deny", "decline", "cancel", "nahi", "nahin", "नहीं"}


def parse_confirmation(text: str) -> bool | None:
    normalized = " ".join(text.casefold().strip().split())
    if normalized in _YES:
        return True
    if normalized in _NO:
        return False
    return None


async def signal_pending_confirmation(
    business_id: str,
    message_text: str,
) -> dict[str, Any] | None:
    """Pop and signal the oldest pending workflow when the owner says YES/NO."""
    approved = parse_confirmation(message_text)
    if approved is None:
        return None

    redis = get_redis()
    key = workflow_confirmation_key(business_id)
    raw = await redis.lpop(key)
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        entry = json.loads(text)
    except json.JSONDecodeError:
        return None

    temporal_id = str(entry.get("temporal_instance_id") or "")
    if not temporal_id:
        return None
    try:
        from workflow_builder.deployer import _client
        from workflow_builder.interpreter import DynamicBusinessWorkflow

        client = await _client()
        handle = client.get_workflow_handle(temporal_id)
        await handle.signal(
            DynamicBusinessWorkflow.on_event,
            {
                "type": "workflow_confirmation",
                "approved": approved,
                "business_id": business_id,
            },
        )
    except Exception:
        # Preserve the pending approval if Temporal was transiently unavailable.
        await redis.lpush(key, text)
        await redis.expire(key, 15 * 60)
        raise
    return {
        "approved": approved,
        "workflow_id": entry.get("workflow_id"),
        "temporal_instance_id": temporal_id,
        "summary": entry.get("summary"),
    }
