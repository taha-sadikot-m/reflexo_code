#!/usr/bin/env python3
"""Wipe the public schema so Reflexo can start from a clean DATABASE_SCHEMA.

Requires --confirm. Optionally flushes Reflexo Redis key prefixes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# Allow running from repo root without installing the package
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

FLUSH_PREFIXES = (
    "identity:",
    "business:",
    "dynamic:",
    "conv_velocity:",
    "conv_depth:",
    "reflexo:",
    "usage:",
)


async def wipe_postgres(database_url: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        print("Dropping schema public CASCADE...")
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await conn.execute("GRANT ALL ON SCHEMA public TO public")
        await conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        print("Public schema recreated.")
    finally:
        await conn.close()


async def flush_redis(redis_url: str) -> None:
    from redis.asyncio import from_url

    client = from_url(redis_url, decode_responses=True)
    try:
        deleted = 0
        for prefix in FLUSH_PREFIXES:
            cursor = 0
            pattern = f"{prefix}*"
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    deleted += await client.delete(*keys)
                if cursor == 0:
                    break
        print(f"Flushed Redis keys matching Reflexo prefixes ({deleted} deleted).")
    finally:
        await client.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Reset Reflexo database (destructive).")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Acknowledge that all public-schema data will be destroyed.",
    )
    parser.add_argument(
        "--flush-redis",
        action="store_true",
        help="Also delete Reflexo-prefixed keys from REDIS_URL.",
    )
    args = parser.parse_args()

    if not args.confirm:
        print("Refusing to run without --confirm (this destroys all public-schema data).")
        return 1

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
        return 1

    # Mask password in log
    safe_url = database_url.split("@")[-1] if "@" in database_url else database_url
    print(f"Target database host: {safe_url}")

    await wipe_postgres(database_url)

    if args.flush_redis:
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            print("REDIS_URL is not set; skipping Redis flush.")
        else:
            await flush_redis(redis_url)

    print("Reset complete. Next: python scripts/apply_migrations.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
