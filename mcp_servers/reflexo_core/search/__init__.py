"""Embedding helpers — re-export shared + init hook."""

from __future__ import annotations

from shared.embeddings import embed, placeholder_embedding


async def init_embeddings() -> None:
    """No heavy local model to load; google-generativeai uses API at call time."""
    return None


__all__ = ["embed", "placeholder_embedding", "init_embeddings"]
