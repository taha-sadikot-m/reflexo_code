from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent.logging_setup import configure_worker_logging, correlation_context
from agent.redis_bus import _correlation_for
from agent.tools import _ainvoke_with_session_lock


def _flush_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_rotating_worker_log_has_correlation_and_redacts_httpx(tmp_path: Path):
    path = configure_worker_logging(
        level="INFO",
        log_file="worker.log",
        max_bytes=260,
        backup_count=30,
        root=tmp_path,
    )
    assert path == tmp_path / "worker.log"

    for index in range(20):
        logging.getLogger("agent.test").info("safe rotation record %s", index)
    # Emit assertions after forcing rollover so unrelated suite logging cannot
    # evict the records this test is validating.
    with correlation_context("corr-test-123"):
        logging.getLogger("agent.test").info("EVENT_PROCESS_START channel=instagram_dm")
        logging.getLogger("httpx").info(
            "HTTP Request: GET https://graph.facebook.com/oauth?"
            "code=oauth-secret&fb_exchange_token=token-secret"
        )
    _flush_handlers()

    rendered = "".join(
        file.read_text(encoding="utf-8")
        for file in sorted(tmp_path.glob("worker.log*"))
    )
    assert "corr=corr-test-123" in rendered
    assert "oauth-secret" not in rendered
    assert "token-secret" not in rendered
    assert "[REDACTED]" in rendered
    assert (tmp_path / "worker.log.1").exists()


def test_event_correlation_is_stable_and_does_not_use_message_body():
    event = {
        "channel": "facebook_comment",
        "comment_id": "comment-1",
        "content": {"text": "private customer text"},
    }
    first = _correlation_for("1-0", event)
    changed_body = {**event, "content": {"text": "different private text"}}
    assert _correlation_for("9-0", changed_body) == first
    assert len(first) == 12


@pytest.mark.asyncio
async def test_tool_lifecycle_logs_names_but_not_arguments(caplog):
    class Tool:
        name = "send_instagram_dm"

        async def ainvoke(self, _kwargs):
            return {"status": "sent"}

    caplog.set_level(logging.INFO)
    await _ainvoke_with_session_lock(
        Tool(),
        "meta",
        {"access_token": "must-not-appear", "text": "private reply"},
    )
    text = "\n".join(caplog.messages)
    assert "TOOL_START server=meta tool=send_instagram_dm" in text
    assert "TOOL_END server=meta tool=send_instagram_dm status=sent" in text
    assert "must-not-appear" not in text
    assert "private reply" not in text
