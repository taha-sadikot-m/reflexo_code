#!/usr/bin/env python3
"""Link an Instagram/Facebook sender ID to the business owner for recognition.

Usage:
  python scripts/link_owner_channel_identity.py \\
    --business-id <uuid> --channel instagram_dm --external-id <IGSID>

After linking, Meta DMs from that IGSID/PSID resolve as is_owner=true
(see IDENTITY_RESOLUTION_SPEC owner check via identity_map).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


async def _run(business_id: str, channel: str, external_id: str, display_name: str | None) -> int:
    from identity.owner_link import link_owner_channel_identity
    from shared.db import close_pool, init_pool
    from shared.redis_client import close_redis, init_redis

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL missing")
        return 1

    await init_pool(database_url, application_name="link-owner-identity")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    try:
        await init_redis(redis_url)
    except Exception as exc:
        print(f"Redis init failed (continuing; cache invalidate may no-op): {exc}")

    try:
        result = await link_owner_channel_identity(
            business_id,
            channel,
            external_id,
            display_name=display_name,
        )
        print(json.dumps(result, indent=2))
        return 0
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        try:
            await close_redis()
        except Exception:
            pass
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument(
        "--channel",
        required=True,
        choices=[
            "instagram_dm",
            "instagram_comment",
            "facebook_messenger",
            "facebook_comment",
        ],
    )
    parser.add_argument("--external-id", required=True, help="IGSID or PSID from Meta webhook")
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args()
    return asyncio.run(
        _run(args.business_id, args.channel, args.external_id, args.display_name)
    )


if __name__ == "__main__":
    raise SystemExit(main())
