"""Channel adapter registry."""

from __future__ import annotations

from channel_framework.adapters.base import ChannelAdapter

_ADAPTERS: dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    platform = adapter.platform.strip().lower()
    if not platform:
        raise ValueError("adapter.platform is required")
    _ADAPTERS[platform] = adapter


def get_adapter(platform: str) -> ChannelAdapter:
    normalized = platform.strip().lower()
    if normalized not in _ADAPTERS:
        raise KeyError(f"No adapter registered for platform: {platform}")
    return _ADAPTERS[normalized]


def registered_platforms() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


from channel_framework.adapters.gmail import GmailAdapter
from channel_framework.adapters.outlook import OutlookAdapter
from channel_framework.adapters.slack import SlackAdapter

for _adapter in (GmailAdapter(), OutlookAdapter(), SlackAdapter()):
    register_adapter(_adapter)
