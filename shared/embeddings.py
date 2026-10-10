"""Embedding helpers for knowledge_entries / structured_entities (768-d)."""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from typing import Sequence

_ROOT = Path(__file__).resolve().parents[1]
_dotenv_loaded = False


def _google_api_key() -> str:
    """Resolve the key from the environment, falling back to the repo .env once.

    Callers that configure settings via pydantic-settings (e.g. the agent
    worker) never export .env into os.environ, so the fallback keeps embedding
    working regardless of the entrypoint.
    """
    global _dotenv_loaded

    key = os.getenv("GOOGLE_API_KEY", "")
    if not key and not _dotenv_loaded:
        _dotenv_loaded = True
        try:
            from dotenv import load_dotenv

            load_dotenv(_ROOT / ".env")
        except Exception:
            pass
        key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set")
    return key


@lru_cache(maxsize=256)
def _embed_cached(api_key: str, text: str, task_type: str) -> tuple[float, ...]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    # task_type maps to embedding task where supported
    config = types.EmbedContentConfig(
        output_dimensionality=768,
        task_type=task_type,
    )
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=config,
    )
    embedding: Sequence[float] = result.embeddings[0].values
    if len(embedding) != 768:
        raise ValueError(f"Expected 768-dim embedding, got {len(embedding)}")
    return tuple(embedding)


async def embed(text: str, *, task_type: str = "retrieval_document") -> list[float]:
    """Generate a cached 768-d embedding without blocking the async event loop.

    Spec originally cited text-embedding-004; current Gemini API exposes
    gemini-embedding-001 with output_dimensionality=768 to match our schema.
    """
    embedding = await asyncio.to_thread(
        _embed_cached,
        _google_api_key(),
        text.strip(),
        task_type,
    )
    return list(embedding)


def placeholder_embedding() -> list[float]:
    """Deterministic placeholder for tests (not for production search)."""
    return [0.0] * 768
