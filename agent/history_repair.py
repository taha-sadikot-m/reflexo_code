"""Repair LangGraph chat history with orphaned AIMessage tool_calls."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

ORPHAN_TOOL_RESULT = (
    "Tool call interrupted or not completed in a previous turn. Please retry the action."
)


def _tool_call_id(tc: Any) -> str | None:
    if isinstance(tc, dict):
        return tc.get("id") or None
    return getattr(tc, "id", None) or None


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        return str(tc.get("name") or "unknown")
    return str(getattr(tc, "name", None) or "unknown")


def find_orphaned_tool_calls(messages: list[Any]) -> list[tuple[str, str]]:
    """
    Return (tool_call_id, tool_name) pairs that have no matching ToolMessage.

    Walks messages in order; a tool_call is orphaned if no later ToolMessage
    references its id.
    """
    answered: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage) or getattr(msg, "type", None) == "tool":
            tid = getattr(msg, "tool_call_id", None)
            if tid:
                answered.add(str(tid))

    orphans: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls and not isinstance(msg, AIMessage):
            continue
        for tc in tool_calls:
            tid = _tool_call_id(tc)
            if not tid or tid in answered or tid in seen:
                continue
            orphans.append((tid, _tool_call_name(tc)))
            seen.add(tid)
    return orphans


def synthetic_tool_messages_for_orphans(
    orphans: list[tuple[str, str]],
    *,
    content: str = ORPHAN_TOOL_RESULT,
) -> list[ToolMessage]:
    """Build ToolMessages that close orphaned tool_calls."""
    return [
        ToolMessage(content=content, tool_call_id=tid, name=name)
        for tid, name in orphans
    ]


def repair_orphaned_tool_calls(messages: list[Any]) -> tuple[list[Any], list[ToolMessage]]:
    """
    Return (full_message_list, appended_tool_messages).

    If nothing is orphaned, appended list is empty and messages are unchanged.
    """
    orphans = find_orphaned_tool_calls(messages)
    if not orphans:
        return list(messages), []
    extras = synthetic_tool_messages_for_orphans(orphans)
    return list(messages) + extras, extras


def is_invalid_chat_history_error(exc: BaseException) -> bool:
    text = str(exc)
    return (
        "INVALID_CHAT_HISTORY" in text
        or "tool_calls that do not have a corresponding ToolMessage" in text
        or "do not have a corresponding ToolMessage" in text
    )
