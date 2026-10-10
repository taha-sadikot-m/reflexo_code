"""REFLEXO ReAct agent — message orchestrator."""

from __future__ import annotations

__all__ = ["handle_incoming_message"]


async def handle_incoming_message(*args, **kwargs):
    from agent.session import handle_incoming_message as _handle

    return await _handle(*args, **kwargs)
