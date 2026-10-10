"""Stage 4 BGE reranker (mocked CrossEncoder — no 1.1GB model in CI)."""

from __future__ import annotations

import time

from mcp_servers.reflexo_core.reranker import rerank_results


def _twenty_kb_rows() -> list[dict]:
    return [
        {
            "id": str(i),
            "question": f"q{i}",
            "answer": f"answer {i}",
            "rrf_score": 1.0 - (i * 0.01),
        }
        for i in range(20)
    ]


def test_t4_1_rerank_top1_differs_from_rrf(monkeypatch):
    rows = _twenty_kb_rows()
    assert rows[0]["id"] == "0"

    def _scores(pairs):
        # Last RRF row is the most relevant according to the cross-encoder.
        return [0.05] * 19 + [0.99]

    monkeypatch.setattr("mcp_servers.reflexo_core.reranker._score_pairs", _scores)
    ranked, used = rerank_results("return policy", rows, top_k=5)
    assert used is True
    assert ranked[0]["id"] == "19"
    assert ranked[0]["rerank_score"] == 0.99
    assert len(ranked) == 5
    assert ranked[0]["id"] != rows[0]["id"]


def test_t4_2_mocked_predict_under_300ms(monkeypatch):
    rows = _twenty_kb_rows()

    def _scores(pairs):
        return [float(i) for i in range(len(pairs))]

    monkeypatch.setattr("mcp_servers.reflexo_core.reranker._score_pairs", _scores)
    t0 = time.perf_counter()
    ranked, used = rerank_results("hours", rows, top_k=5)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert used is True
    assert ranked[0]["id"] == "19"
    assert elapsed_ms < 300


def test_rerank_skips_when_few_results():
    rows = [{"id": "a", "answer": "one"}, {"id": "b", "answer": "two"}]
    ranked, used = rerank_results("q", rows, top_k=5)
    assert used is False
    assert [r["id"] for r in ranked] == ["a", "b"]


def test_rerank_noop_when_predict_unavailable(monkeypatch):
    rows = _twenty_kb_rows()

    def _boom(_pairs):
        raise RuntimeError("model not loaded")

    monkeypatch.setattr("mcp_servers.reflexo_core.reranker._score_pairs", _boom)
    ranked, used = rerank_results("q", rows, top_k=5)
    assert used is False
    assert [r["id"] for r in ranked] == [str(i) for i in range(5)]
