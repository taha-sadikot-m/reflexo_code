"""Standard Meta MCP tool error formatting."""

from __future__ import annotations

from typing import Any


ERROR_GUIDANCE: dict[str, str] = {
    "BUSINESS_NOT_FOUND": (
        "The business_id may be incorrect. Check the session context for the correct ID."
    ),
    "CHANNEL_NOT_CONNECTED": (
        "The business owner has not connected this channel. Inform the customer this "
        "channel is unavailable, or escalate to the owner to set it up."
    ),
    "INVALID_RECIPIENT": (
        "Check the recipient format. For WhatsApp, use E.164 (+919876543210). "
        "For Instagram, use the IG-scoped user ID from the webhook. If the 24-hour "
        "window expired, use send_whatsapp_template instead of send_whatsapp_message."
    ),
    "MESSAGE_FAILED": (
        "Check the error details. Common causes: 24h window expired (use template), "
        "recipient blocked the business, invalid media URL. Log the failure and inform "
        "the customer there was a delivery issue."
    ),
    "RATE_LIMITED": (
        "Wait retry_after_seconds before retrying. Do not inform the customer about "
        "rate limiting. Queue the message and retry automatically."
    ),
    "TOKEN_EXPIRED": (
        "The business's Meta connection needs to be re-authorized. Escalate to the "
        "business owner with instructions to reconnect their account. Do not retry."
    ),
    "MEDIA_UPLOAD_FAILED": (
        "Check the media URL is publicly accessible HTTPS, correct MIME type, and "
        "within size limits (image 5MB, document 100MB, video 16MB, audio 16MB)."
    ),
    "ENTRY_NOT_FOUND": "The ID was not found for this business.",
    "INVALID_INPUT": "Check the tool arguments and try again.",
}


def tool_error(
    code: str,
    message: str,
    agent_guidance: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "agent_guidance": agent_guidance or ERROR_GUIDANCE.get(code, "Fix the input and retry."),
    }
    err.update(extra)
    return {"error": err}


class MetaMCPError(Exception):
    """Base error for Meta MCP operations (raised internally, converted to tool_error)."""

    def __init__(self, code: str, message: str, agent_guidance: str | None = None, **extra: Any):
        self.code = code
        self.message = message
        self.agent_guidance = agent_guidance or ERROR_GUIDANCE.get(code, "Fix the input and retry.")
        self.extra = extra
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return tool_error(self.code, self.message, self.agent_guidance, **self.extra)


class ChannelNotConnectedError(MetaMCPError):
    def __init__(self, business_id: str, channel: str):
        super().__init__(
            "CHANNEL_NOT_CONNECTED",
            f"Business {business_id} has no active {channel} channel",
        )


class TokenExpiredError(MetaMCPError):
    def __init__(self, business_id: str, channel: str):
        super().__init__(
            "TOKEN_EXPIRED",
            f"Access token expired for business {business_id} on {channel}",
        )


class MetaAuthError(MetaMCPError):
    """Graph rejected the access token (code 190/102/463/467 or HTTP 401).

    Raised from the HTTP layer, which has no business/channel context. Callers
    that do know the channel convert this into cache invalidation + reconnect.
    """

    def __init__(self, details: str):
        super().__init__("TOKEN_EXPIRED", f"Meta rejected the access token: {details}")


class RateLimitedError(MetaMCPError):
    def __init__(
        self,
        endpoint: str,
        limit: int,
        window_seconds: int,
        retry_after_seconds: int,
    ):
        super().__init__(
            "RATE_LIMITED",
            f"Rate limit exceeded for {endpoint}: {limit} calls per {window_seconds}s",
            retry_after_seconds=retry_after_seconds,
        )
        self.retry_after_seconds = retry_after_seconds


class MessageFailedError(MetaMCPError):
    def __init__(
        self,
        details: str,
        *,
        graph_code: int | None = None,
        http_status: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(
            "MESSAGE_FAILED",
            f"Message delivery failed: {details}",
            graph_code=graph_code,
            http_status=http_status,
            retryable=retryable,
        )


class MediaUploadFailedError(MetaMCPError):
    def __init__(self, reason: str):
        super().__init__("MEDIA_UPLOAD_FAILED", f"Media upload failed: {reason}")


class InvalidRecipientError(MetaMCPError):
    def __init__(self, recipient: str, reason: str = ""):
        msg = f"Invalid recipient: {recipient}"
        if reason:
            msg = f"{msg}. {reason}"
        super().__init__("INVALID_RECIPIENT", msg)
