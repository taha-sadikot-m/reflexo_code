"""Meta Instagram / Facebook OAuth helpers for onboarding."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from mcp_servers.meta_mcp.auth.tokens import encrypt_token, init_fernet
from shared.db import service_connection

logger = logging.getLogger(__name__)

MetaOAuthMode = Literal["normal", "rerequest", "fresh"]

INSTAGRAM_SCOPES = [
    "instagram_basic",
    "instagram_manage_messages",
    "instagram_manage_comments",
    "pages_show_list",
    "pages_messaging",
    # Required to POST {page-id}/subscribed_apps; without it Meta never
    # delivers Instagram DMs or comments to our webhook.
    "pages_manage_metadata",
]

FACEBOOK_SCOPES = [
    "pages_show_list",
    "pages_messaging",
    "pages_manage_metadata",
    "pages_read_engagement",
    "pages_manage_engagement",
]


def _app_id() -> str:
    return (os.getenv("META_APP_ID") or "").strip()


def _app_secret() -> str:
    return (os.getenv("META_APP_SECRET") or "").strip()


def _api_version() -> str:
    return (os.getenv("META_API_VERSION") or "v22.0").strip()


def _default_redirect_uri() -> str:
    """Meta MCP serves /api/oauth/callback, so fall back to its port."""
    try:
        from mcp_servers.meta_mcp.settings import get_settings

        port = get_settings().port
    except Exception:
        port = 8021
    return f"http://127.0.0.1:{port}/api/oauth/callback"


def _redirect_uri() -> str:
    return (os.getenv("META_OAUTH_REDIRECT_URI") or _default_redirect_uri()).strip()


def _state_secret() -> bytes:
    secret = _app_secret() or (os.getenv("ENCRYPTION_KEY") or "dev-oauth-state")
    return secret.encode("utf-8")


def encode_oauth_state(payload: dict[str, Any]) -> str:
    """Signed base64url JSON state for OAuth round-trip."""
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def decode_oauth_state(state: str) -> dict[str, Any]:
    try:
        body, sig = state.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError("invalid_state_format") from exc
    expected = hmac.new(_state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("invalid_state_signature")
    pad = "=" * (-len(body) % 4)
    raw = base64.urlsafe_b64decode(body + pad)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_state_payload")
    return data


def generate_instagram_oauth_url(
    business_id: str,
    *,
    setup_token: str | None = None,
    mode: MetaOAuthMode = "normal",
) -> str:
    """Meta Login URL requesting Instagram messaging scopes."""
    return _build_oauth_url(
        business_id,
        "instagram",
        INSTAGRAM_SCOPES,
        setup_token=setup_token,
        mode=mode,
    )


def generate_facebook_oauth_url(
    business_id: str,
    *,
    setup_token: str | None = None,
    mode: MetaOAuthMode = "normal",
) -> str:
    """Meta Login URL requesting Facebook Page messaging scopes."""
    return _build_oauth_url(
        business_id,
        "facebook",
        FACEBOOK_SCOPES,
        setup_token=setup_token,
        mode=mode,
    )


def _build_oauth_url(
    business_id: str,
    integration: str,
    scopes: list[str],
    *,
    setup_token: str | None = None,
    mode: MetaOAuthMode = "normal",
) -> str:
    app_id = _app_id()
    if not app_id:
        raise ValueError("META_APP_ID is not configured")
    payload: dict[str, Any] = {
        "business_id": business_id,
        "integration": integration,
        "redirect": "setup" if setup_token else "onboarding",
    }
    if setup_token:
        payload["setup_token"] = setup_token
    state = encode_oauth_state(payload)
    params = {
        "client_id": app_id,
        "redirect_uri": _redirect_uri(),
        "scope": ",".join(scopes),
        "state": state,
        "response_type": "code",
    }
    if mode == "rerequest":
        params["auth_type"] = "rerequest"
    elif mode == "fresh":
        # Meta documents auth_type as a comma-separated set. Reauthenticate
        # forces credential confirmation; rerequest also revisits declined
        # permissions. Meta still controls browser-session account selection.
        params["auth_type"] = "reauthenticate,rerequest"
        params["return_scopes"] = "true"
    return (
        f"https://www.facebook.com/{_api_version()}/dialog/oauth?"
        f"{urlencode(params)}"
    )


async def exchange_meta_code_for_token(code: str) -> dict[str, Any]:
    """Exchange auth code for a long-lived user access token."""
    app_id = _app_id()
    app_secret = _app_secret()
    if not app_id or not app_secret:
        raise RuntimeError("META_APP_ID and META_APP_SECRET are required")

    version = _api_version()
    redirect_uri = _redirect_uri()
    verify = os.getenv("META_HTTP_VERIFY", "true").lower() not in ("0", "false", "no")

    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        short = await client.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        short.raise_for_status()
        short_data = short.json()
        short_token = short_data["access_token"]

        long = await client.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
        long.raise_for_status()
        long_data = long.json()
        return {
            "access_token": long_data.get("access_token") or short_token,
            "token_type": long_data.get("token_type", "bearer"),
            "expires_in": long_data.get("expires_in"),
        }


async def get_instagram_accounts(access_token: str) -> list[dict[str, Any]]:
    """List IG business accounts linked via the user's Facebook Pages."""
    version = _api_version()
    verify = os.getenv("META_HTTP_VERIFY", "true").lower() not in ("0", "false", "no")
    accounts: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        pages = await client.get(
            f"https://graph.facebook.com/{version}/me/accounts",
            params={
                "fields": (
                    "id,name,category,about,website,picture{url},access_token,"
                    "instagram_business_account{id,username,name,biography,"
                    "profile_picture_url,followers_count,follows_count,media_count}"
                ),
                "access_token": access_token,
            },
        )
        pages.raise_for_status()
        for page in pages.json().get("data") or []:
            ig = page.get("instagram_business_account") or {}
            if ig.get("id"):
                accounts.append(
                    {
                        "id": ig["id"],
                        "username": ig.get("username") or ig["id"],
                        "page_id": page.get("id"),
                        "page_access_token": page.get("access_token") or access_token,
                        "meta_page_data": {
                            "page": {
                                key: page.get(key)
                                for key in (
                                    "id",
                                    "name",
                                    "category",
                                    "about",
                                    "website",
                                    "picture",
                                )
                                if page.get(key) is not None
                            },
                            "instagram": {
                                key: ig.get(key)
                                for key in (
                                    "id",
                                    "username",
                                    "name",
                                    "biography",
                                    "profile_picture_url",
                                    "followers_count",
                                    "follows_count",
                                    "media_count",
                                )
                                if ig.get(key) is not None
                            },
                        },
                    }
                )
    return accounts


# Both are POSTed to {page-id}/subscribed_apps, so every entry must be a field
# valid on the Page object. "comments" is not one: including it makes Meta
# reject the whole call, leaving even "messages" unsubscribed. Instagram comment
# webhooks arrive via the app-level `instagram` object subscription instead.
INSTAGRAM_WEBHOOK_FIELDS = "messages,messaging_postbacks"
FACEBOOK_WEBHOOK_FIELDS = "messages,messaging_postbacks,feed"

PAGE_VALID_SUBSCRIBED_FIELDS = frozenset(
    {
        "messages",
        "messaging_postbacks",
        "messaging_optins",
        "messaging_referrals",
        "messaging_handovers",
        "message_deliveries",
        "message_reads",
        "message_echoes",
        "feed",
        "mention",
        "messaging_seen",
    }
)


async def subscribe_page_webhooks(
    page_id: str,
    page_access_token: str,
    subscribed_fields: str,
) -> None:
    """Subscribe the app to a Page's webhook fields.

    Storing a token is not enough — without this call Meta never delivers
    inbound messages or comments, so the channel looks connected and stays
    silent. Raises on failure so the caller can report it.
    """
    version = _api_version()
    verify = os.getenv("META_HTTP_VERIFY", "true").lower() not in ("0", "false", "no")

    requested = {
        field.strip()
        for field in subscribed_fields.split(",")
        if field.strip()
    }
    invalid = requested - PAGE_VALID_SUBSCRIBED_FIELDS
    if invalid:
        raise RuntimeError(
            "invalid Page subscribed_fields: " + ", ".join(sorted(invalid))
        )

    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        current = await _fetch_page_webhook_fields(
            client, page_id, page_access_token
        )
        merged = current | requested

        # Token goes in the form body, not the query string: httpx logs the URL
        # at INFO and httpx errors embed it, either of which leaks the token.
        response = await client.post(
            f"https://graph.facebook.com/{version}/{page_id}/subscribed_apps",
            data={
                "subscribed_fields": ",".join(sorted(merged)),
                "access_token": page_access_token,
            },
        )

        if response.status_code >= 400:
            detail = response.text
            try:
                detail = (response.json().get("error") or {}).get("message") or detail
            except Exception:
                pass
            raise RuntimeError(
                f"subscribed_apps HTTP {response.status_code}: {detail}"
            )

        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(
                f"subscribed_apps returned success=false for page {page_id}"
            )

        confirmed = await _fetch_page_webhook_fields(
            client, page_id, page_access_token
        )
        missing = requested - confirmed
        if missing:
            raise RuntimeError(
                f"subscribed_apps did not retain fields for page {page_id}: "
                + ", ".join(sorted(missing))
            )


async def _fetch_page_webhook_fields(
    client: httpx.AsyncClient,
    page_id: str,
    page_access_token: str,
) -> set[str]:
    """Read this app's Page fields without putting its token in the URL."""
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/{page_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {page_access_token}"},
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = (response.json().get("error") or {}).get("message") or detail
        except Exception:
            pass
        raise RuntimeError(
            f"subscribed_apps read HTTP {response.status_code}: {detail}"
        )

    app_id = _app_id()
    fields: set[str] = set()
    for row in (response.json() or {}).get("data") or []:
        if app_id and str(row.get("id") or "") not in ("", app_id):
            continue
        fields.update(str(field) for field in row.get("subscribed_fields") or [])
    return fields


async def get_facebook_pages(access_token: str) -> list[dict[str, Any]]:
    """List Facebook Pages the user manages (for Messenger)."""
    version = _api_version()
    verify = os.getenv("META_HTTP_VERIFY", "true").lower() not in ("0", "false", "no")

    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        pages = await client.get(
            f"https://graph.facebook.com/{version}/me/accounts",
            params={
                "fields": (
                    "id,name,category,about,website,picture{url},fan_count,"
                    "followers_count,access_token"
                ),
                "access_token": access_token,
            },
        )
        pages.raise_for_status()
        return [
            {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "access_token": p.get("access_token") or access_token,
                "meta_page_data": {
                    key: p.get(key)
                    for key in (
                        "id",
                        "name",
                        "category",
                        "about",
                        "website",
                        "picture",
                        "fan_count",
                        "followers_count",
                    )
                    if p.get(key) is not None
                },
            }
            for p in (pages.json().get("data") or [])
            if p.get("id")
        ]


async def notify_owner_text(business_id: str, text: str) -> None:
    """Best-effort free-form WhatsApp notify; no-op outside the 24h window."""
    try:
        from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token, init_fernet
        from mcp_servers.meta_mcp.settings import get_settings
        from mcp_servers.meta_mcp.tools.helpers import validate_e164, wa_to_api
        from mcp_servers.meta_mcp.utils.graph_client import graph_post

        settings = get_settings()
        if settings.encryption_key:
            init_fernet(settings.encryption_key)

        async with service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT owner_phone FROM businesses WHERE id = $1::uuid",
                business_id,
            )
        if not row or not row["owner_phone"]:
            return

        creds = await ensure_valid_token(business_id, "whatsapp")
        to_e164 = validate_e164(row["owner_phone"])
        await graph_post(
            f"{creds['phone_number_id']}/messages",
            creds["access_token"],
            {
                "messaging_product": "whatsapp",
                "to": wa_to_api(to_e164),
                "type": "text",
                "text": {"body": text},
            },
            business_id=business_id,
            channel="whatsapp",
        )
    except Exception as exc:
        logger.warning("Owner notify failed for %s: %s", business_id, exc)


async def _mark_channel_connected(business_id: str, integration: str) -> None:
    try:
        from onboarding.progress import load_onboarding_progress, save_onboarding_progress

        progress = await load_onboarding_progress(business_id)
        if integration == "instagram":
            progress.instagram_connected = True
        else:
            progress.facebook_connected = True
        await save_onboarding_progress(business_id, progress)
    except Exception as exc:
        logger.warning("Onboarding progress update failed for %s: %s", business_id, exc)


async def complete_meta_oauth(code: str, state: str) -> dict[str, Any]:
    """Finish the Meta login round-trip and connect the channel.

    Single source of truth for the exchange, shared by the API server route and
    the Meta MCP route, so the two entry points cannot drift apart.

    Returns a result dict for the caller to render. Raises only on genuinely
    unexpected failures; expected outcomes (bad state, no eligible accounts)
    come back as ok=False with a reason the owner can act on.
    """
    payload = decode_oauth_state(state)
    business_id = str(payload.get("business_id") or "").strip()
    integration = str(payload.get("integration") or "").strip().lower()

    if not business_id:
        return {"ok": False, "reason": "missing_business", "integration": integration}
    if integration not in ("instagram", "facebook"):
        return {"ok": False, "reason": "unknown_integration", "integration": integration}

    token_data = await exchange_meta_code_for_token(code)
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in")

    if integration == "instagram":
        accounts = await get_instagram_accounts(user_token)
        if not accounts:
            await notify_owner_text(
                business_id,
                "Instagram connect nahi ho paya. Check ki aapka Instagram business "
                "account hai aur Facebook Page se linked hai.",
            )
            return {"ok": False, "reason": "no_instagram_account", "integration": integration}
        await store_instagram_channels(business_id, accounts, user_token, expires_in)
        fields = INSTAGRAM_WEBHOOK_FIELDS
        labels = [a.get("username") or a["id"] for a in accounts]
    else:
        accounts = await get_facebook_pages(user_token)
        if not accounts:
            await notify_owner_text(
                business_id,
                "Facebook Page connect nahi ho paya. Ensure aapke paas Page admin access hai.",
            )
            return {"ok": False, "reason": "no_facebook_page", "integration": integration}
        await store_facebook_channels(business_id, accounts, user_token, expires_in)
        fields = FACEBOOK_WEBHOOK_FIELDS
        labels = [a.get("name") or a["id"] for a in accounts]

    # Subscribe on the Page: Instagram messaging webhooks are delivered through
    # the linked Page, not the IG account id we store as channel_account_id.
    warnings: list[str] = []
    for account in accounts:
        page_id = str(account.get("page_id") or account.get("id") or "")
        page_token = account.get("page_access_token") or account.get("access_token") or user_token
        if not page_id:
            continue
        try:
            await subscribe_page_webhooks(page_id, page_token, fields)
        except Exception as exc:
            logger.warning("subscribed_apps failed for page %s: %s", page_id, exc)
            warnings.append(f"{page_id}: {exc}")

    await _mark_channel_connected(business_id, integration)

    primary = labels[0] if labels else business_id
    if integration == "instagram":
        await notify_owner_text(
            business_id,
            f"Instagram connected! @{primary} ke DMs ab Maya handle karegi.",
        )
    else:
        await notify_owner_text(
            business_id,
            f"Facebook Messenger connected! {primary} ke messages ab Maya handle karegi.",
        )

    return {
        "ok": True,
        "integration": integration,
        "business_id": business_id,
        "accounts": labels,
        "subscription_warnings": warnings,
        "setup_token": str(payload.get("setup_token") or "").strip() or None,
    }


def _expiry_from(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))


async def _invalidate_channel_caches(business_id: str, channel: str, account_id: str) -> None:
    """Drop cached credentials and inbound routing for a freshly stored channel.

    Without this the 5-minute creds cache and the hour-long channel_account
    cache keep serving pre-connect state, so a successful connect appears to
    do nothing until the entries age out.
    """
    try:
        from mcp_servers.meta_mcp.auth.tokens import invalidate_creds_cache
        from shared.redis_client import get_redis
        from shared.redis_keys import channel_account_key

        await invalidate_creds_cache(business_id, channel)
        if account_id:
            await get_redis().delete(channel_account_key(channel, account_id))
    except Exception as exc:
        logger.warning(
            "Cache invalidation skipped for %s/%s: %s — stale reads possible for up to 1h",
            business_id,
            channel,
            exc,
        )


async def store_instagram_channels(
    business_id: str,
    accounts: list[dict[str, Any]],
    fallback_token: str,
    expires_in: int | None = None,
) -> None:
    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required to store channel tokens")
    init_fernet(encryption_key)

    expires_at = _expiry_from(expires_in)

    async with service_connection() as conn:
        for ig in accounts:
            token = ig.get("page_access_token") or fallback_token
            encrypted = encrypt_token(token)
            await conn.execute(
                """
                INSERT INTO business_channels (
                    business_id, channel, channel_account_id, channel_display_name,
                    access_token_encrypted, token_expires_at, is_active,
                    error_message, meta_page_data, updated_at
                ) VALUES (
                    $1::uuid, 'instagram_dm', $2, $3, $4, $5, true, NULL,
                    $6::jsonb, now()
                )
                ON CONFLICT (business_id, channel, channel_account_id)
                DO UPDATE SET
                    access_token_encrypted = EXCLUDED.access_token_encrypted,
                    token_expires_at = EXCLUDED.token_expires_at,
                    channel_display_name = EXCLUDED.channel_display_name,
                    meta_page_data = EXCLUDED.meta_page_data,
                    is_active = true,
                    error_message = NULL,
                    updated_at = now()
                """,
                business_id,
                ig["id"],
                ig.get("username") or ig["id"],
                encrypted,
                expires_at,
                json.dumps(ig.get("meta_page_data") or {}),
            )

    for ig in accounts:
        await _invalidate_channel_caches(business_id, "instagram_dm", str(ig["id"]))


async def store_facebook_channels(
    business_id: str,
    pages: list[dict[str, Any]],
    fallback_token: str,
    expires_in: int | None = None,
) -> None:
    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required to store channel tokens")
    init_fernet(encryption_key)

    expires_at = _expiry_from(expires_in)

    async with service_connection() as conn:
        for page in pages:
            token = page.get("access_token") or fallback_token
            encrypted = encrypt_token(token)
            await conn.execute(
                """
                INSERT INTO business_channels (
                    business_id, channel, channel_account_id, channel_display_name,
                    access_token_encrypted, token_expires_at, is_active,
                    error_message, meta_page_data, updated_at
                ) VALUES (
                    $1::uuid, 'facebook_messenger', $2, $3, $4, $5, true, NULL,
                    $6::jsonb, now()
                )
                ON CONFLICT (business_id, channel, channel_account_id)
                DO UPDATE SET
                    access_token_encrypted = EXCLUDED.access_token_encrypted,
                    token_expires_at = EXCLUDED.token_expires_at,
                    channel_display_name = EXCLUDED.channel_display_name,
                    meta_page_data = EXCLUDED.meta_page_data,
                    is_active = true,
                    error_message = NULL,
                    updated_at = now()
                """,
                business_id,
                page["id"],
                page.get("name") or page["id"],
                encrypted,
                expires_at,
                json.dumps(page.get("meta_page_data") or {}),
            )

    for page in pages:
        await _invalidate_channel_caches(business_id, "facebook_messenger", str(page["id"]))
