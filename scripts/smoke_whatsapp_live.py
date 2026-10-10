#!/usr/bin/env python3
"""Live WhatsApp credential check (+ optional outbound send).

Prerequisites:
  1. python scripts/seed_whatsapp_channel.py
  2. DATABASE_URL, ENCRYPTION_KEY, REDIS_URL in .env

Usage:
  python scripts/smoke_whatsapp_live.py           # load credentials only
  python scripts/smoke_whatsapp_live.py --send    # also send a test message
                                                 # requires META_TEST_RECIPIENT=+91...
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


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"ERROR: {name} is missing in .env")
        sys.exit(1)
    return value


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test WhatsApp channel credentials")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send a real WhatsApp text to META_TEST_RECIPIENT",
    )
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    business_id = (
        os.getenv("WHATSAPP_SEED_BUSINESS_ID") or DEFAULT_BIZ
    ).strip()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token, init_fernet
    from mcp_servers.meta_mcp.tools.helpers import validate_e164, wa_to_api
    from mcp_servers.meta_mcp.utils.errors import MetaMCPError
    from mcp_servers.meta_mcp.utils.graph_client import graph_post
    from shared.db import close_pool, init_pool
    from shared.redis_client import close_redis, init_redis

    init_fernet(encryption_key)
    await init_pool(database_url, application_name="smoke-whatsapp-live")

    try:
        await init_redis(redis_url)
        print("OK Redis connected")
    except Exception as exc:
        print(f"ERROR Redis required for credential cache path: {exc}")
        await close_pool()
        return 1

    try:
        creds = await ensure_valid_token(business_id, "whatsapp")
    except MetaMCPError as exc:
        print(f"ERROR credential load failed: {exc.code} — {exc.message}")
        print("  Run: python scripts/seed_whatsapp_channel.py")
        await close_redis()
        await close_pool()
        return 1
    except Exception as exc:
        print(f"ERROR credential load failed: {exc}")
        print("  Run: python scripts/seed_whatsapp_channel.py")
        await close_redis()
        await close_pool()
        return 1

    phone_number_id = creds.get("phone_number_id") or creds.get("channel_account_id")
    print("OK credentials loaded from business_channels")
    print(f"  business_id      = {business_id}")
    print(f"  phone_number_id  = {phone_number_id}")
    print(f"  token_expires_at = {creds.get('token_expires_at')}")
    print("  access_token     = (present, not printed)")

    if args.send:
        recipient = (os.getenv("META_TEST_RECIPIENT") or "").strip()
        if not recipient:
            print("ERROR: --send requires META_TEST_RECIPIENT in .env (E.164, e.g. +9198...)")
            await close_redis()
            await close_pool()
            return 1

        try:
            to_e164 = validate_e164(recipient)
            data = await graph_post(
                f"{phone_number_id}/messages",
                creds["access_token"],
                {
                    "messaging_product": "whatsapp",
                    "to": wa_to_api(to_e164),
                    "type": "text",
                    "text": {
                        "body": (
                            "REFLEXO smoke test: WhatsApp channel is working. "
                            "You can ignore this message."
                        )
                    },
                },
            )
            message_id = (data.get("messages") or [{}])[0].get("id")
            print("OK outbound send")
            print(f"  to         = {to_e164}")
            print(f"  message_id = {message_id}")
        except MetaMCPError as exc:
            print(f"ERROR send failed: {exc.code} — {exc.message}")
            print(
                "  Tip: recipient must have messaged the business within 24h, "
                "or use a template for business-initiated messages."
            )
            await close_redis()
            await close_pool()
            return 1
        except Exception as exc:
            print(f"ERROR send failed: {exc}")
            await close_redis()
            await close_pool()
            return 1
    else:
        print()
        print("Skipped outbound send (pass --send to send a test message).")

    print()
    print("Inbound test (manual):")
    print("  1. Keep Meta MCP + ngrok running")
    print("  2. From your phone, message the WhatsApp Business number")
    print(f"  3. Watch Meta MCP logs for publish to incoming:{business_id}")
    print("  4. Health check: GET https://<ngrok-host>/health")

    await close_redis()
    await close_pool()
    print()
    print("smoke_whatsapp_live: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
