#!/usr/bin/env python3
"""Seed Instagram DM + Facebook Messenger rows on business_channels.

Use the SAME Meta webhook callback URL as WhatsApp (Meta MCP). Then:

1. Meta Developer Console → your app → Instagram:
   Subscribe fields: messages, comments
2. Meta Developer Console → Facebook Page / Webhooks:
   Subscribe fields: messages, feed
3. Connect the IG professional account and Page to this app (roles / login).
4. Run this script so inbound webhooks resolve to HolonCode (or --business-id).
5. Restart Meta MCP. The agent worker already listens on incoming:*.

Required env:
  DATABASE_URL
  ENCRYPTION_KEY
  META_SYSTEM_ACCESS_TOKEN   — Graph token that can send on the Page/IG

At least one of:
  META_IG_ACCOUNT_ID         — Instagram professional account id (entry.id)
  META_FACEBOOK_PAGE_ID      — Facebook Page id (entry.id)

Optional:
  REDIS_URL
  WHATSAPP_SEED_BUSINESS_ID / --business-id  — default HolonCode live tenant
  META_IG_DISPLAY_NAME / META_FB_DISPLAY_NAME

Live T5.1–T5.4 after seed: DM or comment from a test user, then confirm the
worker logs status=responded (or cache_hit) and a reply on that surface.
WhatsApp+Instagram “same person” still needs merge_customers or a shared email;
IGSID is not a phone number.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DEFAULT_BIZ = "918750d7-2254-4cfd-bd02-c22c3ab708ec"


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"ERROR: {name} is missing in .env")
        sys.exit(1)
    return value


async def _upsert_channel(
    conn,
    *,
    business_id: str,
    channel: str,
    account_id: str,
    display_name: str,
    encrypted: str,
) -> dict:
    await conn.execute(
        """
        UPDATE business_channels
        SET is_active = false, updated_at = now()
        WHERE business_id = $1::uuid
          AND channel = $2::channel_type
          AND channel_account_id <> $3
        """,
        business_id,
        channel,
        account_id,
    )
    row = await conn.fetchrow(
        """
        INSERT INTO business_channels (
            business_id, channel, channel_account_id, channel_display_name,
            access_token_encrypted, is_active, error_message, updated_at
        ) VALUES (
            $1::uuid, $2::channel_type, $3, $4, $5, true, NULL, now()
        )
        ON CONFLICT (business_id, channel, channel_account_id) DO UPDATE
        SET access_token_encrypted = EXCLUDED.access_token_encrypted,
            channel_display_name = EXCLUDED.channel_display_name,
            is_active = true,
            error_message = NULL,
            updated_at = now()
        RETURNING id, channel, channel_account_id, is_active
        """,
        business_id,
        channel,
        account_id,
        display_name,
        encrypted,
    )
    return dict(row)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--business-id",
        default=(os.getenv("WHATSAPP_SEED_BUSINESS_ID") or DEFAULT_BIZ).strip(),
    )
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    access_token = _require("META_SYSTEM_ACCESS_TOKEN")
    ig_id = (os.getenv("META_IG_ACCOUNT_ID") or "").strip()
    page_id = (os.getenv("META_FACEBOOK_PAGE_ID") or "").strip()
    if not ig_id and not page_id:
        print("ERROR: set META_IG_ACCOUNT_ID and/or META_FACEBOOK_PAGE_ID")
        print("  These are the entry.id values Meta sends on Instagram/Page webhooks.")
        return 1

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    business_id = args.business_id

    from mcp_servers.meta_mcp.auth.tokens import encrypt_token, init_fernet
    from mcp_servers.meta_mcp.redis_keys import meta_creds_key
    from shared.db import close_pool, init_pool, service_connection
    from shared.redis_client import close_redis, get_redis, init_redis
    from shared.redis_keys import channel_account_key

    init_fernet(encryption_key)
    encrypted = encrypt_token(access_token)

    await init_pool(database_url, application_name="seed-social-channels")

    async with service_connection() as conn:
        biz = await conn.fetchrow(
            "SELECT id, name FROM businesses WHERE id = $1::uuid",
            business_id,
        )
        if not biz:
            print(f"ERROR: business {business_id} not found.")
            await close_pool()
            return 1

        seeded: list[dict] = []
        if ig_id:
            seeded.append(
                await _upsert_channel(
                    conn,
                    business_id=business_id,
                    channel="instagram_dm",
                    account_id=ig_id,
                    display_name=(os.getenv("META_IG_DISPLAY_NAME") or "Instagram").strip(),
                    encrypted=encrypted,
                )
            )
        if page_id:
            seeded.append(
                await _upsert_channel(
                    conn,
                    business_id=business_id,
                    channel="facebook_messenger",
                    account_id=page_id,
                    display_name=(os.getenv("META_FB_DISPLAY_NAME") or "Facebook Page").strip(),
                    encrypted=encrypted,
                )
            )

    try:
        await init_redis(redis_url)
        redis = get_redis()
        if ig_id:
            await redis.delete(meta_creds_key(business_id, "instagram_dm"))
            await redis.delete(channel_account_key("instagram_dm", ig_id))
        if page_id:
            await redis.delete(meta_creds_key(business_id, "facebook_messenger"))
            await redis.delete(channel_account_key("facebook_messenger", page_id))
        await close_redis()
        print("OK Redis caches invalidated")
    except Exception as exc:
        print(f"WARN Redis cache invalidate skipped: {exc}")

    await close_pool()

    print("OK social channels seeded")
    print(f"  business_id = {business_id} ({biz['name']})")
    for row in seeded:
        print(
            f"  {row['channel']} account={row['channel_account_id']} "
            f"active={row['is_active']} row={row['id']}"
        )
    print()
    print("Meta app checklist:")
    print("  - Instagram webhook fields: messages, comments")
    print("  - Page webhook fields: messages, feed")
    print("  - Restart python -m mcp_servers.meta_mcp.server")
    print("  - WhatsApp remains on the same callback URL")
    print("  - Run python -m scripts.refresh_meta_tokens to record token expiry")
    print("    (seeded rows have token_expires_at NULL, so auto-refresh stays off)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
