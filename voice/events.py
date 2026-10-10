"""Call lifecycle event names (no SDP / tokens / audio)."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class CallEventType(str, enum.Enum):
    CONNECT = "connect"
    CONNECTED = "connected"
    HANGUP = "hangup"
    TIMEOUT = "timeout"
    ERROR = "error"
    SDP_ACCEPTED = "sdp_accepted"
    BRIDGE_READY = "bridge_ready"
    CLEANUP = "cleanup"


CONNECT_NAMES = frozenset({"connect", "CONNECT"})
CONNECTED_NAMES = frozenset({"connected", "CONNECTED"})
HANGUP_NAMES = frozenset(
    {"hangup", "disconnected", "terminate", "timeout", "error", "HANGUP"}
)


@dataclass(frozen=True)
class CallEvent:
    event_type: CallEventType
    call_id: str
    business_id: str | None = None
    phone_number_id: str | None = None


def emit_call_event(
    event_type: CallEventType,
    *,
    call_id: str,
    business_id: str | None = None,
    phone_number_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = extra or {}
    logger.info(
        "VOICE_EVENT type=%s call_id=%s business=%s phone_number_id=%s %s",
        event_type.value,
        call_id,
        business_id or "-",
        phone_number_id or "-",
        " ".join(f"{k}={v}" for k, v in payload.items()),
    )
