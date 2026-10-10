"""Meta WhatsApp Calling Graph actions (no token logging)."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.utils.errors import MessageFailedError
from mcp_servers.meta_mcp.utils.graph_client import graph_post
from voice.errors import VoiceCallNotAllowedError, VoiceSdpError

logger = logging.getLogger(__name__)


async def pre_accept_call(
    phone_number_id: str,
    call_id: str,
    access_token: str,
    *,
    business_id: str,
    sdp_answer: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "pre_accept",
    }
    if sdp_answer:
        body["session"] = {"sdp_type": "answer", "sdp": sdp_answer}
    logger.info(
        "VOICE_GRAPH pre_accept call_id=%s phone_number_id=%s has_sdp=%s",
        call_id,
        phone_number_id,
        bool(sdp_answer),
    )
    try:
        return await graph_post(
            f"{phone_number_id}/calls",
            access_token,
            body,
            business_id=business_id,
            channel="whatsapp",
        )
    except MessageFailedError as exc:
        if exc.extra.get("graph_code") == 138008:
            raise VoiceSdpError("Meta rejected the pre-accept SDP answer") from exc
        raise


async def accept_call(
    phone_number_id: str,
    call_id: str,
    access_token: str,
    sdp_answer: str,
    *,
    business_id: str,
) -> dict[str, Any]:
    logger.info(
        "VOICE_GRAPH accept call_id=%s phone_number_id=%s",
        call_id,
        phone_number_id,
    )
    try:
        return await graph_post(
            f"{phone_number_id}/calls",
            access_token,
            {
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": "accept",
                "session": {"sdp_type": "answer", "sdp": sdp_answer},
            },
            business_id=business_id,
            channel="whatsapp",
        )
    except MessageFailedError as exc:
        if exc.extra.get("graph_code") == 138008:
            raise VoiceSdpError("Meta rejected the accept SDP answer") from exc
        raise


async def send_whatsapp_text(
    *,
    business_id: str,
    to: str,
    text: str,
) -> dict[str, Any]:
    """Send a WhatsApp Cloud text message during a live call (24h window)."""
    from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
    from mcp_servers.meta_mcp.tools.helpers import validate_e164, wa_to_api

    creds = await ensure_valid_token(business_id, "whatsapp")
    phone_number_id = creds["phone_number_id"]
    to_e164 = validate_e164(to)
    logger.info("VOICE_GRAPH whatsapp_text to=%s chars=%s", to_e164, len(text))
    return await graph_post(
        f"{phone_number_id}/messages",
        creds["access_token"],
        {
            "messaging_product": "whatsapp",
            "to": wa_to_api(to_e164),
            "type": "text",
            "text": {"body": text},
        },
        business_id=business_id,
        channel="whatsapp",
    )


# Business-initiated calling rejections that no retry will fix.
BIC_ERRORS: dict[int, str] = {
    138000: (
        "Calling is not enabled on this WhatsApp number. "
        "Run enable_whatsapp_calling first."
    ),
    138001: (
        "This number cannot receive WhatsApp calls — the recipient is not on "
        "WhatsApp, has not accepted the calling terms, or uses an unsupported app."
    ),
    138003: "A call with this customer is already in progress.",
    138005: "WhatsApp call rate limit reached. Try again shortly.",
    138006: (
        "No approved call permission for this customer. They must call the "
        "business or accept a call permission request before we can call them."
    ),
    138012: "Daily business-initiated call limit reached for this customer.",
    138013: (
        "Business-initiated calling is not available for this business number's "
        "country."
    ),
    138014: "WhatsApp calling is temporarily disabled for this number.",
}


async def initiate_call(
    phone_number_id: str,
    access_token: str,
    to_phone: str,
    sdp_offer: str,
    *,
    business_id: str,
) -> dict[str, Any]:
    """Place a business-initiated WhatsApp call.

    Meta requires our SDP offer up front and returns its answer on the `calls`
    webhook, so this is the mirror image of the inbound accept flow.
    """
    from mcp_servers.meta_mcp.tools.helpers import wa_to_api

    to_api = wa_to_api(to_phone)
    logger.info(
        "VOICE_GRAPH initiate_call phone_number_id=%s to=%s sdp_chars=%s",
        phone_number_id,
        to_api,
        len(sdp_offer or ""),
    )
    try:
        return await graph_post(
            f"{phone_number_id}/calls",
            access_token,
            {
                "messaging_product": "whatsapp",
                "to": to_api,
                "action": "connect",
                "session": {"sdp_type": "offer", "sdp": sdp_offer},
                "biz_opaque_callback_data": business_id,
            },
            business_id=business_id,
            channel="whatsapp",
        )
    except MessageFailedError as exc:
        graph_code = exc.extra.get("graph_code")
        if graph_code in BIC_ERRORS:
            raise VoiceCallNotAllowedError(BIC_ERRORS[graph_code]) from exc
        if graph_code == 138008:
            raise VoiceSdpError("Meta rejected the outbound SDP offer") from exc
        raise


def extract_call_id(data: dict[str, Any]) -> str:
    """Read the wacid from a `POST /calls` response."""
    calls = data.get("calls")
    if isinstance(calls, list) and calls and isinstance(calls[0], dict):
        first = calls[0]
        if first.get("id"):
            return str(first["id"])
    return str(data.get("call_id") or data.get("id") or "")


async def terminate_call(
    phone_number_id: str,
    call_id: str,
    access_token: str,
    *,
    business_id: str,
) -> dict[str, Any]:
    logger.info("VOICE_GRAPH terminate call_id=%s", call_id)
    return await graph_post(
        f"{phone_number_id}/calls",
        access_token,
        {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "terminate",
        },
        business_id=business_id,
        channel="whatsapp",
    )
