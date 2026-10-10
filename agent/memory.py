"""Cross-channel context and long-term memory helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any

from langgraph.store.base import BaseStore

from agent.config import AGENT_CONFIG
from shared.db import tenant_connection

logger = logging.getLogger(__name__)

_MAX_TOPICS = 8
_TOPIC_MAX_LEN = 80
_MEM0_CLIENT: Any | None = None
_MEM0_LOCK = threading.Lock()


async def load_cross_channel_context(
    business_id: str,
    customer_id: str,
    current_channel: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load recent messages from other channels for the same customer."""
    limit = limit or int(AGENT_CONFIG["cross_channel_context_limit"])
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT m.role, m.content, c.channel, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.business_id = $1::uuid
              AND c.customer_id = $2::uuid
              AND c.channel::text <> $3
            ORDER BY m.created_at DESC
            LIMIT $4
            """,
            business_id,
            customer_id,
            current_channel,
            limit,
        )
    return _rows_to_cross_channel_messages(rows)


async def load_owner_cross_channel_context(
    business_id: str,
    current_channel: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load recent owner-session messages from other channels (customer_id IS NULL)."""
    limit = limit or int(AGENT_CONFIG["cross_channel_context_limit"])
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT m.role, m.content, c.channel, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.business_id = $1::uuid
              AND c.customer_id IS NULL
              AND c.channel::text <> $2
            ORDER BY m.created_at DESC
            LIMIT $3
            """,
            business_id,
            current_channel,
            limit,
        )
    return _rows_to_cross_channel_messages(rows)


def _rows_to_cross_channel_messages(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in reversed(list(rows)):
        out.append(
            {
                "role": r["role"],
                "content": r["content"],
                "channel": r["channel"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return out


async def load_recent_messages(
    business_id: str,
    conversation_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    limit = limit or int(AGENT_CONFIG["recent_messages_limit"])
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE conversation_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT $2
            """,
            conversation_id,
            limit,
        )
    return [
        {
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in reversed(list(rows))
    ]


def customer_memory_namespace(business_id: str, customer_id: str) -> tuple[str, str, str]:
    return ("customer_memory", business_id, customer_id)


async def store_customer_memory(
    store: BaseStore,
    business_id: str,
    customer_id: str,
    key: str,
    value: dict[str, Any],
) -> None:
    ns = customer_memory_namespace(business_id, customer_id)
    await store.aput(ns, key, value)


async def load_customer_memory(
    store: BaseStore,
    business_id: str,
    customer_id: str,
    key: str,
) -> dict[str, Any] | None:
    ns = customer_memory_namespace(business_id, customer_id)
    item = await store.aget(ns, key)
    if item is None:
        return None
    return item.value if hasattr(item, "value") else item  # type: ignore[return-value]


def _topic_from_message(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) < 3:
        return None
    return cleaned[:_TOPIC_MAX_LEN]


def _infer_preference_signals(
    message_text: str | None,
    response_text: str | None,
    tool_calls_made: list[str],
) -> dict[str, Any]:
    prefs: dict[str, Any] = {}
    blob = f"{message_text or ''} {response_text or ''}".lower()
    if "cod" in blob or "cash on delivery" in blob:
        prefs["prefers_cod"] = True
    if "upi" in blob or "online payment" in blob:
        prefs["prefers_cod"] = False
    size_match = re.search(r"\bsize\s*[:\-]?\s*(xs|s|m|l|xl|xxl|\d+)\b", blob)
    if size_match:
        prefs["preferred_size"] = size_match.group(1).upper()
    if "create_payment_link" in tool_calls_made:
        prefs["used_payments"] = True
    return prefs


def _satisfaction_heuristic(
    *,
    escalated: bool,
    tool_calls_made: list[str],
    response_text: str | None,
) -> str:
    if escalated or "escalate_to_owner" in tool_calls_made:
        return "needed owner help this turn"
    if "create_payment_link" in tool_calls_made:
        return "positive — payment link offered"
    lower = (response_text or "").lower()
    if any(w in lower for w in ("sorry", "apolog", "cannot", "don't know")):
        return "mild friction this turn"
    return "generally positive, quick conversation"


def merge_customer_memory_profile(
    prior: dict[str, Any] | None,
    *,
    message_text: str | None,
    response_text: str | None,
    channel: str | None,
    tool_calls_made: list[str],
    ai_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build §7.2 long-term memory shape, merging with prior store value."""
    prior = prior or {}
    prefs = dict(prior.get("preferences") or {})
    if isinstance(ai_metadata.get("customer_preferences"), dict):
        prefs.update(ai_metadata["customer_preferences"])
    prefs.update(_infer_preference_signals(message_text, response_text, tool_calls_made))

    prior_summary = str(prior.get("interaction_summary") or "").strip()
    reply_snip = (response_text or "").strip()[:200]
    if prior_summary and reply_snip:
        interaction_summary = f"{prior_summary} | {reply_snip}"[-400:]
    else:
        interaction_summary = (prior_summary or reply_snip)[:400]

    topics = list(prior.get("last_topics") or [])
    topic = _topic_from_message(message_text)
    if topic and topic not in topics:
        topics.append(topic)
    topics = topics[-_MAX_TOPICS:]

    return {
        "preferences": prefs,
        "interaction_summary": interaction_summary,
        "last_topics": topics,
        "satisfaction_signals": _satisfaction_heuristic(
            escalated=bool(ai_metadata.get("escalated")),
            tool_calls_made=tool_calls_made,
            response_text=response_text,
        ),
        "last_channel": channel,
        "last_customer_message": (message_text or "")[:300],
    }


def _mem0_config() -> dict[str, Any]:
    """Build the installed Mem0 2.x configuration.

    Keep this local rather than at import time: tests and Stage 1-5 workers must
    still start when Qdrant is disabled or temporarily unavailable.
    """
    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    qdrant_config: dict[str, Any] = {
        "host": settings.qdrant_host,
        "port": settings.qdrant_port,
        "collection_name": settings.mem0_collection,
        "embedding_model_dims": 768,
    }
    if settings.qdrant_api_key:
        qdrant_config["api_key"] = settings.qdrant_api_key

    return {
        "llm": {
            "provider": "gemini",
            "config": {
                "model": "gemini-2.5-flash",
                "api_key": settings.google_api_key,
                "temperature": 0.1,
            },
        },
        "embedder": {
            "provider": "gemini",
            "config": {
                "model": "models/gemini-embedding-001",
                "api_key": settings.google_api_key,
                "embedding_dims": 768,
            },
        },
        "vector_store": {"provider": "qdrant", "config": qdrant_config},
        "version": "v1.1",
    }


def get_mem0_client() -> Any:
    """Return the process-wide Mem0 client, creating it lazily."""
    global _MEM0_CLIENT
    if _MEM0_CLIENT is not None:
        return _MEM0_CLIENT
    with _MEM0_LOCK:
        if _MEM0_CLIENT is None:
            # Mem0's anonymous telemetry uses its own PostHog client. Disable it
            # before import: customer-memory infrastructure must not emit an
            # undeclared third-party analytics stream.
            os.environ.setdefault("MEM0_TELEMETRY", "False")
            from mem0 import Memory

            _MEM0_CLIENT = Memory.from_config(_mem0_config())
    return _MEM0_CLIENT


def reset_mem0_client() -> None:
    """Drop the lazy singleton (tests and configuration reloads)."""
    global _MEM0_CLIENT
    with _MEM0_LOCK:
        _MEM0_CLIENT = None


def _result_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize Mem0 1.x/2.x result containers into dictionaries."""
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("memories") or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _belongs_to_tenant(item: dict[str, Any], business_id: str, customer_id: str) -> bool:
    """Post-filter even though the same constraints are sent to Qdrant.

    This second gate protects tenant isolation if a backend/provider version
    ignores metadata filters or returns a malformed result.
    """
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return (
        str(metadata.get("business_id") or "") == str(business_id)
        and str(metadata.get("customer_id") or customer_id) == str(customer_id)
    )


async def get_customer_memories(
    customer_id: str,
    query: str,
    business_id: str,
    *,
    limit: int | None = None,
    client: Any | None = None,
) -> list[str]:
    """Return up to five relevant, tenant-safe semantic customer facts.

    Retrieval is deliberately failure-open. Memory improves a reply but must
    never prevent one from being delivered.
    """
    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    if (
        not settings.mem0_enabled
        or not customer_id
        or not business_id
        or not (query or "").strip()
    ):
        return []
    top_k = max(1, min(int(limit or settings.mem0_search_limit), 5))
    try:
        memory = client or await asyncio.to_thread(get_mem0_client)
        payload = await asyncio.to_thread(
            memory.search,
            query=query.strip(),
            top_k=top_k,
            filters={
                "user_id": str(customer_id),
                "business_id": str(business_id),
                "customer_id": str(customer_id),
            },
        )
        facts: list[str] = []
        for item in _result_items(payload):
            if not _belongs_to_tenant(item, business_id, customer_id):
                continue
            text = item.get("memory") or item.get("text")
            if isinstance(text, str) and text.strip():
                facts.append(text.strip())
            if len(facts) >= top_k:
                break
        return facts
    except Exception as exc:
        logger.warning(
            "Mem0 search failed open business=%s customer=%s error=%s",
            business_id,
            customer_id,
            type(exc).__name__,
        )
        return []


async def extract_and_store_memories(
    messages: list[dict[str, str]],
    customer_id: str,
    business_id: str,
    *,
    client: Any | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Extract semantic facts from one completed customer turn into Mem0."""
    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    cleaned = [
        {"role": str(item.get("role") or ""), "content": str(item.get("content") or "").strip()}
        for item in messages
        if isinstance(item, dict)
        and item.get("role") in ("user", "assistant")
        and str(item.get("content") or "").strip()
    ]
    if not settings.mem0_enabled or not customer_id or not business_id or not cleaned:
        return {"results": []}
    try:
        memory = client or await asyncio.to_thread(get_mem0_client)
        result = await asyncio.to_thread(
            memory.add,
            messages=cleaned,
            user_id=str(customer_id),
            metadata={
                "business_id": str(business_id),
                "customer_id": str(customer_id),
            },
        )
        return result if isinstance(result, dict) else {"results": result or []}
    except Exception as exc:
        logger.warning(
            "Mem0 extraction failed business=%s customer=%s error=%s",
            business_id,
            customer_id,
            type(exc).__name__,
        )
        if raise_on_error:
            raise
        return {"results": [], "error": type(exc).__name__}
