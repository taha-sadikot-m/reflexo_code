"""Stage 4 semantic cache T4.3–T4.7 (FakeRedis, injected embeddings)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.semantic_cache import (
    cache_eligible,
    get_cached_response,
    set_cached_response,
)
from shared.redis_keys import semantic_cache_key


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}
        self.clock = 0.0

    def _alive(self, key: str) -> bool:
        exp = self.expires.get(key)
        if exp is not None and self.clock >= exp:
            self.hashes.pop(key, None)
            self.expires.pop(key, None)
            return False
        return True

    async def hgetall(self, key: str):
        if not self._alive(key):
            return {}
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, field: str, value: str):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def expire(self, key: str, ttl: int):
        self.expires[key] = self.clock + int(ttl)


def _vec(seed: float) -> list[float]:
    return [seed, 1.0, 0.0]


@pytest.mark.asyncio
async def test_t4_3_cold_miss():
    redis = FakeRedis()
    hit = await get_cached_response(
        "biz-a",
        "what are your hours",
        query_embedding=_vec(1.0),
        redis=redis,
    )
    assert hit is None


@pytest.mark.asyncio
async def test_t4_4_paraphrase_hit():
    redis = FakeRedis()
    hours_emb = _vec(1.0)
    await set_cached_response(
        "biz-a",
        "what are your hours",
        "We are open 9 to 5.",
        query_embedding=hours_emb,
        redis=redis,
        ttl=3600,
        now=0.0,
    )
    # Near-identical embedding for a paraphrase (hours vs when are you open).
    paraphrase_emb = [1.001, 1.0, 0.0]
    hit = await get_cached_response(
        "biz-a",
        "when are you open",
        query_embedding=paraphrase_emb,
        redis=redis,
        now=1.0,
    )
    assert hit == "We are open 9 to 5."


@pytest.mark.asyncio
async def test_t4_5_isolated_per_business():
    redis = FakeRedis()
    emb = _vec(1.0)
    await set_cached_response(
        "biz-a",
        "hours",
        "Open 9-5 A",
        query_embedding=emb,
        redis=redis,
        now=0.0,
    )
    miss = await get_cached_response(
        "biz-b",
        "hours",
        query_embedding=emb,
        redis=redis,
        now=1.0,
    )
    assert miss is None
    assert semantic_cache_key("biz-a") in redis.hashes
    assert semantic_cache_key("biz-b") not in redis.hashes


@pytest.mark.asyncio
async def test_t4_6_ttl_expiry_miss():
    redis = FakeRedis()
    emb = _vec(1.0)
    await set_cached_response(
        "biz-a",
        "hours",
        "Open 9-5",
        query_embedding=emb,
        redis=redis,
        ttl=1,
        now=0.0,
    )
    redis.clock = 2.0
    miss = await get_cached_response(
        "biz-a",
        "hours",
        query_embedding=emb,
        redis=redis,
        ttl=1,
        now=2.0,
    )
    assert miss is None


def test_t4_7_owner_never_reads_or_writes():
    assert cache_eligible(session_type="owner") is False
    assert cache_eligible(session_type="onboarding") is False
    assert cache_eligible(session_type="customer") is True
    assert cache_eligible(session_type="customer", media_url="https://x") is False
    assert cache_eligible(session_type="customer", message_type="image") is False
    assert cache_eligible(session_type="customer", input_blocked=True) is False


@pytest.mark.asyncio
async def test_threshold_is_configurable(monkeypatch):
    """SEMANTIC_CACHE_THRESHOLD lets a 0.71-similarity paraphrase hit."""
    import agent.semantic_cache as sc

    redis = FakeRedis()
    await set_cached_response(
        "biz-a",
        "what are your business hours",
        "Open 9-6",
        query_embedding=[1.0, 0.0],
        redis=redis,
        now=0.0,
    )
    loose = [0.71, 0.71]

    monkeypatch.setattr(sc, "default_threshold", lambda: 0.92)
    assert await get_cached_response(
        "biz-a", "when are you open", query_embedding=loose, redis=redis, now=1.0
    ) is None

    monkeypatch.setattr(sc, "default_threshold", lambda: 0.65)
    assert await get_cached_response(
        "biz-a", "when are you open", query_embedding=loose, redis=redis, now=1.0
    ) == "Open 9-6"


def test_threshold_default_matches_settings(monkeypatch):
    import agent.semantic_cache as sc

    class _S:
        semantic_cache_threshold = 0.85

    monkeypatch.setattr("agent.settings.get_agent_settings", lambda: _S())
    assert sc.default_threshold() == 0.85


def test_embed_key_falls_back_to_dotenv(monkeypatch):
    """Worker settings never export .env, so embed() must load it itself."""
    import shared.embeddings as emb

    env_file = emb._ROOT / ".env"
    if not env_file.exists():
        pytest.skip("no local .env")
    if "GOOGLE_API_KEY" not in env_file.read_text(encoding="utf-8", errors="ignore"):
        pytest.skip(".env has no GOOGLE_API_KEY")

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(emb, "_dotenv_loaded", False)
    assert emb._google_api_key()


def test_embed_key_missing_still_raises(monkeypatch):
    import shared.embeddings as emb

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(emb, "_dotenv_loaded", True)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY is not set"):
        emb._google_api_key()


@pytest.mark.asyncio
async def test_cache_stores_text_and_response():
    redis = FakeRedis()
    await set_cached_response(
        "biz-a",
        "hours",
        "Open 9-5",
        query_embedding=_vec(1.0),
        redis=redis,
        now=0.0,
    )
    raw = next(iter(redis.hashes[semantic_cache_key("biz-a")].values()))
    entry = json.loads(raw)
    assert entry["text"] == "hours"
    assert entry["response"] == "Open 9-5"
    assert len(entry["embedding"]) == 3
