"""Per-call voice session metadata (stored in LiveKit room metadata)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class UserData:
    business_id: str
    call_id: str
    phone_number_id: str
    caller_phone: str
    is_owner: bool = False
    customer_id: str | None = None
    customer_name: str = ""
    business_name: str = ""
    persona_name: str = "the assistant"
    greeting_message: str = ""
    conversation_id: str = ""
    language_preference: str = "en"
    is_outbound: bool = False
    outbound_reason: str = ""
    outbound_customer_name: str = ""
    utterance_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> UserData:
        raw = dict(data or {})
        return cls(
            business_id=str(raw.get("business_id") or ""),
            call_id=str(raw.get("call_id") or ""),
            phone_number_id=str(raw.get("phone_number_id") or ""),
            caller_phone=str(raw.get("caller_phone") or ""),
            is_owner=bool(raw.get("is_owner", False)),
            customer_id=raw.get("customer_id") or None,
            customer_name=str(raw.get("customer_name") or ""),
            business_name=str(raw.get("business_name") or ""),
            persona_name=str(raw.get("persona_name") or "the assistant"),
            greeting_message=str(raw.get("greeting_message") or ""),
            conversation_id=str(raw.get("conversation_id") or ""),
            language_preference=str(raw.get("language_preference") or "en"),
            is_outbound=bool(raw.get("is_outbound", False)),
            outbound_reason=str(raw.get("outbound_reason") or ""),
            outbound_customer_name=str(raw.get("outbound_customer_name") or ""),
            utterance_count=int(raw.get("utterance_count") or 0),
            extra=dict(raw.get("extra") or {}),
        )
