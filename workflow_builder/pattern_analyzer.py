"""Mine repeated customer messages and suggest inactive Temporal workflows."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from onboarding.celery_app import celery_app
from workflow_builder.decomposer import decompose_workflow
from workflow_builder.deployer import deploy_workflow
from workflow_builder.store import (
    mark_pattern_suggestion,
    pattern_suggestion_exists,
)

logger = logging.getLogger(__name__)

MIN_OCCURRENCES = int(os.getenv("PATTERN_MIN_OCCURRENCES", "3"))
LOOKBACK_DAYS = int(os.getenv("PATTERN_LOOKBACK_DAYS", "7"))

_CHANNEL_TRIGGER = {
    "whatsapp": "whatsapp_message",
    "instagram_dm": "instagram_dm",
    "instagram_comment": "instagram_comment",
    "facebook_messenger": "facebook_message",
    "facebook_comment": "facebook_comment",
    "email": "email_received",
}


def _fingerprint(channel: str, normalized_text: str) -> str:
    return hashlib.sha256(
        f"{channel}:{normalized_text}".encode("utf-8")
    ).hexdigest()


async def find_repeated_patterns(
    business_id: str,
    *,
    min_occurrences: int = MIN_OCCURRENCES,
) -> list[dict[str, Any]]:
    from shared.db import tenant_connection

    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT channel::text AS channel,
                   lower(regexp_replace(trim(content), '\\s+', ' ', 'g'))
                       AS normalized_text,
                   count(*)::int AS occurrence_count
            FROM messages
            WHERE business_id = $1::uuid
              AND role = 'customer'
              AND created_at > now() - ($2::int * interval '1 day')
              AND length(trim(content)) BETWEEN 3 AND 500
            GROUP BY channel, normalized_text
            HAVING count(*) >= $3
            ORDER BY occurrence_count DESC
            LIMIT 20
            """,
            business_id,
            LOOKBACK_DAYS,
            min_occurrences,
        )
    return [dict(row) for row in rows]


async def analyze_business_patterns(
    business_id: str,
    *,
    min_occurrences: int = MIN_OCCURRENCES,
) -> list[dict[str, Any]]:
    """Create at most one new Studio-ready draft for each repeated pattern."""
    suggestions: list[dict[str, Any]] = []
    for pattern in await find_repeated_patterns(
        business_id,
        min_occurrences=min_occurrences,
    ):
        channel = str(pattern.get("channel") or "")
        text = str(pattern.get("normalized_text") or "").strip()
        trigger = _CHANNEL_TRIGGER.get(channel)
        if not trigger or not text:
            continue
        fingerprint = _fingerprint(channel, text)
        if await pattern_suggestion_exists(business_id, fingerprint):
            continue
        prompt = (
            f"Create a simple automation for repeated {channel} customer messages "
            f"containing this phrase: {text!r}. Respond helpfully using available "
            "business context and the correct channel send tool."
        )
        definition = await decompose_workflow(prompt, business_id=business_id)
        definition.trigger_type = trigger
        definition.conditions = [
            {
                "field": "message.text",
                "operator": "contains",
                "value": text,
            }
        ]
        definition.instance_key = "{{customer.id}}"
        definition.nl_description = prompt
        row = await deploy_workflow(
            business_id,
            definition,
            nl_description=prompt,
        )
        workflow_id = str(row["id"])
        await mark_pattern_suggestion(
            business_id,
            workflow_id,
            fingerprint=fingerprint,
            occurrence_count=int(pattern.get("occurrence_count") or 0),
        )
        row["is_active"] = False
        row["pattern_fingerprint"] = fingerprint
        suggestions.append(row)
        await _notify_owner(business_id, row)
    return suggestions


async def _notify_owner(business_id: str, workflow: dict[str, Any]) -> None:
    try:
        from agent.tools import call_mcp_tool
        from api.settings import get_api_settings
        from mcp_servers.reflexo_core.utils.db_helpers import get_business_row
        from workflow_builder.studio import regenerate_studio_link

        business = await get_business_row(business_id)
        owner_phone = str((business or {}).get("owner_phone") or "")
        if not owner_phone:
            return
        studio = await regenerate_studio_link(
            business_id=business_id,
            workflow_id=str(workflow["id"]),
            created_by="pattern_analyzer",
        )
        url = (
            f"{get_api_settings().admin_frontend_origin.rstrip('/')}"
            f"/wf/{studio['token']}"
        )
        await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {
                "business_id": business_id,
                "to": owner_phone,
                "text": (
                    "I found a repeated customer request and prepared an "
                    f"inactive workflow draft: {workflow.get('name')}.\n{url}"
                ),
            },
        )
    except Exception:
        logger.warning(
            "WF_PATTERN owner notification failed business=%s",
            business_id,
            exc_info=True,
        )


async def analyze_all_businesses() -> dict[str, int]:
    from shared.db import service_connection

    async with service_connection() as conn:
        rows = await conn.fetch("SELECT id FROM businesses WHERE is_active = true")
    created = 0
    for row in rows:
        created += len(await analyze_business_patterns(str(row["id"])))
    return {"businesses": len(rows), "suggestions_created": created}


@celery_app.task(name="analyze_workflow_patterns")
def run_pattern_analyzer_task() -> dict[str, int]:
    """Celery-compatible sync entry point."""
    from agent.asyncio_compat import run_async
    from onboarding.reminders import _ensure_runtime

    async def run() -> dict[str, int]:
        await _ensure_runtime()
        return await analyze_all_businesses()

    return run_async(run())
