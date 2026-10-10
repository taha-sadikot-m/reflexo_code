#!/usr/bin/env python3
"""Multi-turn onboarding E2E (skip_send) after signup + progress completion.

Usage:
    python scripts/e2e_onboarding.py
    python scripts/e2e_onboarding.py --keep
    python scripts/e2e_onboarding.py --via-api   # POST http://127.0.0.1:8030/api/signup

Requires DATABASE_URL. --live-agent turns need GOOGLE_API_KEY + Core/Meta MCP.
Default path fills progress via API helpers then one agent turn + complete.
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
logger = logging.getLogger("e2e_onboarding")


ESSENTIAL_UPDATES = {
    "business_name": "E2E Cool Style",
    "owner_name": "Taha",
    "what_they_sell": "western wear and ethnic kurtis",
    "payment_methods": ["UPI", "COD"],
    "business_hours": {"mon-sat": {"open": "10:00", "close": "21:00"}},
    "return_policy": "7 day exchange",
    "delivery_info": "2-3 days Mumbai suburbs",
    "vertical": "ecommerce",
    "skipped_steps": ["offerings", "faqs", "persona", "razorpay", "instagram"],
}


async def _signup_db(phone: str) -> str:
    from onboarding.channel_seed import seed_whatsapp_channel_for_business
    from onboarding.progress import ensure_onboarding_session, load_onboarding_progress, save_onboarding_progress
    from shared.db import service_connection

    async with service_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO businesses (
                name, owner_phone, vertical, plan, plan_status,
                onboarding_completed, is_active
            )
            VALUES ('E2E Business', $1, 'general', 'free', 'trial', false, true)
            RETURNING id
            """,
            phone,
        )
        business_id = str(row["id"])
        seed = await seed_whatsapp_channel_for_business(conn, business_id)

    await ensure_onboarding_session(business_id)
    if seed.get("ok"):
        progress = await load_onboarding_progress(business_id)
        progress.whatsapp_connected = True
        await save_onboarding_progress(business_id, progress)
    logger.info("DB signup business_id=%s channel=%s", business_id, seed.get("ok"))
    return business_id


async def _signup_api(phone: str, base_url: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/signup",
            json={"phone": phone, "language": "en"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"signup API {resp.status_code}: {resp.text}")
        data = resp.json()
        logger.info("API signup: %s", data)
        return str(data["business_id"])


async def main() -> int:
    parser = argparse.ArgumentParser(description="E2E onboarding multi-turn harness")
    parser.add_argument("--keep", action="store_true", help="Keep the temp business")
    parser.add_argument("--via-api", action="store_true", help="Create via POST /api/signup")
    parser.add_argument("--api-url", default="http://127.0.0.1:8030")
    parser.add_argument(
        "--agent-turns",
        action="store_true",
        help="Run handle_incoming_message turns (needs GOOGLE_API_KEY + MCP)",
    )
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL missing")
        return 1

    from onboarding.flow import complete_onboarding
    from onboarding.progress import (
        apply_progress_updates,
        load_onboarding_progress,
        save_onboarding_progress,
    )
    from shared.db import close_pool, init_pool, service_connection

    await init_pool(os.environ["DATABASE_URL"], application_name="e2e-onboarding")

    phone = "+9199" + "".join(str(int(c, 16) % 10) for c in uuid.uuid4().hex[:8])
    business_id: str | None = None

    try:
        if args.via_api:
            business_id = await _signup_api(phone, args.api_url)
        else:
            business_id = await _signup_db(phone)

        progress = await load_onboarding_progress(business_id)
        apply_progress_updates(progress, ESSENTIAL_UPDATES)
        await save_onboarding_progress(business_id, progress)
        progress = await load_onboarding_progress(business_id)
        assert progress.is_essential_complete(), progress.get_required_remaining()
        logger.info("essential progress filled")

        if args.agent_turns:
            if not os.getenv("GOOGLE_API_KEY"):
                logger.error("--agent-turns requires GOOGLE_API_KEY")
                return 1

            from agent.infrastructure import (
                close_checkpointer,
                close_store,
                init_checkpointer,
                init_store,
            )
            from agent.session import handle_incoming_message
            from agent.settings import get_agent_settings
            from agent.tools import abandon_mcp_sessions
            from shared.redis_client import close_redis, init_redis

            settings = get_agent_settings()
            await init_redis(settings.redis_url)
            await init_checkpointer()
            await init_store()

            phone_number_id = (os.getenv("META_PHONE_NUMBER_ID") or "").strip() or "e2e-fake-pnid"
            receiving = f"{phone_number_id}-e2e"
            async with service_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO business_channels (
                        business_id, channel, channel_account_id,
                        channel_display_name, is_active
                    )
                    VALUES ($1::uuid, 'whatsapp', $2, 'E2E WA', true)
                    ON CONFLICT (business_id, channel, channel_account_id)
                    DO UPDATE SET is_active = true
                    """,
                    business_id,
                    receiving,
                )

            turns = [
                "Hi, my shop is E2E Cool Style and I am Taha.",
                "We sell kurtis, take UPI and COD, open 10-9 mon-sat.",
                "7 day exchange, delivery 2-3 days in Mumbai. Done for now.",
            ]
            try:
                for i, text in enumerate(turns):
                    result = await handle_incoming_message(
                        channel="whatsapp",
                        external_sender_id=phone,
                        message_text=text,
                        receiving_account_id=receiving,
                        message_type="text",
                        external_message_id=f"e2e-{uuid.uuid4().hex}",
                        skip_send=True,
                    )
                    logger.info("turn %s status=%s", i + 1, result.get("status"))
                    if result.get("status") not in ("responded", "skipped"):
                        logger.error("unexpected turn result: %s", result)
                        return 1
            finally:
                abandon_mcp_sessions()
                await close_store()
                await close_checkpointer()
                await close_redis()

        if os.getenv("GOOGLE_API_KEY"):
            from mcp_servers.reflexo_core.search.embeddings import init_embeddings

            await init_embeddings()
            result = await complete_onboarding(business_id)
            assert result.get("status") == "completed", result
            logger.info("complete_onboarding ok seeded=%s", result.get("knowledge_seeded"))
        else:
            # Mark complete without embedding seed for CI-lite
            apply_progress_updates(progress, ESSENTIAL_UPDATES)
            await save_onboarding_progress(business_id, progress)
            async with service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE businesses SET onboarding_completed = true, updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    business_id,
                )
                await conn.execute(
                    """
                    UPDATE onboarding_sessions
                    SET is_completed = true, completed_at = now()
                    WHERE business_id = $1::uuid
                    """,
                    business_id,
                )
            logger.warning("GOOGLE_API_KEY missing — marked complete without seed")

        async with service_connection() as conn:
            flag = await conn.fetchval(
                "SELECT onboarding_completed FROM businesses WHERE id = $1::uuid",
                business_id,
            )
        assert flag is True

        from agent.session import resolve_session_type

        assert resolve_session_type(is_owner=True, onboarding_completed=True) == "owner"
        logger.info("e2e_onboarding OK business_id=%s phone=%s", business_id, phone)
        return 0
    finally:
        if business_id and not args.keep:
            try:
                async with service_connection() as conn:
                    await conn.execute(
                        "DELETE FROM businesses WHERE id = $1::uuid", business_id
                    )
            except Exception as exc:
                logger.warning("cleanup failed: %s", exc)
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
