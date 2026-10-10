"""Normalize Meta WhatsApp Calling webhook shapes."""

from __future__ import annotations

from typing import Any

from voice.events import CONNECT_NAMES, CONNECTED_NAMES, HANGUP_NAMES


def iter_call_records(value: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield per-call dicts from flat, nested or status-shaped `calls` values."""
    calls = value.get("calls")
    if isinstance(calls, list) and calls:
        records: list[dict[str, Any]] = []
        for item in calls:
            if isinstance(item, dict):
                merged = dict(item)
                if "metadata" not in merged and value.get("metadata"):
                    merged["metadata"] = value["metadata"]
                records.append(merged)
        return records
    statuses = value.get("statuses")
    if isinstance(statuses, list) and statuses:
        # Business-initiated RINGING / ACCEPTED / REJECTED updates.
        records = []
        for item in statuses:
            if not isinstance(item, dict) or item.get("type") != "call":
                continue
            records.append(
                {
                    "call_id": str(item.get("id") or ""),
                    "type": str(item.get("status") or ""),
                    "to": str(item.get("recipient_id") or ""),
                    "direction": "BUSINESS_INITIATED",
                    "timestamp": item.get("timestamp"),
                    "metadata": item.get("metadata") or value.get("metadata") or {},
                }
            )
        return records
    if value.get("call_id") or value.get("id") or value.get("session"):
        return [value]
    return []


def call_event_name(call: dict[str, Any]) -> str:
    raw = call.get("type") or call.get("event") or call.get("status") or ""
    return str(raw)


def call_id_of(call: dict[str, Any], fallback: Any = None) -> str:
    return str(call.get("call_id") or call.get("id") or fallback or "")


def sdp_offer_of(call: dict[str, Any]) -> str:
    session = call.get("session") or {}
    if isinstance(session, dict):
        return str(session.get("sdp") or "")
    return ""


def sdp_type_of(call: dict[str, Any]) -> str:
    session = call.get("session") or {}
    if isinstance(session, dict):
        return str(session.get("sdp_type") or "").lower()
    return ""


def call_direction_of(call: dict[str, Any]) -> str:
    return str(call.get("direction") or "").upper()


def is_business_initiated(call: dict[str, Any]) -> bool:
    """True when this record belongs to a call we placed, not one we received."""
    return (
        call_direction_of(call) == "BUSINESS_INITIATED"
        or sdp_type_of(call) == "answer"
    )


# Business-initiated call status values from the `statuses` array.
_STATUS_ALIASES = {
    "ringing": "ringing",
    "accepted": "accepted",
    "rejected": "hangup",
}


def classify_call_event(name: str) -> str:
    if name in CONNECT_NAMES:
        return "connect"
    if name in CONNECTED_NAMES:
        return "connected"
    alias = _STATUS_ALIASES.get(name.lower())
    if alias:
        return alias
    if name in HANGUP_NAMES or name.lower() in {
        "hangup",
        "disconnected",
        "terminate",
        "timeout",
        "error",
    }:
        lowered = name.lower()
        if lowered == "timeout":
            return "timeout"
        if lowered == "error":
            return "error"
        return "hangup"
    return name.lower() or "unknown"


def caller_name_of(value: dict[str, Any], call: dict[str, Any]) -> str:
    """WhatsApp profile name of the caller, from the sibling `contacts` array."""
    contacts = value.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        return ""
    caller = str(call.get("from") or "").lstrip("+")
    fallback = ""
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        profile = contact.get("profile")
        name = str((profile or {}).get("name") or "").strip()
        if not name:
            continue
        if caller and str(contact.get("wa_id") or "").lstrip("+") == caller:
            return name
        fallback = fallback or name
    return fallback


def phone_number_id_of(value: dict[str, Any], call: dict[str, Any]) -> str:
    meta = call.get("metadata") or value.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("phone_number_id"):
        return str(meta["phone_number_id"])
    return str(call.get("phone_number_id") or value.get("phone_number_id") or "")
