"""Per-business Redis semantic response cache (MVP Stage 4 / §1.9 / §8.5)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable

from shared.redis_keys import semantic_cache_key

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.92
CACHE_TTL_STATIC = 86400
CACHE_TTL_DYNAMIC = 3600

EmbedFn = Callable[[str], Awaitable[list[float]]]


def default_threshold() -> float:
    """Configured cosine floor, falling back to the spec default."""
    try:
        from agent.settings import get_agent_settings

        return float(get_agent_settings().semantic_cache_threshold)
    except Exception:
        return SIMILARITY_THRESHOLD


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        xf = float(x)
        yf = float(y)
        dot += xf * yf
        na += xf * xf
        nb += yf * yf
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def cache_eligible(
    *,
    session_type: str,
    message_type: str = "text",
    media_url: str | None = None,
    input_blocked: bool = False,
    enabled: bool = True,
) -> bool:
    """Owner/onboarding, media, non-text, and injection-blocked turns skip cache."""
    if not enabled:
        return False
    if session_type in ("owner", "onboarding"):
        return False
    if (message_type or "text") != "text":
        return False
    if media_url:
        return False
    if input_blocked:
        return False
    return True


def _decode_hash_map(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in dict(raw).items():
        k = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else str(key)
        v = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)
        out[k] = v
    return out


def _redis():
    from shared.redis_client import get_redis

    return get_redis()


async def embed_for_cache(
    text: str,
    *,
    embed_fn: EmbedFn | None = None,
) -> list[float]:
    if embed_fn is not None:
        return await embed_fn(text)
    from shared.embeddings import embed

    return await embed(text, task_type="retrieval_query")


async def get_cached_response(
    business_id: str,
    query: str,
    *,
    query_embedding: list[float] | None = None,
    redis: Any | None = None,
    threshold: float | None = None,
    ttl: int = CACHE_TTL_DYNAMIC,
    now: float | None = None,
    embed_fn: EmbedFn | None = None,
) -> str | None:
    """Return a cached reply if cosine similarity >= threshold."""
    if not business_id or not (query or "").strip():
        return None
    floor = default_threshold() if threshold is None else float(threshold)
    try:
        embedding = query_embedding
        if embedding is None:
            embedding = await embed_for_cache(query.strip(), embed_fn=embed_fn)
        client = redis if redis is not None else _redis()
        key = semantic_cache_key(business_id)
        entries = _decode_hash_map(await client.hgetall(key))
        clock = time.time() if now is None else now
        for _entry_id, raw in entries.items():
            try:
                entry = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            ts = float(entry.get("ts") or 0)
            if ts and ttl > 0 and (clock - ts) > ttl:
                continue
            cached_emb = entry.get("embedding") or []
            if cosine_similarity(embedding, [float(x) for x in cached_emb]) >= floor:
                text = entry.get("response")
                if isinstance(text, str) and text.strip():
                    return text
    except Exception as exc:
        logger.warning("Semantic cache get failed: %s", exc)
    return None


async def set_cached_response(
    business_id: str,
    query: str,
    response: str,
    *,
    query_embedding: list[float] | None = None,
    redis: Any | None = None,
    is_static: bool = False,
    ttl: int | None = None,
    now: float | None = None,
    embed_fn: EmbedFn | None = None,
) -> None:
    if not business_id or not (query or "").strip() or not (response or "").strip():
        return
    try:
        embedding = query_embedding
        if embedding is None:
            embedding = await embed_for_cache(query.strip(), embed_fn=embed_fn)
        client = redis if redis is not None else _redis()
        key = semantic_cache_key(business_id)
        entry_id = hashlib.md5(query.strip().encode("utf-8")).hexdigest()[:12]
        ttl_s = CACHE_TTL_STATIC if is_static else CACHE_TTL_DYNAMIC
        if ttl is not None:
            ttl_s = int(ttl)
        entry = {
            "text": query.strip(),
            "query": query.strip(),
            "embedding": embedding,
            "response": response,
            "ts": time.time() if now is None else now,
        }
        await client.hset(key, entry_id, json.dumps(entry))
        await client.expire(key, ttl_s)
    except Exception as exc:
        logger.warning("Semantic cache set failed: %s", exc)
