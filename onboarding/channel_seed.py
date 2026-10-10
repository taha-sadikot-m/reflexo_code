"""Shared helpers for seeding WhatsApp channel credentials onto a business."""

from __future__ import annotations

import os
from typing import Any


async def seed_whatsapp_channel_for_business(
    conn: Any,
    business_id: str,
    *,
    seed_business_id: str | None = None,
) -> dict[str, Any]:
    """
    Attach active WhatsApp credentials to ``business_id``.

    Prefers copying an active channel from ``seed_business_id`` (default mall
    seed), else uses META_PHONE_NUMBER_ID + META_SYSTEM_ACCESS_TOKEN + ENCRYPTION_KEY.
    Deactivates the same phone_number_id on other businesses so identity maps here.
    """
    default_seed = "00000000-0000-0000-0000-000000000001"
    seed_id = (seed_business_id or os.getenv("WHATSAPP_SEED_BUSINESS_ID") or default_seed).strip()

    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    access_token = (os.getenv("META_SYSTEM_ACCESS_TOKEN") or "").strip()
    phone_number_id = (os.getenv("META_PHONE_NUMBER_ID") or "").strip()

    seed_row = None
    if seed_id and seed_id != business_id:
        seed_row = await conn.fetchrow(
            """
            SELECT channel_account_id, channel_display_name,
                   access_token_encrypted, refresh_token_encrypted, token_expires_at
            FROM business_channels
            WHERE business_id = $1::uuid AND channel = 'whatsapp' AND is_active = true
            LIMIT 1
            """,
            seed_id,
        )

    refresh = None
    expires = None
    if seed_row and seed_row["access_token_encrypted"]:
        phone_number_id = seed_row["channel_account_id"]
        display_name = seed_row["channel_display_name"] or "Onboarding WhatsApp"
        encrypted = seed_row["access_token_encrypted"]
        refresh = seed_row["refresh_token_encrypted"]
        expires = seed_row["token_expires_at"]
    elif encryption_key and access_token and phone_number_id:
        from mcp_servers.meta_mcp.auth.tokens import encrypt_token, init_fernet

        init_fernet(encryption_key)
        encrypted = encrypt_token(access_token)
        display_name = os.getenv("META_WHATSAPP_DISPLAY_NAME") or "Onboarding WhatsApp"
    else:
        return {
            "ok": False,
            "error": (
                "Cannot seed WhatsApp channel — set META_PHONE_NUMBER_ID + "
                "META_SYSTEM_ACCESS_TOKEN + ENCRYPTION_KEY, or seed a source business first."
            ),
        }

    await conn.execute(
        """
        UPDATE business_channels
        SET is_active = false, updated_at = now()
        WHERE channel = 'whatsapp'
          AND channel_account_id = $1
          AND business_id <> $2::uuid
        """,
        phone_number_id,
        business_id,
    )
    await conn.execute(
        """
        UPDATE business_channels
        SET is_active = false, updated_at = now()
        WHERE business_id = $1::uuid
          AND channel = 'whatsapp'
          AND channel_account_id <> $2
        """,
        business_id,
        phone_number_id,
    )
    await conn.execute(
        """
        INSERT INTO business_channels (
            business_id, channel, channel_account_id, channel_display_name,
            access_token_encrypted, refresh_token_encrypted, token_expires_at, is_active
        )
        VALUES (
            $1::uuid, 'whatsapp', $2, $3, $4, $5, $6, true
        )
        ON CONFLICT (business_id, channel, channel_account_id)
        DO UPDATE SET
            channel_display_name = EXCLUDED.channel_display_name,
            access_token_encrypted = EXCLUDED.access_token_encrypted,
            refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
            token_expires_at = EXCLUDED.token_expires_at,
            is_active = true,
            updated_at = now()
        """,
        business_id,
        phone_number_id,
        display_name,
        encrypted,
        refresh,
        expires,
    )

    try:
        from shared.redis_client import get_redis, init_redis
        from shared.redis_keys import channel_account_key

        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            await init_redis(redis_url)
            r = get_redis()
            await r.set(channel_account_key("whatsapp", phone_number_id), business_id)
    except Exception:
        pass

    return {
        "ok": True,
        "phone_number_id": phone_number_id,
        "display_name": display_name,
    }
