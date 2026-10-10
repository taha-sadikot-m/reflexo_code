#!/usr/bin/env python3
"""Seed encrypted WhatsApp credentials into business_channels.

Required env:
  DATABASE_URL
  ENCRYPTION_KEY
  META_SYSTEM_ACCESS_TOKEN   — Graph API access token
  META_PHONE_NUMBER_ID       — WhatsApp Phone number ID from Meta API Setup

Optional:
  REDIS_URL
  WHATSAPP_SEED_BUSINESS_ID  — default seed business UUID
  META_WHATSAPP_DISPLAY_NAME — display label for the channel row
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

DEFAULT_BIZ = "00000000-0000-0000-0000-000000000001"

# A USER token is revoked whenever the same admin completes a fresh Facebook
# Login, which is exactly what connecting Instagram or a Page does. That is how
# WhatsApp started returning (#131005) Access denied mid-session.
DURABLE_TOKEN_TYPES = ("SYSTEM_USER",)


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"ERROR: {name} is missing in .env")
        if name == "META_PHONE_NUMBER_ID":
            print(
                "  Find it in Meta Developer Console → WhatsApp → API Setup → Phone number ID"
            )
        elif name == "META_SYSTEM_ACCESS_TOKEN":
            print(
                "  Use a System User permanent token or the temporary token from API Setup"
            )
        elif name == "ENCRYPTION_KEY":
            print(
                '  Generate with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        sys.exit(1)
    return value


async def inspect_token(access_token: str) -> dict[str, object]:
    """Ask Graph what this token is. Returns {} when the app creds are absent."""
    import httpx

    app_id = (os.getenv("META_APP_ID") or "").strip()
    app_secret = (os.getenv("META_APP_SECRET") or "").strip()
    if not (app_id and app_secret):
        return {}

    version = (os.getenv("META_API_VERSION") or "v22.0").strip()
    verify = (os.getenv("META_HTTP_VERIFY") or "true").strip().lower() not in ("false", "0", "no")
    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        response = await client.get(
            f"https://graph.facebook.com/{version}/debug_token",
            params={"input_token": access_token},
            headers={"Authorization": f"Bearer {app_id}|{app_secret}"},
        )
        response.raise_for_status()
        return (response.json() or {}).get("data") or {}


async def preflight(access_token: str, *, force: bool) -> bool:
    """Refuse to seed a token that cannot send. Never prints token material."""
    try:
        info = await inspect_token(access_token)
    except Exception as exc:
        print(f"WARN could not verify token via debug_token: {exc}")
        return True

    if not info:
        print("WARN META_APP_ID / META_APP_SECRET unset — skipping token verification")
        return True

    token_type = str(info.get("type") or "unknown")
    scopes = list(info.get("scopes") or [])
    print(f"  token_type = {token_type}")

    if not info.get("is_valid"):
        reason = (info.get("error") or {}).get("message") or "token is not valid"
        print(f"ERROR token is not usable: {reason}")
        return False

    if "whatsapp_business_messaging" not in scopes:
        print("ERROR token lacks whatsapp_business_messaging — sends will be denied")
        return False

    if token_type not in DURABLE_TOKEN_TYPES:
        print(f"WARN {token_type} tokens are revoked when the same admin logs in again.")
        print("     Business Settings -> System Users -> Generate token (no expiry),")
        print("     assign the WABA and whatsapp_business_messaging.")
        if not force:
            print("     Re-run with --force to seed it anyway.")
            return False

    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed even when the token is not a permanent System User token",
    )
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    access_token = _require("META_SYSTEM_ACCESS_TOKEN")
    phone_number_id = _require("META_PHONE_NUMBER_ID")
    business_id = (
        os.getenv("WHATSAPP_SEED_BUSINESS_ID") or DEFAULT_BIZ
    ).strip()
    display_name = (
        os.getenv("META_WHATSAPP_DISPLAY_NAME") or "Dev WhatsApp"
    ).strip()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    from mcp_servers.meta_mcp.auth.tokens import encrypt_token, init_fernet
    from mcp_servers.meta_mcp.redis_keys import meta_creds_key
    from shared.db import close_pool, init_pool, service_connection
    from shared.redis_client import close_redis, get_redis, init_redis
    from shared.redis_keys import channel_account_key

    if not await preflight(access_token, force=args.force):
        return 1

    init_fernet(encryption_key)
    encrypted = encrypt_token(access_token)

    await init_pool(database_url, application_name="seed-whatsapp-channel")

    async with service_connection() as conn:
        biz = await conn.fetchrow(
            "SELECT id, name FROM businesses WHERE id = $1::uuid",
            business_id,
        )
        if not biz:
            print(f"ERROR: business {business_id} not found. Run migrations/seed first.")
            await close_pool()
            return 1

        row = await conn.fetchrow(
            """
            INSERT INTO business_channels (
                business_id, channel, channel_account_id, channel_display_name,
                access_token_encrypted, is_active, error_message, updated_at
            ) VALUES (
                $1::uuid, 'whatsapp', $2, $3, $4, true, NULL, now()
            )
            ON CONFLICT (business_id, channel, channel_account_id) DO UPDATE
            SET access_token_encrypted = EXCLUDED.access_token_encrypted,
                channel_display_name = EXCLUDED.channel_display_name,
                is_active = true,
                error_message = NULL,
                updated_at = now()
            RETURNING id, channel_account_id, is_active
            """,
            business_id,
            phone_number_id,
            display_name,
            encrypted,
        )

    # Best-effort Redis cache invalidation
    try:
        await init_redis(redis_url)
        redis = get_redis()
        await redis.delete(meta_creds_key(business_id, "whatsapp"))
        await redis.delete(channel_account_key("whatsapp", phone_number_id))
        await close_redis()
        print("OK Redis caches invalidated")
    except Exception as exc:
        print(f"WARN Redis cache invalidate skipped: {exc}")

    await close_pool()

    print("OK WhatsApp channel seeded")
    print(f"  business_id        = {business_id} ({biz['name']})")
    print(f"  channel_account_id = {row['channel_account_id']}")
    print(f"  channel_row_id     = {row['id']}")
    print(f"  is_active          = {row['is_active']}")
    print("  (access token stored encrypted — not printed)")
    print()
    print("Next:")
    print("  python scripts/smoke_whatsapp_live.py")
    print("  python scripts/smoke_whatsapp_live.py --send   # needs META_TEST_RECIPIENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
