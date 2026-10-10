"""Short-lived business-bound Connection Hub tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


def _secret() -> bytes:
    value = os.getenv("CONNECTION_HUB_SECRET", "")
    if not value:
        raise RuntimeError("CONNECTION_HUB_SECRET is required")
    return value.encode()


def issue_hub_token(
    business_id: str,
    *,
    ttl_seconds: int = 48 * 3600,
    extra: dict[str, Any] | None = None,
) -> str:
    payload = {
        "business_id": business_id,
        "exp": int(time.time()) + max(60, ttl_seconds),
        **(extra or {}),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_hub_token(token: str, business_id: str | None = None) -> dict[str, Any]:
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        )
    except Exception as exc:
        raise ValueError("invalid connection token") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("connection token expired")
    if business_id and payload.get("business_id") != business_id:
        raise ValueError("connection token belongs to another business")
    return payload
