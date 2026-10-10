"""Outbound sender registry."""

from channel_framework.senders.email import send_email
from channel_framework.senders.slack import send_slack_message

__all__ = ["send_email", "send_slack_message"]
