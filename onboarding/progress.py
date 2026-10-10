"""Onboarding progress model and persistence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared.db import service_connection, tenant_connection


APPEND_FIELDS = {"entity_names", "faq_topics", "document_summaries"}
APPEND_DEDUP_FIELDS = {"entity_types_used", "business_context_keys", "skipped_steps"}
INCREMENT_FIELDS = {"entities_added", "faqs_added", "documents_processed"}


@dataclass
class OnboardingProgress:
    """Tracks what has been collected during onboarding (JSONB in onboarding_sessions)."""

    business_id: str = ""

    business_name: str | None = None
    vertical: str | None = None
    owner_name: str | None = None

    what_they_sell: str | None = None
    target_customers: str | None = None

    business_hours: dict | None = None
    address: dict | None = None

    return_policy: str | None = None
    delivery_info: str | None = None
    payment_methods: list[str] | None = None

    entities_added: int = 0
    entity_names: list[str] = field(default_factory=list)
    entity_types_used: list[str] = field(default_factory=list)

    documents_processed: int = 0
    document_summaries: list[str] = field(default_factory=list)

    faqs_added: int = 0
    faq_topics: list[str] = field(default_factory=list)

    business_context_keys: list[str] = field(default_factory=list)

    persona_name: str | None = None
    persona_tone: str | None = None
    greeting_message: str | None = None

    whatsapp_connected: bool = False
    instagram_connected: bool = False
    facebook_connected: bool = False
    email_connected: bool = False

    razorpay_connected: bool = False

    started_at: str = ""
    completed_at: str | None = None
    current_step: int = 1
    is_completed: bool = False
    skipped_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, data: dict | str) -> OnboardingProgress:
        if isinstance(data, str):
            data = json.loads(data) if data else {}
        if not isinstance(data, dict):
            data = {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def get_completion_percentage(self) -> int:
        core = [
            self.business_name,
            self.owner_name,
            self.what_they_sell,
            self.payment_methods,
        ]
        conditional = [
            self.business_hours or ("business_hours" in self.skipped_steps),
            self.return_policy or ("return_policy" in self.skipped_steps),
            self.delivery_info or ("delivery_info" in self.skipped_steps),
        ]
        all_fields = core + conditional
        filled = sum(1 for f in all_fields if f)
        return int((filled / len(all_fields)) * 100)

    def get_required_remaining(self) -> list[str]:
        remaining: list[str] = []
        if not self.business_name:
            remaining.append("business_name")
        if not self.owner_name:
            remaining.append("owner_name")
        if not self.what_they_sell:
            remaining.append("what_they_sell")
        if not self.payment_methods:
            remaining.append("payment_methods")
        if not self.business_hours and "business_hours" not in self.skipped_steps:
            remaining.append("business_hours")
        if not self.return_policy and "return_policy" not in self.skipped_steps:
            remaining.append("return_policy")
        if not self.delivery_info and "delivery_info" not in self.skipped_steps:
            remaining.append("delivery_info")
        return remaining

    def get_recommended_remaining(self) -> list[str]:
        remaining: list[str] = []
        if self.entities_added == 0 and "offerings" not in self.skipped_steps:
            remaining.append("offerings")
        if self.faqs_added == 0 and "faqs" not in self.skipped_steps:
            remaining.append("faqs")
        if not self.persona_name and "persona" not in self.skipped_steps:
            remaining.append("persona")
        if not self.razorpay_connected and "razorpay" not in self.skipped_steps:
            remaining.append("razorpay")
        if not self.instagram_connected and "instagram" not in self.skipped_steps:
            remaining.append("instagram")
        return remaining

    def is_essential_complete(self) -> bool:
        return len(self.get_required_remaining()) == 0

    def generate_progress_summary(self) -> str:
        lines: list[str] = []
        lines.append("=== ONBOARDING PROGRESS ===")
        lines.append(f"Completion: {self.get_completion_percentage()}%")
        lines.append("")

        lines.append("COLLECTED:")
        if self.business_name:
            lines.append(f"  - Business name: {self.business_name}")
        if self.owner_name:
            lines.append(f"  - Owner name: {self.owner_name}")
        if self.what_they_sell:
            lines.append(f"  - What they sell/do: {self.what_they_sell}")
        if self.target_customers:
            lines.append(f"  - Target customers: {self.target_customers}")
        if self.vertical:
            lines.append(f"  - Vertical: {self.vertical}")
        if self.business_hours:
            lines.append("  - Business hours: set")
        if self.address:
            city = self.address.get("city", "set") if isinstance(self.address, dict) else "set"
            lines.append(f"  - Address: {city}")
        if self.return_policy:
            lines.append("  - Return policy: set")
        if self.delivery_info:
            lines.append("  - Delivery info: set")
        if self.payment_methods:
            lines.append(f"  - Payment methods: {', '.join(self.payment_methods)}")
        if self.entities_added > 0:
            type_summary = (
                f" (types: {', '.join(self.entity_types_used)})"
                if self.entity_types_used
                else ""
            )
            names = ", ".join(self.entity_names[:5])
            lines.append(f"  - Entities: {self.entities_added} added ({names}){type_summary}")
        if self.documents_processed > 0:
            lines.append(f"  - Documents: {self.documents_processed} processed")
            for summary in self.document_summaries[:3]:
                lines.append(f"    • {summary}")
        if self.business_context_keys:
            lines.append(f"  - Business context: {', '.join(self.business_context_keys)}")
        if self.faqs_added > 0:
            lines.append(f"  - FAQs: {self.faqs_added} added")
        if self.persona_name:
            lines.append(f"  - AI persona: {self.persona_name} ({self.persona_tone or 'friendly'})")
        if self.greeting_message:
            lines.append("  - Greeting: set")
        if self.whatsapp_connected:
            lines.append("  - WhatsApp: connected")
        if self.instagram_connected:
            lines.append("  - Instagram: connected")
        if self.facebook_connected:
            lines.append("  - Facebook Messenger: connected")
        if self.razorpay_connected:
            lines.append("  - Razorpay: connected")

        lines.append("")

        req_remaining = self.get_required_remaining()
        if req_remaining:
            lines.append("STILL NEEDED (required):")
            field_prompts = {
                "business_name": "Business name",
                "owner_name": "Owner's name",
                "what_they_sell": "What the business sells/does",
                "business_hours": "Business hours / availability",
                "return_policy": "Return/refund/cancellation policy",
                "delivery_info": "Delivery / fulfilment information",
                "payment_methods": "Accepted payment methods",
            }
            for f in req_remaining:
                lines.append(f"  - {field_prompts.get(f, f)}")

        rec_remaining = self.get_recommended_remaining()
        if rec_remaining:
            lines.append("RECOMMENDED (can skip):")
            rec_prompts = {
                "offerings": "Key offerings (products, services, packages, etc.)",
                "faqs": "Common customer questions and answers",
                "persona": "AI persona preferences (name, tone, greeting)",
                "razorpay": "Razorpay payment integration",
                "instagram": "Instagram connection",
            }
            for f in rec_remaining:
                lines.append(f"  - {rec_prompts.get(f, f)}")

        if self.skipped_steps:
            lines.append(f"SKIPPED: {', '.join(self.skipped_steps)}")

        if self.is_essential_complete():
            lines.append("")
            lines.append("ALL REQUIRED INFO COLLECTED. You can offer to wrap up onboarding.")
            lines.append(
                "Ask if they want to add more offerings, share documents, add FAQs, "
                "or connect integrations before finishing."
            )

        return "\n".join(lines)


def apply_progress_updates(progress: OnboardingProgress, updates: dict[str, Any]) -> OnboardingProgress:
    """Apply agent/tool updates onto a progress object (mutates and returns it)."""
    for key, value in updates.items():
        if key not in OnboardingProgress.__dataclass_fields__:
            continue
        if key in APPEND_FIELDS:
            existing = getattr(progress, key) or []
            if isinstance(value, list):
                existing.extend(value)
            else:
                existing.append(value)
            setattr(progress, key, existing)
        elif key in APPEND_DEDUP_FIELDS:
            existing = list(getattr(progress, key) or [])
            items = value if isinstance(value, list) else [value]
            for item in items:
                if item not in existing:
                    existing.append(item)
            setattr(progress, key, existing)
        elif key in INCREMENT_FIELDS:
            current = getattr(progress, key, 0) or 0
            try:
                delta = int(value)
            except (TypeError, ValueError):
                delta = 0
            setattr(progress, key, current + delta)
        else:
            setattr(progress, key, value)
    return progress


async def ensure_onboarding_session(business_id: str) -> None:
    """Create an incomplete onboarding_sessions row if none exists."""
    async with service_connection() as conn:
        existing = await conn.fetchval(
            """
            SELECT id FROM onboarding_sessions
            WHERE business_id = $1::uuid AND is_completed = false
            ORDER BY started_at DESC LIMIT 1
            """,
            business_id,
        )
        if existing:
            return
        now = datetime.now(timezone.utc).isoformat()
        progress = OnboardingProgress(
            business_id=business_id,
            started_at=now,
            whatsapp_connected=True,
        )
        await conn.execute(
            """
            INSERT INTO onboarding_sessions (business_id, progress, current_step)
            VALUES ($1::uuid, $2::jsonb, 1)
            """,
            business_id,
            progress.to_json(),
        )


async def load_onboarding_progress(business_id: str) -> OnboardingProgress:
    """Load onboarding progress from the database (creates session if missing)."""
    await ensure_onboarding_session(business_id)

    async with service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT progress, current_step, started_at, is_completed
            FROM onboarding_sessions
            WHERE business_id = $1::uuid AND is_completed = false
            ORDER BY started_at DESC LIMIT 1
            """,
            business_id,
        )

    if not row:
        return OnboardingProgress(
            business_id=business_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            whatsapp_connected=True,
        )

    raw = row["progress"]
    if isinstance(raw, str):
        progress = OnboardingProgress.from_json(raw)
    elif isinstance(raw, dict):
        progress = OnboardingProgress.from_json(raw)
    else:
        # asyncpg may return JSONB as already-parsed object
        progress = OnboardingProgress.from_json(dict(raw) if raw else {})

    progress.business_id = business_id
    progress.current_step = int(row["current_step"] or 1)
    if row["started_at"] and not progress.started_at:
        progress.started_at = row["started_at"].isoformat()
    progress.is_completed = bool(row["is_completed"])
    if not progress.whatsapp_connected:
        progress.whatsapp_connected = True
    return progress


async def save_onboarding_progress(business_id: str, progress: OnboardingProgress) -> None:
    """Save updated progress to the database."""
    progress.business_id = business_id
    async with service_connection() as conn:
        result = await conn.execute(
            """
            UPDATE onboarding_sessions
            SET progress = $2::jsonb,
                current_step = $3,
                last_activity_at = now(),
                reminder_count = CASE WHEN is_paused THEN 0 ELSE reminder_count END,
                is_paused = false
            WHERE business_id = $1::uuid AND is_completed = false
            """,
            business_id,
            progress.to_json(),
            progress.current_step,
        )
        # result like "UPDATE 0" / "UPDATE 1"
        if result.endswith("0"):
            await conn.execute(
                """
                INSERT INTO onboarding_sessions (business_id, progress, current_step)
                VALUES ($1::uuid, $2::jsonb, $3)
                """,
                business_id,
                progress.to_json(),
                progress.current_step,
            )


async def load_onboarding_progress_tenant(business_id: str) -> OnboardingProgress:
    """Load via tenant connection (RLS). Prefer load_onboarding_progress for tools."""
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT progress, current_step, started_at, is_completed
            FROM onboarding_sessions
            WHERE business_id = $1::uuid AND is_completed = false
            ORDER BY started_at DESC LIMIT 1
            """,
            business_id,
        )
    if not row:
        return OnboardingProgress(business_id=business_id)
    raw = row["progress"]
    if isinstance(raw, dict):
        progress = OnboardingProgress.from_json(raw)
    else:
        progress = OnboardingProgress.from_json(raw or "{}")
    progress.business_id = business_id
    progress.current_step = int(row["current_step"] or 1)
    return progress
