"""POST /api/signup — landing-page entry into conversational onboarding."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from onboarding.channel_seed import seed_whatsapp_channel_for_business
from onboarding.progress import (
    ensure_onboarding_session,
    load_onboarding_progress,
    save_onboarding_progress,
)
from onboarding.whatsapp_send import send_whatsapp_template
from shared.db import service_connection

logger = logging.getLogger(__name__)

router = APIRouter()

PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")


class SignupRequest(BaseModel):
    phone: str
    referral_code: str | None = None
    language: str = Field(default="en", description="Welcome template language: en or hi")

    @field_validator("phone")
    @classmethod
    def validate_indian_phone(cls, v: str) -> str:
        cleaned = re.sub(r"[\s\-\(\)]+", "", (v or "").strip())
        if not cleaned.startswith("+"):
            cleaned = "+" + cleaned
        if not PHONE_RE.match(cleaned):
            raise ValueError("Invalid Indian mobile number. Use +91XXXXXXXXXX format.")
        return cleaned

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        lang = (v or "en").lower()
        if lang not in ("en", "hi"):
            return "en"
        return lang


async def _resolve_referrer(conn, referral_code: str):
    referrer = await conn.fetchrow(
        """
        SELECT business_id FROM referrals
        WHERE referral_code = $1
        ORDER BY created_at ASC
        LIMIT 1
        """,
        referral_code,
    )
    if referrer:
        return referrer["business_id"]
    referrer = await conn.fetchrow(
        """
        SELECT business_id FROM onboarding_sessions
        WHERE referral_code = $1
        ORDER BY created_at ASC
        LIMIT 1
        """,
        referral_code,
    )
    return referrer["business_id"] if referrer else None


@router.post("/api/signup")
async def signup(req: SignupRequest) -> dict[str, Any]:
    """
    Create or resume an incomplete business, seed WhatsApp channel, send welcome template.
    """
    async with service_connection() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, onboarding_completed, name
            FROM businesses
            WHERE owner_phone = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            req.phone,
        )

        resumed = False
        if existing and existing["onboarding_completed"]:
            raise HTTPException(
                status_code=409,
                detail="Business already registered with this phone number.",
            )

        if existing and not existing["onboarding_completed"]:
            business_id = str(existing["id"])
            resumed = True
            logger.info("Resuming incomplete onboarding business_id=%s", business_id)
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO businesses (
                    name, owner_phone, owner_name, vertical,
                    plan, plan_status, onboarding_completed, is_active
                )
                VALUES (
                    'New Business', $1, NULL, 'general',
                    'free', 'trial', false, true
                )
                RETURNING id
                """,
                req.phone,
            )
            business_id = str(row["id"])

        seed = await seed_whatsapp_channel_for_business(conn, business_id)

    await ensure_onboarding_session(business_id)

    if req.referral_code and not resumed:
        async with service_connection() as conn:
            referred_by = await _resolve_referrer(conn, req.referral_code)
            await conn.execute(
                """
                INSERT INTO referrals (business_id, referral_code, referred_by_business_id)
                VALUES ($1::uuid, $2, $3::uuid)
                """,
                business_id,
                req.referral_code,
                referred_by,
            )
            await conn.execute(
                """
                UPDATE onboarding_sessions
                SET referral_code = $2,
                    referral_business_id = $3::uuid,
                    last_activity_at = now()
                WHERE business_id = $1::uuid AND is_completed = false
                """,
                business_id,
                req.referral_code,
                referred_by,
            )

    if seed.get("ok"):
        progress = await load_onboarding_progress(business_id)
        progress.whatsapp_connected = True
        await save_onboarding_progress(business_id, progress)

    template_name = (
        "onboarding_welcome_hi" if req.language == "hi" else "onboarding_welcome"
    )
    language_code = "hi" if req.language == "hi" else "en"

    if not seed.get("ok"):
        template_result: dict[str, Any] = {
            "status": "skipped",
            "warning": seed.get("error")
            or "WhatsApp channel not seeded; welcome template not sent.",
        }
    else:
        template_result = await send_whatsapp_template(
            business_id,
            req.phone,
            template_name,
            language_code=language_code,
        )

    status = "welcome_message_sent"
    if template_result.get("status") != "sent":
        status = "created_template_pending" if not resumed else "resumed_template_pending"

    return {
        "business_id": business_id,
        "status": status,
        "resumed": resumed,
        "whatsapp_channel": seed,
        "welcome_template": template_result,
    }
