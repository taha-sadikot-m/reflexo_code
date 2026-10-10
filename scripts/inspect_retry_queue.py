#!/usr/bin/env python3
"""Inspect (and optionally purge) queued retry entries.

    python scripts/inspect_retry_queue.py                 # list every entry
    python scripts/inspect_retry_queue.py --purge-simulator  # drop lab entries

A lab entry is one whose receiving_account_id equals its business_id (the shape
build_simulator_inbound_event produces) or that carries simulator/skip_send.
Such entries can never resolve identity on the live path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from shared.redis_client import close_redis, get_redis, init_redis
from shared.redis_keys import RETRY_QUEUE_INDEX_KEY, retry_queue_key


def _is_simulator(entry: dict) -> bool:
    if entry.get("skip_send") or entry.get("simulator"):
        return True
    business_id = str(entry.get("business_id") or "")
    return bool(business_id) and str(entry.get("receiving_account_id") or "") == business_id


async def main() -> int:
    purge = "--purge-simulator" in sys.argv
    await init_redis(os.environ["REDIS_URL"])
    redis = get_redis()
    try:
        raw_ids = await redis.smembers(RETRY_QUEUE_INDEX_KEY) or set()
        business_ids = sorted(
            bid.decode() if isinstance(bid, bytes) else str(bid) for bid in raw_ids
        )
        if not business_ids:
            print("retry index is empty")
            return 0

        for business_id in business_ids:
            key = retry_queue_key(business_id)
            raws = await redis.lrange(key, 0, -1) or []
            print(f"\n{key}  ({len(raws)} entries)")
            keep: list[str] = []
            dropped = 0
            for raw in raws:
                text = raw.decode() if isinstance(raw, bytes) else str(raw)
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError:
                    print("  [unparseable entry]")
                    keep.append(text)
                    continue
                tag = "SIMULATOR" if _is_simulator(entry) else "live"
                print(
                    f"  [{tag}] attempts={entry.get('attempts')} "
                    f"channel={entry.get('channel')} queued_at={entry.get('queued_at')}\n"
                    f"      text={str(entry.get('message_text'))[:80]!r}\n"
                    f"      error={str(entry.get('error'))[:110]}"
                )
                if purge and _is_simulator(entry):
                    dropped += 1
                else:
                    keep.append(text)

            if purge:
                await redis.delete(key)
                for text in keep:
                    await redis.rpush(key, text)
                if keep:
                    await redis.expire(key, 86400)
                else:
                    await redis.srem(RETRY_QUEUE_INDEX_KEY, business_id)
                print(f"  purged {dropped} simulator entr(y/ies), kept {len(keep)}")
        return 0
    finally:
        await close_redis()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
