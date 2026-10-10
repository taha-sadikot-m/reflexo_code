"""WhatsApp Calling API tools."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.tools.helpers import business_exists, validate_e164
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_post
from mcp_servers.meta_mcp.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def initiate_whatsapp_call(
        business_id: str,
        to: str,
        reason: str = "",
        customer_name: str = "",
    ) -> dict[str, Any]:
        """
        Initiate an outbound WhatsApp voice call.
        The voice agent joins while the phone rings and greets the customer the
        moment they answer. Optional reason and customer_name shape that greeting.
        Requires call permission: the customer must have called the business
        recently or accepted a call permission request.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        try:
            from voice.errors import VoiceCallNotAllowedError
            from voice.outbound import place_outbound_call

            to_e164 = validate_e164(to)
            await check_rate_limit(business_id, "whatsapp_send")
            call_id = await place_outbound_call(
                business_id,
                to_e164,
                reason=reason,
                customer_name=customer_name,
            )
            return {
                "call_id": call_id,
                "status": "ringing",
                "channel": "whatsapp_voice",
                "message": (
                    "Call initiated. Waiting for the customer to answer. "
                    "The voice agent will greet them when they pick up."
                ),
            }
        except VoiceCallNotAllowedError as exc:
            return tool_error(
                "CALL_NOT_ALLOWED",
                str(exc),
                "Do not retry. Tell the owner why the call cannot be placed, and "
                "offer to send a WhatsApp message instead.",
            )
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("initiate_whatsapp_call failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def enable_whatsapp_calling(
        business_id: str,
    ) -> dict[str, Any]:
        """
        Enable WhatsApp Calling API on the business phone number (one-time setup).
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        try:
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]

            await graph_post(
                f"{phone_number_id}/settings",
                creds["access_token"],
                {"calling": {"status": "ENABLED"}},
                business_id=business_id,
                channel="whatsapp",
            )
            return {
                "status": "enabled",
                "phone_number_id": phone_number_id,
                "channel": "whatsapp_voice",
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("enable_whatsapp_calling failed")
            return tool_error("MESSAGE_FAILED", str(exc))
