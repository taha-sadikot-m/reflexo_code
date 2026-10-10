#!/usr/bin/env python3
"""Check that the live WhatsApp loop prerequisites are in place.

Validates env vars, WhatsApp business_channels row, Redis PING, and Core/Meta /health.

Usage:
    python scripts/check_live_whatsapp_ready.py

Exit 0 if all checks pass; non-zero otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

REQUIRED_ENV = (
    "DATABASE_URL",
    "REDIS_URL",
    "GOOGLE_API_KEY",
    "ENCRYPTION_KEY",
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
    "META_SYSTEM_ACCESS_TOKEN",
    "META_PHONE_NUMBER_ID",
)


def _ok(msg: str) -> None:
    print(f"OK   {msg}")


def _fail(msg: str) -> None:
    print(f"FAIL {msg}")


async def _check_http(name: str, url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            _ok(f"{name} health {url}")
            return True
        _fail(f"{name} health {url} → HTTP {resp.status_code}")
        return False
    except Exception as exc:
        _fail(f"{name} health {url} → {exc}")
        return False


async def main() -> int:
    failed = 0

    print("=== Env ===")
    for name in REQUIRED_ENV:
        value = (os.getenv(name) or "").strip()
        if value:
            _ok(f"{name} set")
        else:
            _fail(f"{name} missing")
            failed += 1

    phone_number_id = (os.getenv("META_PHONE_NUMBER_ID") or "").strip()
    business_id = (os.getenv("WHATSAPP_SEED_BUSINESS_ID") or "").strip()
    meta_port = int(os.getenv("META_MCP_PORT") or "8021")
    core_port = int(os.getenv("PORT") or "8020")

    print("\n=== Database (WhatsApp channel) ===")
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if database_url and phone_number_id:
        from shared.db import close_pool, init_pool, service_connection

        await init_pool(database_url, application_name="check-live-whatsapp")
        try:
            async with service_connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT business_id, channel_account_id, is_active
                    FROM business_channels
                    WHERE channel = 'whatsapp'
                      AND channel_account_id = $1
                      AND ($2 = '' OR business_id = NULLIF($2, '')::uuid)
                    ORDER BY is_active DESC, updated_at DESC
                    LIMIT 1
                    """,
                    phone_number_id,
                    business_id,
                )
            if row and row["is_active"]:
                _ok(
                    f"whatsapp channel phone_number_id={phone_number_id} "
                    f"business={row['business_id']}"
                )
            elif row:
                _fail(f"whatsapp channel exists but is_active=false for {phone_number_id}")
                failed += 1
            else:
                _fail(
                    f"no business_channels row for whatsapp / {phone_number_id} "
                    "(run: python scripts/seed_whatsapp_channel.py)"
                )
                failed += 1
        finally:
            await close_pool()
    else:
        _fail("skipped DB check (DATABASE_URL or META_PHONE_NUMBER_ID missing)")
        failed += 1

    print("\n=== Redis ===")
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if redis_url:
        from shared.redis_client import close_redis, get_redis, init_redis

        try:
            await init_redis(redis_url)
            pong = await get_redis().ping()
            if pong:
                _ok("Redis PING")
            else:
                _fail("Redis PING returned falsy")
                failed += 1
        except Exception as exc:
            _fail(f"Redis -> {exc}")
            failed += 1
        finally:
            await close_redis()
    else:
        _fail("REDIS_URL missing")
        failed += 1

    print("\n=== HTTP health ===")
    if not await _check_http("Core MCP", f"http://127.0.0.1:{core_port}/health"):
        failed += 1
        print("     start with: python -m mcp_servers.reflexo_core.server")
    if not await _check_http("Meta MCP", f"http://127.0.0.1:{meta_port}/health"):
        failed += 1
        print("     start with: python -m mcp_servers.meta_mcp.server")

    print("\n=== Summary ===")
    if failed:
        print(f"{failed} check(s) failed")
        print(
            "\nNext after fixes:\n"
            "  1. python scripts/seed_whatsapp_channel.py\n"
            "  2. Start Core, Meta, agent.worker\n"
            f"  3. ngrok http {meta_port}\n"
            "  4. Meta App webhook -> https://<ngrok>/webhook + META_VERIFY_TOKEN\n"
            "  5. Message the business WhatsApp number from your phone"
        )
        return 1

    print("All checks passed - ready for live WhatsApp")
    print(
        f"\nTunnel Meta webhook: ngrok http {meta_port}\n"
        "Meta callback URL: https://<ngrok-host>/webhook\n"
        "Then start agent.worker if not already running and send a WhatsApp message."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
