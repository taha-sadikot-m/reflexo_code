"""Backend-only admin simulator: SSE progress + inject inbound events."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.settings import get_api_settings
from mcp_servers.meta_mcp.redis_keys import incoming_channel
from mcp_servers.meta_mcp.webhooks.resolve import publish_event
from shared.simulator_inbound import build_simulator_inbound_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/simulator", tags=["admin-simulator"])


class SimulatorSendBody(BaseModel):
    text: str
    mode: str = Field(default="customer", pattern="^(customer|owner)$")
    channel: str = "whatsapp"
    session_id: str | None = None
    customer_name: str = "Test Customer"
    comment_id: str | None = None
    post_id: str | None = None


def _require_simulator_key(x_simulator_key: str | None) -> None:
    expected = (get_api_settings().simulator_api_key or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="SIMULATOR_API_KEY is not configured")
    provided = (x_simulator_key or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid simulator key")


@router.get("/{business_id}/events")
async def simulator_events(
    business_id: str,
    session_id: str = Query(..., min_length=1),
    x_simulator_key: str | None = Header(default=None, alias="X-Simulator-Key"),
) -> StreamingResponse:
    _require_simulator_key(x_simulator_key)
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


@router.post("/{business_id}/send")
async def simulator_send(
    business_id: str,
    body: SimulatorSendBody,
    x_simulator_key: str | None = Header(default=None, alias="X-Simulator-Key"),
) -> dict[str, Any]:
    _require_simulator_key(x_simulator_key)
    session_id = body.session_id or f"sim-{uuid.uuid4().hex[:12]}"
    try:
        event = build_simulator_inbound_event(
            business_id=business_id,
            text=body.text,
            channel=body.channel,
            mode=body.mode,  # type: ignore[arg-type]
            session_id=session_id,
            customer_name=body.customer_name,
            comment_id=body.comment_id,
            post_id=body.post_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await publish_event(incoming_channel(business_id), event)
    return {
        "status": "queued",
        "session_id": event["session_id"],
        "message_id": event.get("message_id"),
        "sender_id": event["sender_id"],
        "channel": event["channel"],
        "event_type": event["event_type"],
        "comment_id": event.get("comment_id"),
        "post_id": event.get("post_id"),
        "skip_send": True,
    }
