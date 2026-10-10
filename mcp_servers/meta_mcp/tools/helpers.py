"""Shared helpers for Meta MCP tools."""

from __future__ import annotations

import re
from typing import Any

from shared.db import service_connection

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


async def business_exists(business_id: str) -> bool:
    async with service_connection() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM businesses WHERE id = $1::uuid AND is_active = true",
            business_id,
        )
    return row is not None


def normalize_e164(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]+", "", phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def validate_e164(phone: str) -> str:
    normalized = normalize_e164(phone)
    if not E164_RE.match(normalized):
        from mcp_servers.meta_mcp.utils.errors import InvalidRecipientError

        raise InvalidRecipientError(phone, "Must match E.164 (+919876543210)")
    return normalized


def wa_to_api(phone_e164: str) -> str:
    """Strip leading + for WhatsApp Cloud API."""
    return phone_e164.lstrip("+")


def catch_tool_errors(fn_result: Any) -> Any:
    """Pass-through; tools use try/except with MetaMCPError.to_dict()."""
    return fn_result
