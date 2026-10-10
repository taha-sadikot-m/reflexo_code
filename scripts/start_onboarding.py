#!/usr/bin/env python3
"""Bootstrap a business for conversational onboarding over WhatsApp.

Usage:
    python scripts/start_onboarding.py --phone +919876543210
    python scripts/start_onboarding.py --phone +919876543210 --name "Cool Style"
    python scripts/start_onboarding.py --phone +919876543210 --force-new
    python scripts/start_onboarding.py --phone +919876543210 --seed-whatsapp

Creates (or resumes) a business with onboarding_completed=false and an
onboarding_sessions row. Optionally copies WhatsApp channel credentials from
the seed mall business / META_* env.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")


def _normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]+", "", phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


async def _seed_whatsapp_channel(conn, business_id: str) -> bool:
    """Copy active WhatsApp channel from seed business or create from env."""
    from onboarding.channel_seed import seed_whatsapp_channel_for_business

    result = await seed_whatsapp_channel_for_business(conn, business_id)
    if not result.get("ok"):
        print(f"WARNING: {result.get('error')}")
        return False
    print(f"  WhatsApp channel seeded: phone_number_id={result.get('phone_number_id')}")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Start / resume conversational onboarding")
    parser.add_argument("--phone", required=True, help="Owner phone in E.164 (+91XXXXXXXXXX)")
    parser.add_argument("--name", default="New Business", help="Placeholder business name")
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="Create a new business even if this phone already completed onboarding",
    )
    parser.add_argument(
        "--seed-whatsapp",
        action="store_true",
        help="Attach WhatsApp channel credentials so Meta webhooks resolve to this business",
    )
    args = parser.parse_args()

    phone = _normalize_phone(args.phone)
    if not PHONE_RE.match(phone):
        print(f"ERROR: Invalid Indian mobile: {phone} (expected +91[6-9]XXXXXXXXX)")
        return 1

    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL missing")
        return 1

    from shared.db import close_pool, init_pool, service_connection
    from onboarding.progress import ensure_onboarding_session

    await init_pool(database_url, application_name="start-onboarding")

    try:
        async with service_connection() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, name, onboarding_completed
                FROM businesses
                WHERE owner_phone = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                phone,
            )

            if existing and not existing["onboarding_completed"]:
                business_id = str(existing["id"])
                print(f"Resuming incomplete onboarding for {phone}")
                print(f"  business_id={business_id} name={existing['name']}")
            elif existing and existing["onboarding_completed"] and not args.force_new:
                print(
                    f"ERROR: Business already onboarded for {phone} "
                    f"(id={existing['id']}). Use --force-new to create another."
                )
                return 1
            else:
                if existing and args.force_new:
                    # Soft-deactivate old owner_phone uniqueness for identity:
                    # keep old row but clear phone so new business owns the number.
                    await conn.execute(
                        """
                        UPDATE businesses
                        SET owner_phone = owner_phone || '_archived_' || id::text,
                            updated_at = now()
                        WHERE id = $1::uuid
                        """,
                        existing["id"],
                    )
                row = await conn.fetchrow(
                    """
                    INSERT INTO businesses (
                        name, owner_phone, owner_name, vertical,
                        plan, plan_status, onboarding_completed, is_active
                    )
                    VALUES ($1, $2, NULL, 'general', 'free', 'trial', false, true)
                    RETURNING id, name
                    """,
                    args.name,
                    phone,
                )
                business_id = str(row["id"])
                print(f"Created business for onboarding")
                print(f"  business_id={business_id} name={row['name']} phone={phone}")

            await ensure_onboarding_session(business_id)

            # Ensure progress marks WhatsApp as connected when seeding channel
            if args.seed_whatsapp:
                ok = await _seed_whatsapp_channel(conn, business_id)
                if ok:
                    from onboarding.progress import load_onboarding_progress, save_onboarding_progress

                    progress = await load_onboarding_progress(business_id)
                    progress.whatsapp_connected = True
                    await save_onboarding_progress(business_id, progress)

        print()
        print("Next steps:")
        print("  1. python scripts/apply_migrations.py   # if 013 not applied yet")
        print("  2. Start Core + Meta + agent.worker")
        print("  3. Message the WhatsApp business number FROM this owner phone")
        print("  4. Agent should use session_type=onboarding")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
