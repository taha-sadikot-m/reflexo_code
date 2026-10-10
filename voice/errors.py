"""Voice pipeline errors — never crash Meta MCP on a failed call."""

from __future__ import annotations


class VoiceError(Exception):
    """Non-fatal voice pipeline failure."""


class VoiceNotConfiguredError(VoiceError):
    """LiveKit or Deepgram credentials are missing."""


class VoiceCapacityError(VoiceError):
    """Concurrent call cap reached."""


class VoiceSdpError(VoiceError):
    """SDP answer could not be created."""


class VoiceCallNotAllowedError(VoiceError):
    """Meta refused to place the outbound call (permission, config, limits)."""
