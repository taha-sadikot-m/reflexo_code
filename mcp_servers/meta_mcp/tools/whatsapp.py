"""WhatsApp messaging tools."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.tools.helpers import business_exists, validate_e164, wa_to_api
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_get, graph_post
from mcp_servers.meta_mcp.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)


async def _send_advanced_payload(
    business_id: str,
    to: str,
    payload: dict[str, Any],
    *,
    rate_key: str,
    result_type: str,
    credentials: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        to_e164 = validate_e164(to)
        await check_rate_limit(business_id, rate_key)
        creds = credentials or await ensure_valid_token(business_id, "whatsapp")
        body = {
            "messaging_product": "whatsapp",
            "to": wa_to_api(to_e164),
            **payload,
        }
        data = await graph_post(
            f"{creds['phone_number_id']}/messages",
            creds["access_token"],
            body,
            business_id=business_id,
            channel="whatsapp",
        )
        return {
            "message_id": (data.get("messages") or [{}])[0].get("id"),
            "status": "sent",
            "channel": "whatsapp",
            "type": result_type,
        }
    except MetaMCPError as exc:
        return exc.to_dict()
    except Exception as exc:
        logger.exception("advanced WhatsApp send failed type=%s", result_type)
        return tool_error("MESSAGE_FAILED", str(exc))


async def _discover_catalog_id(
    business_id: str,
    credentials: dict[str, Any],
) -> str:
    phone = await graph_get(
        str(credentials["phone_number_id"]),
        credentials["access_token"],
        {"fields": "whatsapp_business_account"},
        business_id=business_id,
        channel="whatsapp",
    )
    waba = phone.get("whatsapp_business_account") or {}
    waba_id = str(waba.get("id") if isinstance(waba, dict) else waba)
    if not waba_id:
        return ""
    catalogs = await graph_get(
        f"{waba_id}/product_catalogs",
        credentials["access_token"],
        {"fields": "id", "limit": 1},
        business_id=business_id,
        channel="whatsapp",
    )
    rows = catalogs.get("data") or []
    return str(rows[0].get("id") or "") if rows else ""


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_message(
        business_id: str,
        to: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Send a text message via WhatsApp Business Cloud API (24h window).
        For business-initiated messages outside the window, use send_whatsapp_template.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not text or not text.strip():
            return tool_error("INVALID_INPUT", "text is empty")
        if len(text) > 4096:
            return tool_error("INVALID_INPUT", "text exceeds 4096 characters")

        try:
            to_e164 = validate_e164(to)
            await check_rate_limit(business_id, "whatsapp_send")
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]

            body: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": wa_to_api(to_e164),
                "type": "text",
                "text": {"body": text},
            }
            if reply_to_message_id:
                body["context"] = {"message_id": reply_to_message_id}

            data = await graph_post(
                f"{phone_number_id}/messages",
                creds["access_token"],
                body,
                business_id=business_id,
                channel="whatsapp",
            )
            message_id = (data.get("messages") or [{}])[0].get("id")
            return {
                "message_id": message_id,
                "status": "sent",
                "channel": "whatsapp",
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_whatsapp_message failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_template(
        business_id: str,
        to: str,
        template_name: str,
        template_params: list[str] | None = None,
        language_code: str = "en",
    ) -> dict[str, Any]:
        """Send a pre-approved WhatsApp template (required outside the 24h window)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not template_name:
            return tool_error("INVALID_INPUT", "template_name is required")

        try:
            to_e164 = validate_e164(to)
            await check_rate_limit(business_id, "whatsapp_template")
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
                        "parameters": [
                            {"type": "text", "text": p} for p in template_params
                        ],
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
                business_id=business_id,
                channel="whatsapp",
            )
            message_id = (data.get("messages") or [{}])[0].get("id")
            return {
                "message_id": message_id,
                "status": "sent",
                "channel": "whatsapp",
                "template": template_name,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_whatsapp_template failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_media(
        business_id: str,
        to: str,
        media_type: str,
        media_url: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Send image/document/video/audio/sticker via WhatsApp (public HTTPS URL)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        allowed = {"image", "document", "video", "audio", "sticker"}
        if media_type not in allowed:
            return tool_error(
                "INVALID_INPUT",
                f"media_type must be one of {sorted(allowed)}",
            )
        if not media_url or not media_url.startswith("https://"):
            return tool_error("MEDIA_UPLOAD_FAILED", "media_url must be a public HTTPS URL")

        try:
            to_e164 = validate_e164(to)
            await check_rate_limit(business_id, "whatsapp_send")
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]

            media_obj: dict[str, Any] = {"link": media_url}
            if caption and media_type in ("image", "video", "document"):
                media_obj["caption"] = caption[:1024]
            if filename and media_type == "document":
                media_obj["filename"] = filename

            body = {
                "messaging_product": "whatsapp",
                "to": wa_to_api(to_e164),
                "type": media_type,
                media_type: media_obj,
            }
            data = await graph_post(
                f"{phone_number_id}/messages",
                creds["access_token"],
                body,
                business_id=business_id,
                channel="whatsapp",
            )
            message_id = (data.get("messages") or [{}])[0].get("id")
            return {
                "message_id": message_id,
                "status": "sent",
                "channel": "whatsapp",
                "media_type": media_type,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_whatsapp_media failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_interactive(
        business_id: str,
        to: str,
        interactive_type: str,
        body: str,
        buttons_or_sections: list[dict],
        header: str | None = None,
        footer: str | None = None,
    ) -> dict[str, Any]:
        """Send interactive button or list message via WhatsApp."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if interactive_type not in ("button", "list"):
            return tool_error("INVALID_INPUT", "interactive_type must be 'button' or 'list'")
        if not body:
            return tool_error("INVALID_INPUT", "body is required")

        try:
            to_e164 = validate_e164(to)
            await check_rate_limit(business_id, "whatsapp_send")
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]

            interactive: dict[str, Any] = {
                "type": interactive_type,
                "body": {"text": body},
            }
            if footer:
                interactive["footer"] = {"text": footer[:60]}

            if interactive_type == "button":
                if len(buttons_or_sections) > 3:
                    return tool_error("INVALID_INPUT", "Max 3 buttons allowed")
                interactive["action"] = {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": str(b.get("id", ""))[:256],
                                "title": str(b.get("title", ""))[:20],
                            },
                        }
                        for b in buttons_or_sections
                    ]
                }
            else:
                if header:
                    interactive["header"] = {"type": "text", "text": header[:60]}
                total_rows = sum(len(s.get("rows") or []) for s in buttons_or_sections)
                if total_rows > 10:
                    return tool_error("INVALID_INPUT", "Max 10 list rows total")
                interactive["action"] = {
                    "button": "View",
                    "sections": buttons_or_sections,
                }

            payload = {
                "messaging_product": "whatsapp",
                "to": wa_to_api(to_e164),
                "type": "interactive",
                "interactive": interactive,
            }
            data = await graph_post(
                f"{phone_number_id}/messages",
                creds["access_token"],
                payload,
                business_id=business_id,
                channel="whatsapp",
            )
            message_id = (data.get("messages") or [{}])[0].get("id")
            return {
                "message_id": message_id,
                "status": "sent",
                "channel": "whatsapp",
                "interactive_type": interactive_type,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_whatsapp_interactive failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_location(
        business_id: str,
        to: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Send a WhatsApp map pin within the customer-service window."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return tool_error("INVALID_INPUT", "Invalid latitude or longitude")
        location: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            location["name"] = name
        if address:
            location["address"] = address
        return await _send_advanced_payload(
            business_id,
            to,
            {"type": "location", "location": location},
            rate_key="whatsapp_location",
            result_type="location",
        )

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_flow(
        business_id: str,
        to: str,
        flow_id: str,
        flow_cta: str,
        body: str,
        flow_action: str = "navigate",
        flow_action_payload: dict | None = None,
        header: str | None = None,
        footer: str | None = None,
    ) -> dict[str, Any]:
        """Send a native WhatsApp Flow form."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not flow_id or not flow_cta or not body:
            return tool_error("INVALID_INPUT", "flow_id, flow_cta and body are required")
        if len(flow_cta) > 20:
            return tool_error("INVALID_INPUT", "flow_cta must be at most 20 characters")
        if flow_action not in {"navigate", "data_exchange"}:
            return tool_error("INVALID_INPUT", "flow_action must be navigate or data_exchange")
        if flow_action == "navigate" and not (
            flow_action_payload
            and str(flow_action_payload.get("screen") or "").strip()
        ):
            return tool_error(
                "INVALID_INPUT",
                "navigate flow_action requires flow_action_payload.screen",
            )
        parameters: dict[str, Any] = {
            "flow_message_version": "3",
            "flow_id": flow_id,
            "flow_cta": flow_cta,
            "flow_action": flow_action,
        }
        if flow_action_payload:
            parameters["flow_action_payload"] = flow_action_payload
        interactive: dict[str, Any] = {
            "type": "flow",
            "body": {"text": body},
            "action": {
                "name": "flow",
                "parameters": parameters,
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        return await _send_advanced_payload(
            business_id,
            to,
            {"type": "interactive", "interactive": interactive},
            rate_key="whatsapp_flow",
            result_type="flow",
        )

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_catalog(
        business_id: str,
        to: str,
        body: str,
        product_retailer_ids: list[str] | None = None,
        header: str | None = None,
        footer: str | None = None,
    ) -> dict[str, Any]:
        """Send the full catalog or a list of up to 30 products."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not body:
            return tool_error("INVALID_INPUT", "body is required")
        products = product_retailer_ids or []
        if len(products) > 30:
            return tool_error("INVALID_INPUT", "Max 30 products allowed")
        if products and not header:
            return tool_error(
                "INVALID_INPUT",
                "header is required for a multi-product message",
            )
        try:
            creds = await ensure_valid_token(business_id, "whatsapp")
            catalog_id = str(creds.get("catalog_id") or "")
            if products and not catalog_id:
                catalog_id = await _discover_catalog_id(business_id, creds)
            if products and not catalog_id:
                return tool_error(
                    "CATALOG_NOT_CONFIGURED",
                    "Business has no WhatsApp Commerce catalog",
                )
        except MetaMCPError as exc:
            return exc.to_dict()
        interactive: dict[str, Any] = {
            "type": "product_list" if products else "catalog_message",
            "body": {"text": body},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        if products:
            interactive["action"] = {
                "catalog_id": catalog_id,
                "sections": [
                    {
                        "title": header or "Products",
                        "product_items": [
                            {"product_retailer_id": product_id}
                            for product_id in products
                        ],
                    }
                ],
            }
        else:
            interactive["action"] = {
                "name": "catalog_message",
                "parameters": {},
            }
        return await _send_advanced_payload(
            business_id,
            to,
            {"type": "interactive", "interactive": interactive},
            rate_key="whatsapp_catalog",
            result_type=interactive["type"],
            credentials=creds,
        )

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_whatsapp_carousel(
        business_id: str,
        to: str,
        template_name: str,
        body: str,
        cards: list[dict],
        language_code: str = "en",
    ) -> dict[str, Any]:
        """Send a pre-approved 2–10 card WhatsApp carousel template."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not template_name or not body or not 2 <= len(cards) <= 10:
            return tool_error(
                "INVALID_INPUT",
                "template_name, body and 2-10 cards are required",
            )
        action_cards = []
        for index, card in enumerate(cards):
            components: list[dict[str, Any]] = []
            image_url = str(card.get("header_image_url") or "")
            body_text = str(card.get("body_text") or "").strip()
            buttons = list(card.get("buttons") or [])
            if not image_url.startswith("https://"):
                return tool_error(
                    "INVALID_INPUT",
                    "Every carousel card requires an HTTPS header_image_url",
                )
            if not body_text:
                return tool_error(
                    "INVALID_INPUT",
                    "Every carousel card requires body_text",
                )
            if len(buttons) > 2:
                return tool_error(
                    "INVALID_INPUT",
                    "Carousel cards support at most 2 buttons",
                )
            components.append(
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"link": image_url}}
                    ],
                }
            )
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": body_text}
                    ],
                }
            )
            for button_index, button in enumerate(buttons):
                button_type = str(button.get("type") or "quick_reply")
                parameter = (
                    {
                        "type": "payload",
                        "payload": str(button.get("payload") or button.get("text") or ""),
                    }
                    if button_type == "quick_reply"
                    else {"type": "text", "text": str(button.get("url") or "")}
                )
                components.append(
                    {
                        "type": "button",
                        "sub_type": button_type,
                        "index": str(button_index),
                        "parameters": [parameter],
                    }
                )
            action_cards.append(
                {"card_index": index, "components": components}
            )
        result = await _send_advanced_payload(
            business_id,
            to,
            {
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code or "en"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": body}],
                        },
                        {"type": "carousel", "cards": action_cards},
                    ],
                },
            },
            rate_key="whatsapp_carousel",
            result_type="carousel",
        )
        result["template_type"] = "carousel"
        return result
