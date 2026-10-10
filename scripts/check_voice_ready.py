"""Readiness check for Stage 10 voice (no secrets printed)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


async def _main() -> int:
    from voice.config import get_voice_settings

    settings = get_voice_settings()
    print("voice_configured=", settings.is_configured())
    print("livekit_url_set=", bool(settings.livekit_url))
    print("livekit_key_set=", bool(settings.livekit_api_key))
    print("livekit_secret_set=", bool(settings.livekit_api_secret))
    print("deepgram_key_set=", bool(settings.deepgram_api_key))
    print("google_api_key_set=", _present("GOOGLE_API_KEY"))
    print("meta_phone_number_id_set=", _present("META_PHONE_NUMBER_ID"))

    redis_ok = False
    try:
        from shared.redis_client import init_redis

        r = await init_redis(settings.redis_url)
        redis_ok = bool(await r.ping())
    except Exception as exc:
        print("redis_error=", type(exc).__name__)
    print("redis_ok=", redis_ok)

    db_ok = False
    if settings.database_url:
        try:
            from shared.db import init_pool

            pool = await init_pool(settings.database_url, application_name="voice-ready")
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_ok = True
        except Exception as exc:
            print("db_error=", type(exc).__name__)
    print("db_ok=", db_ok)
    return 0 if settings.is_configured() and redis_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
