"""Token-gated public setup API for business owners (no staff JWT)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from admin.db import get_admin_pool
from admin.setup_links import (
    API_KEY_IDS,
    load_catalog_payload,
    lookup_active_link,
    mint_oauth_url,
    touch_setup_link,
)

router = APIRouter(prefix="/api/setup", tags=["setup"])


class OauthUrlBody(BaseModel):
    integration_id: str


class ApiKeyBody(BaseModel):
    integration_id: str
    api_key: str = Field(min_length=4, max_length=512)
    api_secret: str | None = Field(default=None, max_length=512)


async def _rate_limit(request: Request, token: str) -> None:
    try:
        from shared.redis_client import get_redis

        redis = get_redis()
        ip = request.client.host if request.client else "unknown"
        key = f"setup:rl:{ip}:{token[:12]}"
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 60)
        if n > 40:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    except HTTPException:
        raise
    except Exception:
        return


async def _require_link(token: str) -> dict[str, Any]:
    pool = await get_admin_pool()
    link = await lookup_active_link(pool, token)
    if not link:
        raise HTTPException(status_code=404, detail="Setup link not found")
    await touch_setup_link(pool, link["id"])
    return link


@router.get("/{token}")
async def get_setup_catalog(token: str, request: Request) -> dict[str, Any]:
    await _rate_limit(request, token)
    link = await _require_link(token)
    pool = await get_admin_pool()
    payload = await load_catalog_payload(
        pool,
        link["business_id"],
        public=True,
        expires_at=link.get("expires_at"),
        business_name=link.get("business_name"),
    )
    payload.pop("business_id", None)
    return payload


@router.post("/{token}/oauth-url")
async def setup_oauth_url(
    token: str, body: OauthUrlBody, request: Request
) -> dict[str, Any]:
    await _rate_limit(request, token)
    link = await _require_link(token)
    return await mint_oauth_url(
        link["business_id"], body.integration_id, setup_token=token
    )


@router.post("/{token}/api-keys")
async def setup_api_keys(
    token: str, body: ApiKeyBody, request: Request
) -> dict[str, Any]:
    await _rate_limit(request, token)
    link = await _require_link(token)
    integration_id = body.integration_id.strip()
    if integration_id not in API_KEY_IDS:
        raise HTTPException(status_code=400, detail="Not an API-key integration")
    secret = body.api_secret if integration_id == "razorpay" else None
    if integration_id == "razorpay" and not (body.api_secret or "").strip():
        raise HTTPException(status_code=400, detail="Razorpay requires api_secret")
    from channel_framework.connection_hub import ApiKeyRequest, connect_api_key
    from channel_framework.hub_tokens import issue_hub_token

    result = await connect_api_key(
        link["business_id"],
        issue_hub_token(link["business_id"]),
        ApiKeyRequest(
            platform=integration_id,
            api_key=body.api_key.strip(),
            api_secret=secret,
        ),
    )
    return result
