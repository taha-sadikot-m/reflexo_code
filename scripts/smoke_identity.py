#!/usr/bin/env python3
"""Smoke-test identity resolution against Neon seed business."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

SEED_BIZ = "00000000-0000-0000-0000-000000000001"
TEST_PNID = "smoke_wa_phone_number_id"
OWNER_PHONE = "+919999999999"


async def ensure_channel_row() -> None:
    from shared.db import get_pool

    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO business_channels (
            business_id, channel, channel_account_id, channel_display_name, is_active
        ) VALUES (
            $1::uuid, 'whatsapp', $2, 'Smoke WhatsApp', true
        )
        ON CONFLICT (business_id, channel, channel_account_id) DO UPDATE
        SET is_active = true
        """,
        SEED_BIZ,
        TEST_PNID,
    )


async def main() -> int:
    from shared.db import close_pool, init_pool
    from shared.redis_client import close_redis, init_redis
    from identity import resolve_identity, sync_managed_numbers
    from identity.models import IdentityErrorCode, IdentityResolutionError

    database_url = os.getenv("DATABASE_URL")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    if not database_url:
        print("DATABASE_URL missing")
        return 1

    await init_pool(database_url, application_name="identity-smoke")
    try:
        await init_redis(redis_url)
    except Exception as exc:
        print(f"Redis init failed: {exc}")
        await close_pool()
        return 1

    try:
        await ensure_channel_row()
        n = await sync_managed_numbers()
        print(f"OK synced managed numbers={n}")

        owner = await resolve_identity(
            "whatsapp",
            OWNER_PHONE,
            TEST_PNID,
            sender_display_name="Test Owner",
        )
        assert owner.is_owner, owner
        assert owner.customer_id is None
        print(f"OK owner resolve business={owner.business_id}")

        cust_phone = f"+9198{uuid4().hex[:8]}"
        first = await resolve_identity(
            "whatsapp",
            cust_phone,
            TEST_PNID,
            sender_display_name="Smoke Customer",
        )
        assert first.is_owner is False
        assert first.is_new_customer is True
        assert first.customer_id
        print(f"OK new customer id={first.customer_id}")

        second = await resolve_identity(
            "whatsapp",
            cust_phone,
            TEST_PNID,
            sender_display_name="Smoke Customer",
        )
        assert second.is_new_customer is False
        assert second.customer_id == first.customer_id
        print("OK cached/known customer path")

        try:
            await resolve_identity("whatsapp", "+910000000001", "does_not_exist")
            print("FAIL expected UNKNOWN_CHANNEL_ACCOUNT")
            return 1
        except IdentityResolutionError as e:
            assert e.code == IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT
            print("OK unknown channel account")

        print("Identity smoke passed.")
        return 0
    finally:
        await close_redis()
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
