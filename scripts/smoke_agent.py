"""Smoke test for the ReAct agent session path.

Requires DATABASE_URL, REDIS_URL, GOOGLE_API_KEY in .env.
Optionally hits live Core/Meta MCP when REFLEXO_CORE_MCP_URL / META_MCP_URL are up.

Usage:
    python scripts/smoke_agent.py
    python scripts/smoke_agent.py --send   # also attempt Meta send (needs WA channel)
    python scripts/smoke_agent.py --text "What are your hours?"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("smoke_agent")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test ReAct agent handle_incoming_message")
    parser.add_argument("--text", default="Hi, what are your business hours?")
    parser.add_argument(
        "--business-id",
        default=os.getenv("WHATSAPP_SEED_BUSINESS_ID", "00000000-0000-0000-0000-000000000001"),
    )
    parser.add_argument("--sender", default=os.getenv("META_TEST_RECIPIENT", "+919876543210"))
    parser.add_argument(
        "--phone-number-id",
        default=os.getenv("META_PHONE_NUMBER_ID", ""),
        help="Receiving WhatsApp phone_number_id (required for identity)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the reply via Meta MCP (default: skip_send)",
    )
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL missing")
        return 1
    if not os.getenv("GOOGLE_API_KEY"):
        logger.error("GOOGLE_API_KEY missing")
        return 1

    from agent.infrastructure import (
        close_checkpointer,
        close_store,
        init_checkpointer,
        init_store,
    )
    from agent.session import handle_incoming_message
    from agent.settings import get_agent_settings
    from shared.db import close_pool, init_pool, service_connection
    from shared.redis_client import close_redis, init_redis

    settings = get_agent_settings()
    await init_pool(settings.database_url, application_name="smoke-agent")
    await init_redis(settings.redis_url)
    await init_checkpointer()
    await init_store()

    phone_number_id = args.phone_number_id
    if not phone_number_id:
        async with service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT channel_account_id
                FROM business_channels
                WHERE business_id = $1::uuid AND channel = 'whatsapp' AND is_active = true
                LIMIT 1
                """,
                args.business_id,
            )
        if not row:
            logger.error("No whatsapp business_channels row for business %s", args.business_id)
            await close_store()
            await close_checkpointer()
            await close_redis()
            await close_pool()
            return 1
        phone_number_id = str(row["channel_account_id"])

    logger.info(
        "Invoking agent business=%s sender=%s phone_number_id=%s skip_send=%s",
        args.business_id,
        args.sender,
        phone_number_id,
        not args.send,
    )

    try:
        result = await handle_incoming_message(
            channel="whatsapp",
            external_sender_id=args.sender,
            message_text=args.text,
            receiving_account_id=phone_number_id,
            message_type="text",
            external_message_id=f"smoke-agent-{uuid.uuid4()}",
            skip_send=not args.send,
        )
    finally:
        from agent.tools import abandon_mcp_sessions

        abandon_mcp_sessions()
        try:
            await close_store()
        except Exception as exc:
            logger.warning("close_store: %s", exc)
        try:
            await close_checkpointer()
        except Exception as exc:
            logger.warning("close_checkpointer: %s", exc)
        try:
            await close_redis()
        except Exception as exc:
            logger.warning("close_redis: %s", exc)
        try:
            await close_pool()
        except Exception as exc:
            logger.warning("close_pool: %s", exc)

    logger.info("Result: %s", result)
    try:
        from langfuse import Langfuse

        settings = get_agent_settings()
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            ).flush()
    except Exception as exc:
        logger.debug("Langfuse flush skipped: %s", exc)
    meta = result.get("ai_metadata") or {}
    print(
        "META",
        {
            "status": result.get("status"),
            "specialist": meta.get("specialist"),
            "route_method": meta.get("route_method"),
            "delegates": meta.get("delegates"),
            "handoff_chain": meta.get("handoff_chain"),
            "latency_ms": result.get("latency_ms"),
            "is_owner": meta.get("is_owner"),
            "tokens": meta.get("tokens"),
            "estimated_cost_usd": meta.get("estimated_cost_usd"),
        },
    )
    if result.get("status") not in ("responded", "limit_reached", "bot_detected"):
        return 1
    if result.get("status") == "responded" and not result.get("response_text"):
        return 1
    print(result.get("response_text") or result.get("status"))
    return 0


if __name__ == "__main__":
    from agent.asyncio_compat import run_async

    raise SystemExit(run_async(main()))
