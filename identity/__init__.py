"""
REFLEXO Identity Resolution Service.

Usage:
    from identity import resolve_identity, IdentityResult

    result = await resolve_identity(
        channel="whatsapp",
        sender_external_id="+919876543210",
        receiving_account_id="107456789012345",
        sender_display_name="Priya",
    )
"""

from identity.bot_detection import (
    BotDetector,
    increment_conversation_depth,
    register_managed_number,
    sync_managed_numbers,
)
from identity.customer_merge import auto_merge_on_email_match, merge_customers
from identity.models import (
    Channel,
    IdentityErrorCode,
    IdentityResolutionError,
    IdentityResult,
)
from identity.owner_link import link_owner_channel_identity
from identity.resolver import resolve_identity

__all__ = [
    "resolve_identity",
    "link_owner_channel_identity",
    "IdentityResult",
    "IdentityResolutionError",
    "IdentityErrorCode",
    "Channel",
    "merge_customers",
    "auto_merge_on_email_match",
    "BotDetector",
    "sync_managed_numbers",
    "register_managed_number",
    "increment_conversation_depth",
]
