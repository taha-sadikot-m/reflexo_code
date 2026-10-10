"""Redis key builders for hot-path caches (see DATABASE_SCHEMA.md Redis Schema)."""

from __future__ import annotations


def identity_key(channel: str, external_id: str, business_id: str | None = None) -> str:
    """identity:{channel}:{external_id}[:{business_id}] — TTL 3600s"""
    if business_id:
        return f"identity:{channel}:{external_id}:{business_id}"
    return f"identity:{channel}:{external_id}"


def channel_account_key(channel: str, receiving_account_id: str) -> str:
    """channel_account:{channel}:{receiving_account_id} — TTL 3600s"""
    return f"channel_account:{channel}:{receiving_account_id}"


def owner_check_key(business_id: str, channel: str, sender_external_id: str) -> str:
    """owner_check:{business_id}:{channel}:{sender_external_id} — TTL 3600s / 1800s"""
    return f"owner_check:{business_id}:{channel}:{sender_external_id}"


def business_config_key(business_id: str) -> str:
    """business:{business_id}:config — TTL 300s"""
    return f"business:{business_id}:config"


def dynamic_data_key(business_id: str, key: str) -> str:
    """dynamic:{business_id}:{key} — TTL set by business"""
    return f"dynamic:{business_id}:{key}"


def conv_velocity_key(business_id: str, sender_id: str) -> str:
    """conv_velocity:{business_id}:{sender_id} — sorted set, TTL 300s"""
    return f"conv_velocity:{business_id}:{sender_id}"


def conv_depth_key(business_id: str, sender_id: str, date: str) -> str:
    """conv_depth:{business_id}:{sender_id}:{date} — counter, TTL 86400s"""
    return f"conv_depth:{business_id}:{sender_id}:{date}"


MANAGED_NUMBERS_KEY = "reflexo:managed_numbers"

# Durable inbound transport. Pub/Sub remains as an observability signal, while
# the worker consumes this stream so restarts do not lose Meta webhooks.
INBOUND_EVENTS_STREAM = "reflexo:inbound_events"
INBOUND_EVENTS_GROUP = "reflexo:agent_workers"


def token_budget_key(business_id: str, day: str) -> str:
    """tokens:{business_id}:{YYYY-MM-DD} — daily LLM token counter, TTL 2 days."""
    return f"tokens:{business_id}:{day}"


def semantic_cache_key(business_id: str) -> str:
    """sem_cache:{business_id} — hash of query embeddings + responses."""
    return f"sem_cache:{business_id}"


def usage_key(business_id: str, billing_cycle_start: str) -> str:
    """usage:{business_id}:{billing_cycle_start} — counter, TTL 45 days"""
    return f"usage:{business_id}:{billing_cycle_start}"


def daily_review_key(business_id: str, day: str) -> str:
    """daily_review:{business_id}:{YYYY-MM-DD} — set of conversation IDs for MEDIUM confidence."""
    return f"daily_review:{business_id}:{day}"


def retry_queue_key(business_id: str) -> str:
    """retry_queue:{business_id} — list of failed turns awaiting reprocess."""
    return f"retry_queue:{business_id}"


RETRY_QUEUE_INDEX_KEY = "retry_queue:index"

# Sorted set: score = unix due time, member = JSON job payload
ESCALATION_SMS_DUE_KEY = "escalation_sms:due"


def escalation_sms_job_key(gap_id: str) -> str:
    """escalation_sms:job:{gap_id} — job payload + idempotency, TTL 48h after due."""
    return f"escalation_sms:job:{gap_id}"


def workflow_confirmation_key(business_id: str) -> str:
    """workflow_confirmation:{business_id} — FIFO Temporal owner approvals."""
    return f"workflow_confirmation:{business_id}"


def processed_message_key(channel: str, message_id: str) -> str:
    """processed:msg:{channel}:{message_id} — inbound idempotency, TTL 48h."""
    return f"processed:msg:{channel}:{message_id}"


def answered_message_key(channel: str, message_id: str) -> str:
    """answered:{channel}:{message_id} — at-most-one customer reply, TTL 48h.

    Distinct from processed:msg: that claim is released when a turn fails so the
    turn can be retried, whereas this marker must survive the retry to stop a
    second customer-visible reply.
    """
    return f"answered:{channel}:{message_id}"
