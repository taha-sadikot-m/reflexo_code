"""WhatsApp template send helpers used by signup API and Celery reminders."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def send_whatsapp_template(
    business_id: str,
    phone_number: str,
    template_name: str,
    *,
    language_code: str = "en",
    template_params: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send a Meta-approved WhatsApp template via Graph API.

    Uses the business's encrypted WhatsApp channel credentials (same path as Meta MCP).
    Returns ``{status, message_id, ...}`` or ``{status: "skipped"|"error", warning|error}``.
    """
    from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token, init_fernet
    from mcp_servers.meta_mcp.settings import get_settings
    from mcp_servers.meta_mcp.tools.helpers import validate_e164, wa_to_api
    from mcp_servers.meta_mcp.utils.errors import MetaMCPError
    from mcp_servers.meta_mcp.utils.graph_client import graph_post

    settings = get_settings()
    if settings.encryption_key:
        init_fernet(settings.encryption_key)

    try:
        to_e164 = validate_e164(phone_number)
        creds = await ensure_valid_token(business_id, "whatsapp")
        phone_number_id = creds["phone_number_id"]

        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code or "en"},
        }
        if template_params:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in template_params],
                }
            ]

        body = {
            "messaging_product": "whatsapp",
            "to": wa_to_api(to_e164),
            "type": "template",
            "template": template,
        }
        data = await graph_post(
            f"{phone_number_id}/messages",
            creds["access_token"],
            body,
        )
        message_id = (data.get("messages") or [{}])[0].get("id")
        return {
            "status": "sent",
            "message_id": message_id,
            "template": template_name,
            "channel": "whatsapp",
        }
    except MetaMCPError as exc:
        logger.warning("Template send failed (%s): %s", template_name, exc)
        return {
            "status": "error",
            "template": template_name,
            "error": str(exc),
            "warning": (
                f"Template '{template_name}' could not be sent. "
                "Approve the template in WhatsApp Manager or check channel credentials."
            ),
        }
    except Exception as exc:
        logger.exception("Template send failed (%s)", template_name)
        return {
            "status": "error",
            "template": template_name,
            "error": str(exc),
            "warning": (
                f"Template '{template_name}' could not be sent: {exc}. "
                "Business was still created; owner can message the WA number to continue."
            ),
        }
