"""Fernet credential loading and Meta long-lived token refresh."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from mcp_servers.meta_mcp.redis_keys import meta_creds_key
from mcp_servers.meta_mcp.settings import get_settings
from mcp_servers.meta_mcp.utils.errors import ChannelNotConnectedError, TokenExpiredError
from shared.db import service_connection
from shared.redis_client import get_redis
from shared.redis_keys import channel_account_key

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def init_fernet(encryption_key: str) -> None:
    global _fernet
    if not encryption_key:
        logger.warning("ENCRYPTION_KEY not set — Meta token decrypt will fail")
        _fernet = None
        return
    _fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)


def get_fernet() -> Fernet:
    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() with ENCRYPTION_KEY.")
    return _fernet


def encrypt_token(token: str) -> bytes:
    return get_fernet().encrypt(token.encode("utf-8"))


def decrypt_token(encrypted: bytes | memoryview) -> str:
    raw = bytes(encrypted)
    return get_fernet().decrypt(raw).decode("utf-8")


def is_token_expiring_soon(expires_at_iso: str | None, buffer_hours: int = 1) -> bool:
    if not expires_at_iso:
        return False
    expires_at = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at - timedelta(hours=buffer_hours)


def _creds_from_row(row: Any, access_token: str) -> dict[str, Any]:
    channel = row["channel"]
    account_id = row["channel_account_id"]
    expires = row["token_expires_at"]
    creds: dict[str, Any] = {
        "access_token": access_token,
        "channel_account_id": account_id,
        "channel": channel,
        "token_expires_at": expires.isoformat() if expires else None,
    }
    if channel == "whatsapp":
        creds["phone_number_id"] = account_id
    elif channel == "instagram_dm":
        creds["ig_account_id"] = account_id
    elif channel in ("facebook_messenger", "facebook_comment"):
        creds["page_id"] = account_id
    return creds


async def get_meta_credentials(business_id: str, channel: str) -> dict[str, Any]:
    """
    Load and decrypt Meta credentials for a business channel.

    Lookup: Redis cache → Postgres business_channels → optional refresh.
    """
    cache_key = meta_creds_key(business_id, channel)
    client = get_redis()

    try:
        cached = await client.get(cache_key)
        if cached:
            creds = json.loads(cached)
            if is_token_expiring_soon(creds.get("token_expires_at")):
                return await refresh_and_cache(business_id, channel, creds)
            return creds
    except Exception as exc:
        logger.debug("meta creds cache miss/error: %s", exc)

    async with service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, channel, channel_account_id, access_token_encrypted,
                   refresh_token_encrypted, token_expires_at
            FROM business_channels
            WHERE business_id = $1::uuid AND channel = $2::channel_type AND is_active = true
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            business_id,
            channel,
        )

    if not row:
        raise ChannelNotConnectedError(business_id, channel)

    if not row["access_token_encrypted"]:
        raise ChannelNotConnectedError(business_id, channel)

    try:
        access_token = decrypt_token(row["access_token_encrypted"])
    except (InvalidToken, RuntimeError) as exc:
        logger.error("Failed to decrypt token for %s/%s: %s", business_id, channel, exc)
        raise TokenExpiredError(business_id, channel) from exc

    creds = _creds_from_row(row, access_token)

    if is_token_expiring_soon(creds.get("token_expires_at")):
        return await refresh_and_cache(business_id, channel, creds, db_row=row)

    try:
        await client.setex(cache_key, 300, json.dumps(creds))
    except Exception:
        pass

    return creds


async def ensure_valid_token(business_id: str, channel: str) -> dict[str, Any]:
    """Single entry point for sending/query tools."""
    return await get_meta_credentials(business_id, channel)


async def invalidate_creds_cache(business_id: str, channel: str) -> None:
    """Drop the cached credentials so the next read comes from Postgres."""
    try:
        await get_redis().delete(meta_creds_key(business_id, channel))
    except Exception as exc:
        logger.debug("creds cache delete failed for %s/%s: %s", business_id, channel, exc)


async def invalidate_channel_token(business_id: str, channel: str, reason: str) -> None:
    """Mark a channel as needing reconnection after a confirmed auth failure.

    Only call this once a token read straight from Postgres has also been
    rejected — a single 401 is usually just a stale cache entry.

    Deactivating the whatsapp row also removes the path used to reach the owner,
    so alert_owner_token_failure can only log; the owner must be told out of band.
    """
    logger.error(
        "Deactivating %s for business=%s — Meta rejected the stored token: %s",
        channel,
        business_id,
        reason,
    )
    account_id = ""
    try:
        async with service_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE business_channels
                SET is_active = false,
                    error_message = $1,
                    updated_at = now()
                WHERE business_id = $2::uuid AND channel = $3::channel_type
                RETURNING channel_account_id
                """,
                f"Meta auth failed: {reason}"[:500],
                business_id,
                channel,
            )
        if row:
            account_id = row["channel_account_id"] or ""
    except Exception as exc:
        logger.error("Failed to deactivate %s for %s: %s", channel, business_id, exc)

    try:
        redis = get_redis()
        await redis.delete(meta_creds_key(business_id, channel))
        if account_id:
            await redis.delete(channel_account_key(channel, account_id))
    except Exception:
        pass

    await alert_owner_token_failure(business_id, channel)


async def refresh_and_cache(
    business_id: str,
    channel: str,
    current_creds: dict[str, Any],
    db_row: Any | None = None,
) -> dict[str, Any]:
    """Refresh an expiring Meta access token via fb_exchange_token."""
    settings = get_settings()
    cache_key = meta_creds_key(business_id, channel)

    try:
        if db_row is None:
            async with service_connection() as conn:
                db_row = await conn.fetchrow(
                    """
                    SELECT id, access_token_encrypted, refresh_token_encrypted,
                           channel_account_id, channel
                    FROM business_channels
                    WHERE business_id = $1::uuid AND channel = $2::channel_type AND is_active = true
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    business_id,
                    channel,
                )
            if not db_row:
                raise ChannelNotConnectedError(business_id, channel)

        # Prefer current access token for exchange; refresh_token_encrypted if present
        exchange_token = current_creds.get("access_token")
        if db_row.get("refresh_token_encrypted"):
            try:
                exchange_token = decrypt_token(db_row["refresh_token_encrypted"])
            except (InvalidToken, RuntimeError):
                pass

        if not settings.meta_app_id or not settings.meta_app_secret:
            logger.warning("META_APP_ID/SECRET missing — skipping token refresh")
            return current_creds

        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.get(
                f"https://graph.facebook.com/{settings.meta_api_version}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": exchange_token,
                },
            )
            response.raise_for_status()
            data = response.json()

        new_token = data["access_token"]
        expires_in = int(data.get("expires_in", 5184000))
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        encrypted = encrypt_token(new_token)

        row_id = db_row.get("id")
        async with service_connection() as conn:
            if row_id is not None:
                # Scope to the row we actually read; a business can hold several
                # rows per channel and the others may carry different accounts.
                await conn.execute(
                    """
                    UPDATE business_channels
                    SET access_token_encrypted = $1,
                        token_expires_at = $2,
                        updated_at = now(),
                        error_message = NULL
                    WHERE id = $3
                    """,
                    encrypted,
                    new_expiry,
                    row_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE business_channels
                    SET access_token_encrypted = $1,
                        token_expires_at = $2,
                        updated_at = now(),
                        error_message = NULL
                    WHERE business_id = $3::uuid AND channel = $4::channel_type
                    """,
                    encrypted,
                    new_expiry,
                    business_id,
                    channel,
                )

        updated = {
            **current_creds,
            "access_token": new_token,
            "token_expires_at": new_expiry.isoformat(),
        }
        try:
            await get_redis().setex(cache_key, 300, json.dumps(updated))
        except Exception:
            pass
        return updated

    except ChannelNotConnectedError:
        raise
    except Exception as exc:
        logger.error(
            "Token refresh failed for business=%s channel=%s: %s",
            business_id,
            channel,
            exc,
        )
        await invalidate_channel_token(business_id, channel, f"Token refresh failed: {exc}")
        raise TokenExpiredError(business_id, channel) from exc


async def alert_owner_token_failure(business_id: str, channel: str) -> None:
    """Fire-and-forget: log owner alert (WhatsApp send deferred until channel reconnect)."""
    try:
        async with service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT name, owner_phone FROM businesses WHERE id = $1::uuid",
                business_id,
            )
        if row:
            logger.warning(
                "Meta token expired for business=%s (%s) channel=%s owner=%s — reconnection required",
                business_id,
                row.get("name"),
                channel,
                row.get("owner_phone"),
            )
    except Exception as exc:
        logger.error("alert_owner_token_failure failed: %s", exc)
