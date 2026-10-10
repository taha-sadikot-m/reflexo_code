"""Stage 3 Langfuse handler factory (no live Langfuse)."""

from __future__ import annotations

from agent.observability import (
    build_langfuse_callbacks,
    estimate_cost_usd,
    get_langfuse_handler,
    merge_token_usage,
)


def test_no_keys_returns_empty_callbacks(monkeypatch):
    class _S:
        langfuse_public_key = ""
        langfuse_secret_key = ""
        langfuse_host = "https://cloud.langfuse.com"

    monkeypatch.setattr("agent.observability.get_agent_settings", lambda: _S())
    assert get_langfuse_handler(business_id="b1") is None
    assert build_langfuse_callbacks(business_id="b1") == []


def test_handler_passes_tags_and_metadata(monkeypatch):
    captured: dict = {}

    class _Handler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _S:
        langfuse_public_key = "pk-test"
        langfuse_secret_key = "sk-test"
        langfuse_host = "https://cloud.langfuse.com"

    monkeypatch.setattr("agent.observability.get_agent_settings", lambda: _S())

    import sys
    import types

    fake_mod = types.ModuleType("langfuse.langchain")
    fake_mod.CallbackHandler = _Handler
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_mod)

    handler = get_langfuse_handler(
        trace_name="inquiry_agent",
        business_id="biz-9",
        session_id="biz-9:c:whatsapp",
        user_id="cust",
        metadata={"specialist": "inquiry_agent", "route_method": "llm", "channel": "whatsapp"},
    )
    assert handler is not None
    assert captured["trace_name"] == "inquiry_agent"
    assert "reflexo" in captured["tags"]
    assert "business:biz-9" in captured["tags"]
    assert captured["metadata"]["specialist"] == "inquiry_agent"
    assert captured["metadata"]["route_method"] == "llm"
    assert captured["session_id"] == "biz-9:c:whatsapp"


def test_merge_and_cost():
    merged = merge_token_usage(
        {"prompt": 10, "completion": 5, "total": 15},
        {"prompt": 20, "completion": 5, "total": 25},
    )
    assert merged == {"prompt": 30, "completion": 10, "total": 40}
    cost = estimate_cost_usd(merged)
    assert cost is not None
    assert cost > 0
