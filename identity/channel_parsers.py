"""Extract sender/receiving IDs from raw webhook payloads."""

from __future__ import annotations

import re
from typing import Any

from identity.models import Channel


def normalize_phone(phone: str) -> str:
    """Normalize phone to E.164-ish (+digits)."""
    cleaned = re.sub(r"[\s\-\(\)]+", "", phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def _extract_message_body(msg: dict) -> str:
    msg_type = msg.get("type", "text")
    if msg_type == "text":
        return (msg.get("text") or {}).get("body", "")
    if msg_type == "button":
        return (msg.get("button") or {}).get("text", "")
    if msg_type == "interactive":
        interactive = msg.get("interactive") or {}
        if "button_reply" in interactive:
            return interactive["button_reply"].get("title", "")
        if "list_reply" in interactive:
            return interactive["list_reply"].get("title", "")
    return f"[{msg_type}]"


def parse_whatsapp_webhook(payload: dict) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")

            contacts: dict[str, str | None] = {}
            for contact in value.get("contacts", []):
                wa_id = contact.get("wa_id", "")
                name = contact.get("profile", {}).get("name")
                contacts[wa_id] = name

            for msg in value.get("messages", []):
                sender_wa_id = msg.get("from", "")
                sender_phone = (
                    f"+{sender_wa_id}" if sender_wa_id and not sender_wa_id.startswith("+") else sender_wa_id
                )
                results.append(
                    {
                        "channel": Channel.WHATSAPP.value,
                        "sender_external_id": sender_phone,
                        "receiving_account_id": phone_number_id,
                        "sender_display_name": contacts.get(sender_wa_id),
                        "message_id": msg.get("id"),
                        "message_type": msg.get("type", "text"),
                        "message_body": _extract_message_body(msg),
                        "timestamp": msg.get("timestamp"),
                    }
                )
    return results


def parse_instagram_dm_webhook(payload: dict) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        ig_account_id = entry.get("id", "")
        for messaging in entry.get("messaging", []):
            sender_id = messaging.get("sender", {}).get("id", "")
            message = messaging.get("message", {})
            results.append(
                {
                    "channel": Channel.INSTAGRAM_DM.value,
                    "sender_external_id": sender_id,
                    "receiving_account_id": ig_account_id,
                    "sender_display_name": None,
                    "message_id": message.get("mid"),
                    "message_type": "text",
                    "message_body": message.get("text", ""),
                    "timestamp": messaging.get("timestamp"),
                }
            )
    return results


def parse_instagram_comment_webhook(payload: dict) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        ig_account_id = entry.get("id", "")
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value", {})
            from_user = value.get("from", {})
            results.append(
                {
                    "channel": Channel.INSTAGRAM_COMMENT.value,
                    "sender_external_id": from_user.get("id", ""),
                    "receiving_account_id": ig_account_id,
                    "sender_display_name": from_user.get("username"),
                    "message_id": value.get("id"),
                    "message_type": "comment",
                    "message_body": value.get("text", ""),
                    "media_id": value.get("media", {}).get("id"),
                }
            )
    return results


def parse_facebook_messenger_webhook(payload: dict) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        page_id = entry.get("id", "")
        for messaging in entry.get("messaging", []):
            sender_id = messaging.get("sender", {}).get("id", "")
            message = messaging.get("message", {})
            results.append(
                {
                    "channel": Channel.FACEBOOK_MESSENGER.value,
                    "sender_external_id": sender_id,
                    "receiving_account_id": page_id,
                    "sender_display_name": None,
                    "message_id": message.get("mid"),
                    "message_type": "text",
                    "message_body": message.get("text", ""),
                    "timestamp": messaging.get("timestamp"),
                }
            )
    return results


def parse_email_webhook(payload: dict) -> list[dict[str, Any]]:
    """Normalize a simple email ingress payload."""
    sender = (payload.get("from") or payload.get("sender") or "").lower().strip()
    receiving = (payload.get("to") or payload.get("recipient") or "").lower().strip()
    if not sender or not receiving:
        return []
    return [
        {
            "channel": Channel.EMAIL.value,
            "sender_external_id": sender,
            "receiving_account_id": receiving,
            "sender_display_name": payload.get("from_name"),
            "message_id": payload.get("message_id"),
            "message_type": "email",
            "message_body": payload.get("body") or payload.get("text") or "",
            "subject": payload.get("subject"),
        }
    ]


def parse_voice_call_webhook(payload: dict) -> list[dict[str, Any]]:
    """WhatsApp Calling-style payload: caller phone + phone_number_id."""
    caller = payload.get("from") or payload.get("caller") or ""
    phone_number_id = payload.get("phone_number_id") or payload.get("receiving_account_id") or ""
    if not caller or not phone_number_id:
        return []
    sender = normalize_phone(caller)
    return [
        {
            "channel": Channel.VOICE.value,
            "sender_external_id": sender,
            "receiving_account_id": phone_number_id,
            "sender_display_name": payload.get("caller_name"),
            "message_id": payload.get("call_id"),
            "message_type": "voice_call",
            "message_body": "",
        }
    ]
