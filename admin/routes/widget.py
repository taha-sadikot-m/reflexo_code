"""Public web widget send + SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from admin.db import get_admin_pool
from admin.helpers import require_business
from api.settings import get_api_settings
from mcp_servers.meta_mcp.redis_keys import incoming_channel
from mcp_servers.meta_mcp.webhooks.resolve import publish_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/widget", tags=["widget"])


class WidgetSendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    visitor_id: str | None = None
    session_id: str | None = None
    display_name: str | None = None


def _origins() -> list[str]:
    raw = (get_api_settings().widget_allowed_origins or "*").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _check_origin(request: Request) -> None:
    allowed = _origins()
    if "*" in allowed:
        return
    origin = request.headers.get("origin") or ""
    if origin and origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin not allowed")


async def _rate_limit(ip: str, visitor_id: str) -> None:
    limit = int(get_api_settings().widget_rate_limit_per_min or 30)
    try:
        from shared.redis_client import get_redis

        redis = get_redis()
        key = f"widget:rl:{ip}:{visitor_id}"
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 60)
        if n > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    except HTTPException:
        raise
    except Exception:
        return


async def _ensure_channel(business_id: str) -> None:
    pool = await get_admin_pool()
    await pool.execute(
        """
        INSERT INTO business_channels (business_id, channel, channel_account_id, channel_display_name, is_active)
        VALUES ($1::uuid, 'web_widget'::channel_type, $1::text, 'Web widget', true)
        ON CONFLICT (business_id, channel, channel_account_id) DO NOTHING
        """,
        business_id,
    )


@router.post("/{business_id}/send")
async def widget_send(
    business_id: str, body: WidgetSendBody, request: Request
) -> dict[str, Any]:
    _check_origin(request)
    await require_business(business_id)
    visitor_id = (body.visitor_id or "").strip() or str(uuid.uuid4())
    session_id = (body.session_id or "").strip() or f"wg-{uuid.uuid4().hex[:12]}"
    ip = request.client.host if request.client else "unknown"
    await _rate_limit(ip, visitor_id)
    await _ensure_channel(business_id)
    message_id = f"wg-msg-{uuid.uuid4().hex[:16]}"
    event = {
        "event_type": "message",
        "channel": "web_widget",
        "sender_id": visitor_id,
        "business_id": business_id,
        "message_id": message_id,
        "message_type": "text",
        "skip_send": True,
        "session_id": session_id,
        "receiving_account_id": business_id,
        "sender_display_name": body.display_name or "Visitor",
        "content": {"text": body.text.strip(), "type": "text"},
    }
    await publish_event(incoming_channel(business_id), event)
    return {
        "status": "queued",
        "visitor_id": visitor_id,
        "session_id": session_id,
        "message_id": message_id,
    }


@router.get("/{business_id}/events")
async def widget_events(
    business_id: str,
    request: Request,
    session_id: str = Query(..., min_length=1),
) -> StreamingResponse:
    _check_origin(request)
    await require_business(business_id)
    from agent.progress import progress_channel
    from shared.redis_client import get_redis

    channel_name = progress_channel(business_id, session_id)

    async def _gen():
        redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_name)
        try:
            yield f"event: ready\ndata: {json.dumps({'session_id': session_id})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    yield f"event: progress\ndata: {data}\n\n"
                else:
                    yield ": ping\n\n"
                    await asyncio.sleep(0.05)
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{business_id}/history")
async def widget_history(
    business_id: str,
    request: Request,
    visitor_id: str = Query(..., min_length=8),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    _check_origin(request)
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT m.role, m.content, m.created_at, m.ai_metadata
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN identity_map im ON im.customer_id = c.customer_id
        WHERE m.business_id = $1::uuid
          AND im.channel = 'web_widget'::channel_type
          AND im.external_id = $2
        ORDER BY m.created_at DESC
        LIMIT $3
        """,
        business_id,
        visitor_id,
        limit,
    )
    items = [
        {
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in reversed(list(rows))
    ]
    return {"items": items}
