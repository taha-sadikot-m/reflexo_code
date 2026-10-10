"""Identity Resolution data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


@dataclass(frozen=True, slots=True)
class IdentityResult:
    """Result of resolving a message sender's identity."""

    business_id: str
    customer_id: Optional[str]
    is_owner: bool
    is_new_customer: bool
    customer_name: Optional[str]
    is_bot_suspected: bool
    bot_detection_layers: list[str] = field(default_factory=list)
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class IdentityErrorCode(str, Enum):
    UNKNOWN_CHANNEL_ACCOUNT = "UNKNOWN_CHANNEL_ACCOUNT"
    IDENTITY_RESOLUTION_FAILED = "IDENTITY_RESOLUTION_FAILED"
    CUSTOMER_CREATION_FAILED = "CUSTOMER_CREATION_FAILED"
    INVALID_CHANNEL = "INVALID_CHANNEL"
    MISSING_SENDER_ID = "MISSING_SENDER_ID"


class IdentityResolutionError(Exception):
    """Raised when identity resolution fails."""

    def __init__(self, code: IdentityErrorCode, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"{code.value}: {message}")


class Channel(str, Enum):
    WHATSAPP = "whatsapp"
    INSTAGRAM_DM = "instagram_dm"
    INSTAGRAM_COMMENT = "instagram_comment"
    FACEBOOK_MESSENGER = "facebook_messenger"
    FACEBOOK_COMMENT = "facebook_comment"
    EMAIL = "email"
    VOICE = "voice"
    SMS = "sms"
    WEB_WIDGET = "web_widget"
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    SLACK = "slack"
