"""Universal webhook routes for non-Meta channels."""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from channel_framework.adapters import get_adapter
from channel_framework.pipeline import process_normalized_message
from shared.db import service_connection

router = APIRouter(prefix="/webhooks/channel", tags=["channel-framework"])


def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = os.getenv("SLACK_SIGNING_SECRET", "").encode()
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    expected = "v0=" + hmac.new(
        secret,
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/{platform}/{business_id}", response_model=None)
async def receive_channel_webhook(
    platform: str,
    business_id: str,
    request: Request,
) -> dict[str, Any] | PlainTextResponse:
    if platform == "gmail":
        raise HTTPException(
            status_code=404,
            detail="Gmail notifications use /webhooks/channel/gmail/pubsub",
        )
    validation_token = request.query_params.get("validationToken")
    if platform == "outlook" and validation_token:
        return PlainTextResponse(validation_token)
    body = await request.body()
    if platform == "slack" and not verify_slack_signature(
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if platform == "slack" and payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    try:
        adapter = get_adapter(platform)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    message = await adapter.parse_webhook(payload, business_id)
    if message is None:
        return {"status": "ignored"}
    result = await process_normalized_message(message)
    return {"status": "processed", "result": result}


@router.post("/gmail/pubsub")
async def receive_gmail_pubsub(request: Request) -> dict[str, Any]:
    expected = os.getenv("GOOGLE_PUBSUB_VERIFICATION_TOKEN", "")
    supplied = (
        request.headers.get("x-goog-pubsub-token")
        or request.query_params.get("token")
        or ""
    )
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid Pub/Sub token")
    payload = await request.json()
    envelope = payload.get("message") or {}
    encoded = envelope.get("data") if isinstance(envelope, dict) else None
    try:
        notice = json.loads(
            base64.urlsafe_b64decode(
                str(encoded) + "=" * (-len(str(encoded)) % 4)
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Pub/Sub data") from exc
    mailbox = str(notice.get("emailAddress") or "").strip().casefold()
    if not mailbox:
        raise HTTPException(status_code=400, detail="emailAddress is required")
    async with service_connection() as conn:
        business_id = await conn.fetchval(
            """
            SELECT business_id::text
            FROM connected_accounts
            WHERE platform = 'gmail'
              AND lower(account_identifier) = $1
              AND is_active = true
              AND is_communication_channel = true
            """,
            mailbox,
        )
    if not business_id:
        raise HTTPException(status_code=404, detail="Gmail account is not connected")
    adapter = get_adapter("gmail")
    message = await adapter.parse_webhook(
        {"message": payload.get("message")},
        business_id,
    )
    if message is None:
        return {"status": "ignored"}
    return {
        "status": "processed",
        "result": await process_normalized_message(message),
    }
