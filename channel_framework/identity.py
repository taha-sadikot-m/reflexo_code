"""Identity normalization shared by non-Meta channel adapters."""

from __future__ import annotations

from identity.models import Channel

FRAMEWORK_CHANNELS = frozenset(
    {Channel.GMAIL, Channel.OUTLOOK, Channel.SLACK}
)


def normalize_channel_sender(channel: str, sender_id: str) -> str:
    parsed = Channel(channel)
    value = sender_id.strip()
    if parsed in {Channel.GMAIL, Channel.OUTLOOK}:
        return value.casefold()
    if parsed == Channel.SLACK:
        return value
    raise ValueError(f"{channel} is not a Channel Framework platform")
