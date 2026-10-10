"""Provider-neutral channel connection and messaging framework."""

from channel_framework.adapters.base import (
    ChannelAdapter,
    NormalizedMessage,
    WebhookSubscription,
)

__all__ = ["ChannelAdapter", "NormalizedMessage", "WebhookSubscription"]
