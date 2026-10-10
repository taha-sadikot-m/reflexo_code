"""BGE cross-encoder rerank after hybrid RRF (MVP Stage 4 / §1.7).

Loads BAAI/bge-reranker-v2-m3 lazily. If sentence-transformers/torch is missing
(common on Python 3.14), returns the original candidate prefix (no-op).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

RERANK_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
RERANK_FETCH = 20

_model: Any = None
_load_attempted = False
_logged_noop = False


def _load_cross_encoder() -> Any | None:
    global _model, _load_attempted, _logged_noop
    if _model is not None:
        return _model
    if _load_attempted:
        return None
    _load_attempted = True
    try:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(RERANK_MODEL_NAME, max_length=512)
        logger.info("Loaded reranker %s", RERANK_MODEL_NAME)
        return _model
    except Exception as exc:
        if not _logged_noop:
            logger.warning(
                "Reranker unavailable (%s) — using RRF order truncated to top_k",
                exc,
            )
            _logged_noop = True
        return None


def _score_pairs(pairs: list[tuple[str, str]]) -> list[float]:
    """Synchronous scoring hook (mocked in tests)."""
    model = _load_cross_encoder()
    if model is None:
        raise RuntimeError("reranker model not loaded")
    scores = model.predict(pairs)
    return [float(s) for s in scores]


def rerank_results(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 5,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (ranked[:top_k], used_cross_encoder)."""
    if not results:
        return [], False
    k = max(1, int(top_k))
    if len(results) <= k:
        return list(results), False

    pairs = [
        (
            query,
            str(r.get("answer") or r.get("name") or r.get("description") or ""),
        )
        for r in results
    ]
    try:
        scores = _score_pairs(pairs)
    except Exception as exc:
        logger.debug("Rerank predict failed: %s", exc)
        return list(results)[:k], False

    ranked = list(results)
    for i, result in enumerate(ranked):
        result["rerank_score"] = float(scores[i]) if i < len(scores) else 0.0
    ranked.sort(key=lambda x: float(x.get("rerank_score") or 0), reverse=True)
    return ranked[:k], True


async def rerank_results_async(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 5,
) -> tuple[list[dict[str, Any]], bool]:
    return await asyncio.to_thread(rerank_results, query, results, top_k)
