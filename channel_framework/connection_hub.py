"""Signed Connection Hub API and OAuth callbacks."""

from __future__ import annotations

import os
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from channel_framework.adapters import get_adapter
from channel_framework.adapters.base import subscription_expiry
from channel_framework.credentials import (
    delete_account,
    list_connected_accounts,
    store_tokens,
)
from channel_framework.hub_tokens import issue_hub_token, verify_hub_token
from shared.db import tenant_connection

router = APIRouter(prefix="/api/connect", tags=["connection-hub"])
logger = logging.getLogger(__name__)

PLATFORMS: dict[str, dict[str, Any]] = {
    "gmail": {
        "label": "Gmail",
        "auth_type": "oauth2",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly",
                   "https://www.googleapis.com/auth/gmail.send",
                   "https://www.googleapis.com/auth/gmail.modify",
                   "https://www.googleapis.com/auth/calendar.events"],
        "capabilities": ["send_message", "read_messages", "thread_support"],
    },
    "outlook": {
        "label": "Outlook",
        "auth_type": "oauth2",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "User.Read", "Mail.Read", "Mail.Send"],
        "capabilities": ["send_message", "read_messages", "thread_support"],
    },
    "slack": {
        "label": "Slack",
        "auth_type": "oauth2",
        "auth_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": ["chat:write", "channels:history", "im:history", "im:write"],
        "capabilities": ["send_message", "read_messages"],
    },
    "razorpay": {"label": "Razorpay", "auth_type": "basic", "capabilities": ["payments"]},
    "fast2sms": {"label": "Fast2SMS", "auth_type": "api_key", "capabilities": ["send_sms"]},
}


def connection_url(business_id: str) -> str:
    base = os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/connect/{business_id}?token={issue_hub_token(business_id)}"


def _authorize(token: str, business_id: str) -> None:
    try:
        verify_hub_token(token, business_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _oauth_env(platform: str) -> tuple[str, str]:
    prefix = {"gmail": "GOOGLE", "outlook": "MICROSOFT", "slack": "SLACK"}[platform]
    client_id = os.getenv(f"{prefix}_CLIENT_ID") or os.getenv(
        f"{prefix}_OAUTH_CLIENT_ID"
    )
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET") or os.getenv(
        f"{prefix}_OAUTH_CLIENT_SECRET"
    )
    if not client_id or not client_secret:
        raise RuntimeError(f"{prefix} OAuth credentials are not configured")
    return client_id, client_secret


def _redirect_uri() -> str:
    return os.getenv("PUBLIC_API_URL", "http://localhost:8030").rstrip("/") + "/api/connect/oauth/callback"


@router.get("/{business_id}")
async def hub_state(business_id: str, token: str) -> dict[str, Any]:
    _authorize(token, business_id)
    accounts = await list_connected_accounts(business_id)
    return {
        "business_id": business_id,
        "platforms": PLATFORMS,
        "accounts": accounts,
    }


@router.get("/{business_id}/auth/{platform}")
async def start_oauth(
    business_id: str,
    platform: str,
    token: str,
    setup_token: str | None = None,
) -> dict[str, str]:
    _authorize(token, business_id)
    config = PLATFORMS.get(platform)
    if not config or config["auth_type"] != "oauth2":
        raise HTTPException(status_code=404, detail="OAuth platform not found")
    client_id, _ = _oauth_env(platform)
    state = issue_hub_token(
        business_id,
        ttl_seconds=600,
        extra={
            "platform": platform,
            "hub_token": token,
            **({"setup_token": setup_token} if setup_token else {}),
        },
    )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(config["scopes"]),
        "state": state,
    }
    if platform == "gmail":
        params.update({"access_type": "offline", "prompt": "consent"})
    return {"url": f"{config['auth_url']}?{urlencode(params)}"}


async def _exchange_oauth(platform: str, code: str) -> dict[str, Any]:
    config = PLATFORMS[platform]
    client_id, client_secret = _oauth_env(platform)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": _redirect_uri(),
    }
    if platform != "slack":
        data["grant_type"] = "authorization_code"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(config["token_url"], data=data)
        response.raise_for_status()
        result = response.json()
    if platform == "slack" and not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "Slack OAuth failed"))
    return result


async def _account_info(platform: str, access_token: str, tokens: dict[str, Any]) -> tuple[str, str]:
    if platform == "slack":
        team = tokens.get("team") or {}
        return str(team.get("id") or ""), str(team.get("name") or team.get("id") or "")
    url = (
        "https://www.googleapis.com/oauth2/v2/userinfo"
        if platform == "gmail"
        else "https://graph.microsoft.com/v1.0/me"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        response.raise_for_status()
        profile = response.json()
    identifier = profile.get("email") or profile.get("mail") or profile.get("userPrincipalName")
    return str(identifier or "").casefold(), str(profile.get("name") or identifier or "")


@router.get("/oauth/callback")
async def oauth_callback(code: str, state: str) -> RedirectResponse:
    try:
        payload = verify_hub_token(state)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    business_id = str(payload["business_id"])
    platform = str(payload.get("platform") or "")
    if platform not in {"gmail", "outlook", "slack"}:
        raise HTTPException(status_code=400, detail="Invalid OAuth platform")
    config = PLATFORMS[platform]
    tokens = await _exchange_oauth(platform, code)
    access_token = str(tokens["access_token"])
    identifier, display = await _account_info(platform, access_token, tokens)
    account = await store_tokens(
        business_id=business_id,
        platform=platform,
        access_token=access_token,
        refresh_token=tokens.get("refresh_token"),
        expires_in=tokens.get("expires_in"),
        scopes=config["scopes"],
        account_identifier=identifier,
        account_display_name=display,
        capabilities=config["capabilities"],
        is_communication_channel=True,
    )
    try:
        subscription = await get_adapter(platform).setup_notifications(
            business_id, access_token
        )
        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                INSERT INTO webhook_subscriptions (
                    business_id, connected_account_id, platform,
                    subscription_id, endpoint_url, expires_at,
                    renewal_interval_hours, next_renewal_at, last_renewed_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3, $4, $5, $6::timestamptz, $7,
                    CASE WHEN $6::timestamptz IS NULL THEN NULL
                         ELSE $6::timestamptz - interval '24 hours' END,
                    now()
                )
                ON CONFLICT (business_id, platform) DO UPDATE SET
                    connected_account_id = EXCLUDED.connected_account_id,
                    subscription_id = EXCLUDED.subscription_id,
                    endpoint_url = EXCLUDED.endpoint_url,
                    expires_at = EXCLUDED.expires_at,
                    renewal_interval_hours = EXCLUDED.renewal_interval_hours,
                    next_renewal_at = EXCLUDED.next_renewal_at,
                    status = 'active',
                    updated_at = now()
                """,
                business_id,
                account["id"],
                platform,
                subscription.subscription_id,
                subscription.endpoint_url,
                subscription_expiry(subscription.expires_at),
                subscription.renewal_interval_hours,
            )
    except Exception:
        await delete_account(business_id, platform)
        raise
    base = os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")
    if payload.get("setup_token"):
        return RedirectResponse(
            f"{base}/setup/{payload['setup_token']}?connected={platform}"
        )
    hub_token = str(payload.get("hub_token") or issue_hub_token(business_id))
    return RedirectResponse(
        f"{base}/connect/{business_id}?token={hub_token}&connected={platform}"
    )


class ApiKeyRequest(BaseModel):
    platform: str
    api_key: str
    api_secret: str | None = None


async def _validate_api_key(body: ApiKeyRequest) -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        if body.platform == "razorpay":
            if not body.api_secret:
                raise HTTPException(status_code=400, detail="api_secret is required")
            response = await client.get(
                "https://api.razorpay.com/v1/payments",
                params={"count": 1},
                auth=(body.api_key, body.api_secret),
            )
        elif body.platform == "fast2sms":
            response = await client.get(
                "https://www.fast2sms.com/dev/wallet",
                headers={"authorization": body.api_key},
            )
        else:
            raise HTTPException(status_code=400, detail="Unsupported API-key platform")
        if response.status_code >= 400:
            raise HTTPException(status_code=400, detail="Invalid credentials")


@router.post("/{business_id}/api-key")
async def connect_api_key(
    business_id: str,
    token: str,
    body: ApiKeyRequest,
) -> dict[str, Any]:
    _authorize(token, business_id)
    await _validate_api_key(body)
    config = PLATFORMS[body.platform]
    await store_tokens(
        business_id=business_id,
        platform=body.platform,
        access_token=body.api_key,
        refresh_token=body.api_secret,
        auth_type=config["auth_type"],
        capabilities=config["capabilities"],
    )
    return {"status": "connected", "platform": body.platform}


@router.delete("/{business_id}/{platform}")
async def disconnect(business_id: str, platform: str, token: str) -> dict[str, str]:
    _authorize(token, business_id)
    if platform in {"gmail", "outlook", "slack"}:
        try:
            await get_adapter(platform).teardown_notifications(business_id)
        except Exception as exc:
            logger.warning(
                "Provider teardown failed; removing local connection business=%s platform=%s: %s",
                business_id,
                platform,
                exc,
            )
    if not await delete_account(business_id, platform):
        raise HTTPException(status_code=404, detail="Connection not found")
    return {"status": "disconnected", "platform": platform}
