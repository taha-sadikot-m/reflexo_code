"""WhatsApp template synchronization and owner queries."""

from __future__ import annotations

import json
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.tools.helpers import business_exists
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_get
from shared.db import tenant_connection


def _component_content(
    components: list[dict[str, Any]],
) -> tuple[str | None, str | None, str, str | None, list[dict[str, Any]]]:
    header_type = None
    header_content = None
    body_text = ""
    footer_text = None
    buttons: list[dict[str, Any]] = []
    for component in components:
        kind = str(component.get("type") or "").upper()
        if kind == "HEADER":
            header_type = str(component.get("format") or "TEXT").upper()
            header_content = str(component.get("text") or "") or None
        elif kind == "BODY":
            body_text = str(component.get("text") or "")
        elif kind == "FOOTER":
            footer_text = str(component.get("text") or "") or None
        elif kind == "BUTTONS":
            buttons = list(component.get("buttons") or [])
    return header_type, header_content, body_text, footer_text, buttons


async def sync_whatsapp_templates_impl(business_id: str) -> dict[str, Any]:
    creds = await ensure_valid_token(business_id, "whatsapp")
    phone_id = str(creds["phone_number_id"])
    phone = await graph_get(
        phone_id,
        creds["access_token"],
        {"fields": "whatsapp_business_account"},
        business_id=business_id,
        channel="whatsapp",
    )
    waba = phone.get("whatsapp_business_account") or {}
    waba_id = str(waba.get("id") if isinstance(waba, dict) else waba)
    if not waba_id:
        return tool_error(
            "CHANNEL_NOT_CONNECTED",
            "WhatsApp Business Account ID is unavailable",
        )
    response = await graph_get(
        f"{waba_id}/message_templates",
        creds["access_token"],
        {
            "fields": (
                "id,name,status,category,language,components,rejected_reason"
            ),
            "limit": 250,
        },
        business_id=business_id,
        channel="whatsapp",
    )
    templates = list(response.get("data") or [])
    counts = {"approved": 0, "pending": 0, "rejected": 0}
    async with tenant_connection(business_id) as conn:
        for template in templates:
            status = str(template.get("status") or "PENDING").upper()
            if status.lower() in counts:
                counts[status.lower()] += 1
            components = list(template.get("components") or [])
            header_type, header_content, body_text, footer_text, buttons = (
                _component_content(components)
            )
            await conn.execute(
                """
                INSERT INTO whatsapp_templates (
                    business_id, template_name, template_id, language,
                    category, header_type, header_content, body_text,
                    footer_text, buttons, status, rejection_reason
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10::jsonb, $11, $12
                )
                ON CONFLICT (business_id, template_name, language)
                DO UPDATE SET
                    template_id = EXCLUDED.template_id,
                    category = EXCLUDED.category,
                    header_type = EXCLUDED.header_type,
                    header_content = EXCLUDED.header_content,
                    body_text = EXCLUDED.body_text,
                    footer_text = EXCLUDED.footer_text,
                    buttons = EXCLUDED.buttons,
                    status = EXCLUDED.status,
                    rejection_reason = EXCLUDED.rejection_reason,
                    updated_at = now()
                """,
                business_id,
                str(template.get("name") or ""),
                str(template.get("id") or "") or None,
                str(template.get("language") or "en"),
                str(template.get("category") or "UTILITY").upper(),
                header_type,
                header_content,
                body_text,
                footer_text,
                json.dumps(buttons),
                status,
                template.get("rejected_reason"),
            )
    return {"synced": len(templates), **counts}


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def sync_whatsapp_templates(business_id: str) -> dict[str, Any]:
        """Sync WhatsApp template status and content from Meta."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            return await sync_whatsapp_templates_impl(business_id)
        except MetaMCPError as exc:
            return exc.to_dict()

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def list_business_templates(
        business_id: str,
        category: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List locally synced WhatsApp templates."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        async with tenant_connection(business_id) as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM whatsapp_templates
                WHERE business_id = $1::uuid
                  AND ($2::text IS NULL OR category = upper($2))
                  AND ($3::text IS NULL OR status = upper($3))
                ORDER BY
                    CASE category
                        WHEN 'UTILITY' THEN 1
                        WHEN 'AUTHENTICATION' THEN 2
                        ELSE 3
                    END,
                    template_name
                """,
                business_id,
                category,
                status,
            )
        return {"templates": [dict(row) for row in rows], "total_count": len(rows)}
