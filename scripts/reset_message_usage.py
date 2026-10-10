#!/usr/bin/env python3
"""Reset message quota counters for a business (dev / after limit false-positive)."""

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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional new message_limit (default: keep existing)",
    )
    args = parser.parse_args()

    from shared.db import close_pool, init_pool, service_connection
    from shared.redis_client import close_redis, init_redis
    from shared.redis_keys import business_config_key, usage_key

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL missing")
        return 1

    await init_pool(database_url, application_name="reset-message-usage")
    await init_redis(os.getenv("REDIS_URL", "redis://localhost:6379"))

    try:
        async with service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT messages_used, message_limit, billing_cycle_start
                FROM businesses WHERE id = $1::uuid
                """,
                args.business_id,
            )
            if not row:
                print(f"No business {args.business_id}")
                return 1

            cycle = row["billing_cycle_start"]
            cycle_key = cycle.date().isoformat() if hasattr(cycle, "date") else str(cycle)[:10]

            if args.limit is not None:
                await conn.execute(
                    """
                    UPDATE businesses
                    SET messages_used = 0, message_limit = $2, updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    args.business_id,
                    args.limit,
                )
            else:
                await conn.execute(
                    """
                    UPDATE businesses SET messages_used = 0, updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    args.business_id,
                )

        from shared.redis_client import get_redis

        redis = get_redis()
        deleted = await redis.delete(
            usage_key(args.business_id, cycle_key),
            business_config_key(args.business_id),
        )
        print(
            f"OK reset business={args.business_id} "
            f"messages_used=0 cycle={cycle_key} redis_keys_deleted={deleted}"
        )
        if args.limit is not None:
            print(f"   message_limit set to {args.limit}")
        return 0
    finally:
        await close_redis()
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
