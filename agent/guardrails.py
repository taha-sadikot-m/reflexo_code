"""Input/output rails: prompt injection, owner PII, catalog price check.

Colang sources live in guardrails/. NeMo is optional; these Python actions are
the Stage 2 source of truth so CI works without nemoguardrails (e.g. 3.14).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

INJECTION_BLOCK_REPLY = "I can only help with questions about this business."
PRICE_FALLBACK_REPLY = "Let me check the exact pricing for you."
REDACTED = "[redacted]"

_PRICED_ENTITY_TYPES = ("product", "service", "offering")

# Phrase / pattern heuristics (case-insensitive). Avoid short tokens like "dan"
# except as a word so normal questions are not blocked (R2.4).
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|your|all)\s+instructions", re.I),
    re.compile(r"ignore\s+your\s+instructions", re.I),
    re.compile(r"disregard\s+(the\s+)?(system\s+)?(prompt|instructions)", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"\bdan\b", re.I),
    re.compile(r"(you\s+are\s+now\s+in\s+)?debug\s+mode", re.I),
    re.compile(r"(reveal|dump|show|print)\s+(the\s+)?(system\s+)?prompt", re.I),
    re.compile(r"you\s+are\s+now\s+unrestricted", re.I),
    re.compile(r"forget\s+(your|all|previous)\s+instructions", re.I),
    re.compile(r"override\s+(your|the)\s+(instructions|prompt|system)", re.I),
    re.compile(r"developer\s+mode", re.I),
]

_PRICE_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)"
    r"|([\d,]+(?:\.\d{1,2})?)\s*(?:₹|rs\.?|inr)",
    re.I,
)


def check_injection(user_message: str) -> bool:
    """Colang action: true if the user text looks like prompt injection."""
    text = (user_message or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _INJECTION_PATTERNS)


def check_input_rails(
    message_text: str,
    session_type: str,
) -> tuple[bool, str | None, list[str]]:
    """Customer (and social-as-customer) input rail. Owner/onboarding skip.

    Returns (blocked, canned_reply_or_none, trigger_names).
    """
    if session_type in ("owner", "onboarding"):
        return False, None, []
    if not check_injection(message_text):
        return False, None, []
    return True, INJECTION_BLOCK_REPLY, ["prompt_injection"]


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_chunk_matches(chunk: str, owner_digits: str) -> bool:
    d = _digits(chunk)
    if not d or not owner_digits:
        return False
    if d == owner_digits:
        return True
    if len(owner_digits) >= 10 and len(d) >= 10 and d[-10:] == owner_digits[-10:]:
        return True
    return False


def redact_owner_pii(response: str, business_config: dict[str, Any] | None) -> tuple[str, bool]:
    """Replace owner_phone / owner_email. Does not strip public contact.email."""
    cfg = business_config or {}
    out = response or ""
    changed = False
    owner_email = str(cfg.get("owner_email") or "").strip()
    if owner_email and owner_email.lower() in out.lower():
        out = re.sub(re.escape(owner_email), REDACTED, out, flags=re.IGNORECASE)
        changed = True

    owner_phone = str(cfg.get("owner_phone") or "").strip()
    od = _digits(owner_phone)
    if od and len(od) >= 8:

        def _repl(match: re.Match[str]) -> str:
            nonlocal changed
            chunk = match.group(0)
            if _phone_chunk_matches(chunk, od):
                changed = True
                return REDACTED
            return chunk

        out = re.sub(r"\+?[\d][\d\s\-\(\)]{6,}\d", _repl, out)
        if owner_phone and owner_phone in out:
            out = out.replace(owner_phone, REDACTED)
            changed = True
    return out, changed


def check_pii_in_response(
    response: str,
    session_type: str,
    business_config: dict[str, Any] | None = None,
) -> bool:
    """Colang action: whether customer output contains owner PII."""
    if session_type in ("owner", "onboarding"):
        return False
    _, has = redact_owner_pii(response, business_config)
    return has


def extract_prices(response: str) -> list[float]:
    """Colang action: currency amounts in the bot reply."""
    amounts: list[float] = []
    for match in _PRICE_RE.finditer(response or ""):
        raw = match.group(1) or match.group(2) or ""
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return amounts


def _parse_price_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[\d,]+(?:\.\d{1,2})?", value.replace("₹", ""))
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("price", "amount", "cost", "mrp", "rate"):
            parsed = _parse_price_value(value.get(key))
            if parsed is not None:
                return parsed
    return None


def catalog_price_for_entity(entity: dict[str, Any]) -> float | None:
    data = entity.get("data")
    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            data = None
    price = _parse_price_value(data)
    if price is not None:
        return price
    return _parse_price_value(entity.get("price"))


def _format_inr(amount: float) -> str:
    if amount == int(amount):
        return f"₹{int(amount)}"
    return f"₹{amount:.2f}"


def _replace_amount_in_text(text: str, old: float, new: float) -> str:
    """Replace a currency mention of ``old`` with formatted ``new``."""

    def _repl(match: re.Match[str]) -> str:
        raw = match.group(1) or match.group(2) or ""
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            return match.group(0)
        if abs(val - old) < 0.009:
            return _format_inr(new)
        return match.group(0)

    return _PRICE_RE.sub(_repl, text)


async def _load_catalog_entities(business_id: str) -> list[dict[str, Any]]:
    """Load product/service/offering rows via Core MCP. Empty on failure."""
    from agent.tools import call_mcp_tool

    entities: list[dict[str, Any]] = []
    for entity_type in _PRICED_ENTITY_TYPES:
        try:
            result = await call_mcp_tool(
                "core",
                "list_entities",
                {"business_id": business_id, "entity_type": entity_type, "limit": 100},
            )
        except Exception as exc:
            logger.debug("list_entities %s failed: %s", entity_type, exc)
            continue
        if not isinstance(result, dict) or result.get("error"):
            continue
        rows = result.get("entities") or result.get("results") or []
        if isinstance(rows, list):
            entities.extend(r for r in rows if isinstance(r, dict))
    return entities


def apply_price_rail(
    response_text: str,
    catalog: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """If a named catalog item's price disagrees, correct or fall back."""
    amounts = extract_prices(response_text)
    if not amounts:
        return response_text, []
    priced: list[tuple[str, float]] = []
    for ent in catalog or []:
        name = str(ent.get("name") or "").strip()
        if len(name) < 3:
            continue
        price = catalog_price_for_entity(ent)
        if price is None:
            continue
        priced.append((name, price))
    if not priced:
        return response_text, []

    lower = response_text.lower()
    mismatches: list[tuple[str, float, float]] = []
    seen_names: set[str] = set()
    for name, catalog_price in sorted(priced, key=lambda x: len(x[0]), reverse=True):
        key = name.lower()
        if key in seen_names:
            continue
        if key not in lower:
            continue
        seen_names.add(key)
        # Pick a stated amount that is not the catalog price
        disagreed = [a for a in amounts if abs(a - catalog_price) > 0.009]
        if not disagreed:
            continue
        mismatches.append((name, disagreed[0], catalog_price))

    if not mismatches:
        return response_text, []
    if len(mismatches) == 1:
        _name, stated, catalog_price = mismatches[0]
        corrected = _replace_amount_in_text(response_text, stated, catalog_price)
        if corrected != response_text:
            return corrected, ["price_mismatch"]
    return PRICE_FALLBACK_REPLY, ["price_mismatch"]


async def verify_prices_against_catalog(response: str, business_id: str) -> bool:
    """Colang action: True when prices match catalog or cannot be checked."""
    catalog = await _load_catalog_entities(business_id)
    _text, triggers = apply_price_rail(response, catalog)
    return "price_mismatch" not in triggers


async def check_output_rails(
    response_text: str,
    business_id: str,
    session_type: str,
    business_config: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """PII (customer only) then price check (customer only). Owner/onboarding skip both."""
    text = response_text or ""
    triggers: list[str] = []
    if session_type in ("owner", "onboarding"):
        return text, triggers

    cleaned, had_pii = redact_owner_pii(text, business_config)
    if had_pii:
        text = cleaned
        triggers.append("pii_redacted")

    try:
        catalog = await _load_catalog_entities(business_id)
    except Exception as exc:
        logger.debug("price catalog load failed: %s", exc)
        catalog = []
    text, price_triggers = apply_price_rail(text, catalog)
    triggers.extend(price_triggers)
    return text, triggers
