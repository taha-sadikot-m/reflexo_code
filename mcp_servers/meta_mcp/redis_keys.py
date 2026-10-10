"""Meta MCP Redis key builders."""

from __future__ import annotations


def meta_creds_key(business_id: str, channel: str) -> str:
    """meta_creds:{business_id}:{channel} — TTL 300s"""
    return f"meta_creds:{business_id}:{channel}"


def meta_rate_key(business_id: str, endpoint: str, window_key: int) -> str:
    """meta_rate:{business_id}:{endpoint}:{window_key}"""
    return f"meta_rate:{business_id}:{endpoint}:{window_key}"


def wa_profile_key(phone_number: str) -> str:
    """wa_profile:{phone_number} — TTL 3600s"""
    return f"wa_profile:{phone_number}"


def ig_post_key(post_id: str) -> str:
    """ig_post:{post_id} — TTL 600s"""
    return f"ig_post:{post_id}"


def fb_post_key(post_id: str) -> str:
    """fb_post:{post_id} — TTL 600s"""
    return f"fb_post:{post_id}"


def incoming_channel(business_id: str) -> str:
    return f"incoming:{business_id}"


def status_channel(business_id: str) -> str:
    return f"status:{business_id}"


def voice_channel(business_id: str) -> str:
    return f"voice:{business_id}"
