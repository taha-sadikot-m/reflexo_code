#!/usr/bin/env python3
"""Smoke-test conversational onboarding routing + progress tools.

Requires DATABASE_URL. GOOGLE_API_KEY + MCP only needed for --live agent turn.

Usage:
    python scripts/smoke_onboarding.py
    python scripts/smoke_onboarding.py --live --text "Hi, my shop is Cool Style"
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
logger = logging.getLogger("smoke_onboarding")


async def _cleanup_business(conn, business_id: str) -> None:
    await conn.execute("DELETE FROM businesses WHERE id = $1::uuid", business_id)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke onboarding progress + optional live turn")
    parser.add_argument("--live", action="store_true", help="Invoke handle_incoming_message")
    parser.add_argument("--text", default="Namaste! Mera business Cool Style Store hai.")
    parser.add_argument("--keep", action="store_true", help="Do not delete the temp business")
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL missing")
        return 1

    from agent.session import resolve_session_type
    from onboarding.flow import complete_onboarding
    from onboarding.progress import (
        OnboardingProgress,
        apply_progress_updates,
        load_onboarding_progress,
        save_onboarding_progress,
    )
    from shared.db import close_pool, init_pool, service_connection

    await init_pool(os.environ["DATABASE_URL"], application_name="smoke-onboarding")

    phone = "+9198" + "".join(str(int(c, 16) % 10) for c in uuid.uuid4().hex[:8])
    business_id: str | None = None

    try:
        async with service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO businesses (
                    name, owner_phone, vertical, plan, plan_status,
                    onboarding_completed, is_active
                )
                VALUES ('Smoke Onboarding Co', $1, 'ecommerce', 'free', 'trial', false, true)
                RETURNING id
                """,
                phone,
            )
            business_id = str(row["id"])

        assert resolve_session_type(is_owner=True, onboarding_completed=False) == "onboarding"
        assert resolve_session_type(is_owner=True, onboarding_completed=True) == "owner"
        assert resolve_session_type(is_owner=False, onboarding_completed=False) == "customer"
        logger.info("routing ok")

        progress = await load_onboarding_progress(business_id)
        assert progress.business_id == business_id
        assert not progress.is_essential_complete()

        apply_progress_updates(
            progress,
            {
                "business_name": "Smoke Onboarding Co",
                "owner_name": "Ravi",
                "what_they_sell": "tshirts",
                "payment_methods": ["UPI", "COD"],
                "business_hours": {"mon": {"open": "10:00", "close": "20:00"}},
                "return_policy": "7 day returns",
                "delivery_info": "2-3 days Mumbai",
                "vertical": "ecommerce",
            },
        )
        await save_onboarding_progress(business_id, progress)
        progress = await load_onboarding_progress(business_id)
        assert progress.is_essential_complete(), progress.get_required_remaining()
        logger.info("progress save/load ok (%s%%)", progress.get_completion_percentage())

        # Incomplete complete_onboarding should fail — reset one field
        progress.return_policy = None
        progress.skipped_steps = [s for s in progress.skipped_steps if s != "return_policy"]
        await save_onboarding_progress(business_id, progress)
        bad = await complete_onboarding(business_id)
        assert "error" in bad, bad
        assert bad["error"]["code"] == "ONBOARDING_INCOMPLETE"
        logger.info("complete gate ok")

        apply_progress_updates(progress, {"return_policy": "7 day returns"})
        await save_onboarding_progress(business_id, progress)

        # Skip embedding-heavy seed if no GOOGLE_API_KEY
        if not os.getenv("GOOGLE_API_KEY"):
            logger.warning("GOOGLE_API_KEY missing — skipping complete_onboarding seed")
        else:
            # Ensure embeddings client can init (used by seeder)
            from mcp_servers.reflexo_core.search.embeddings import init_embeddings

            await init_embeddings()
            result = await complete_onboarding(business_id)
            assert result.get("status") == "completed", result
            logger.info("complete_onboarding ok seeded=%s", result.get("knowledge_seeded"))

            async with service_connection() as conn:
                flag = await conn.fetchval(
                    "SELECT onboarding_completed FROM businesses WHERE id = $1::uuid",
                    business_id,
                )
            assert flag is True
            assert resolve_session_type(is_owner=True, onboarding_completed=True) == "owner"

        if args.live:
            if not os.getenv("GOOGLE_API_KEY"):
                logger.error("--live requires GOOGLE_API_KEY")
                return 1
            from agent.infrastructure import (
                close_checkpointer,
                close_store,
                init_checkpointer,
                init_store,
            )
            from agent.session import handle_incoming_message
            from agent.settings import get_agent_settings
            from shared.redis_client import close_redis, init_redis

            settings = get_agent_settings()
            await init_redis(settings.redis_url)
            await init_checkpointer()
            await init_store()

            # Need a receiving account — use env or skip
            phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "").strip()
            if not phone_number_id:
                async with service_connection() as conn:
                    crow = await conn.fetchrow(
                        """
                        SELECT channel_account_id FROM business_channels
                        WHERE channel = 'whatsapp' AND is_active = true LIMIT 1
                        """
                    )
                phone_number_id = str(crow["channel_account_id"]) if crow else ""

            if not phone_number_id:
                logger.error("No WhatsApp channel for live smoke")
                return 1

            # Point channel at our temp business for this run
            async with service_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO business_channels (
                        business_id, channel, channel_account_id,
                        channel_display_name, is_active
                    )
                    VALUES ($1::uuid, 'whatsapp', $2, 'Smoke WA', true)
                    ON CONFLICT (business_id, channel, channel_account_id)
                    DO UPDATE SET is_active = true
                    """,
                    business_id,
                    phone_number_id + "-smoke",
                )
                receiving = phone_number_id + "-smoke"

            try:
                # Reset onboarding for live turn if we completed above
                async with service_connection() as conn:
                    await conn.execute(
                        """
                        UPDATE businesses SET onboarding_completed = false
                        WHERE id = $1::uuid
                        """,
                        business_id,
                    )
                    await conn.execute(
                        """
                        UPDATE onboarding_sessions SET is_completed = false, completed_at = NULL
                        WHERE business_id = $1::uuid
                        """,
                        business_id,
                    )

                result = await handle_incoming_message(
                    channel="whatsapp",
                    external_sender_id=phone,
                    message_text=args.text,
                    receiving_account_id=receiving,
                    message_type="text",
                    external_message_id=f"smoke-onboard-{uuid.uuid4().hex[:8]}",
                    skip_send=True,
                )
                logger.info("live result: %s", result)
                if result.get("status") != "responded":
                    return 1
            finally:
                from agent.tools import abandon_mcp_sessions

                abandon_mcp_sessions()
                await close_store()
                await close_checkpointer()
                await close_redis()

        logger.info("smoke_onboarding OK business_id=%s", business_id)
        return 0
    finally:
        if business_id and not args.keep:
            try:
                async with service_connection() as conn:
                    await _cleanup_business(conn, business_id)
            except Exception as exc:
                logger.warning("cleanup failed: %s", exc)
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
