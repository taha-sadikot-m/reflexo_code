"""Tests for orphaned tool_call chat-history repair."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.history_repair import (
    find_orphaned_tool_calls,
    is_invalid_chat_history_error,
    repair_orphaned_tool_calls,
)


def test_find_orphans_when_tool_message_missing() -> None:
    messages = [
        HumanMessage(content="send email"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_oauth_connect_url_tool",
                    "args": {"integration_id": "google-mail"},
                    "id": "5c9f7ccc-defb-4d75-a963-deb6f369006b",
                    "type": "tool_call",
                }
            ],
        ),
    ]
    orphans = find_orphaned_tool_calls(messages)
    assert orphans == [
        ("5c9f7ccc-defb-4d75-a963-deb6f369006b", "get_oauth_connect_url_tool")
    ]


def test_no_orphans_when_tool_message_present() -> None:
    tid = "call-1"
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "send_email", "args": {}, "id": tid, "type": "tool_call"}],
        ),
        ToolMessage(content='{"sent": true}', tool_call_id=tid, name="send_email"),
    ]
    assert find_orphaned_tool_calls(messages) == []
    repaired, extras = repair_orphaned_tool_calls(messages)
    assert extras == []
    assert len(repaired) == len(messages)


def test_repair_appends_synthetic_tool_messages() -> None:
    tid = "orphan-id"
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_oauth_connect_url_tool", "args": {}, "id": tid, "type": "tool_call"}
            ],
        ),
    ]
    repaired, extras = repair_orphaned_tool_calls(messages)
    assert len(extras) == 1
    assert extras[0].tool_call_id == tid
    assert extras[0].name == "get_oauth_connect_url_tool"
    assert "interrupted" in extras[0].content.lower() or "retry" in extras[0].content.lower()
    assert find_orphaned_tool_calls(repaired) == []


def test_is_invalid_chat_history_error() -> None:
    exc = ValueError(
        "Found AIMessages with tool_calls that do not have a corresponding ToolMessage. "
        "For troubleshooting, visit: https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CHAT_HISTORY"
    )
    assert is_invalid_chat_history_error(exc)
    assert not is_invalid_chat_history_error(ValueError("something else"))
