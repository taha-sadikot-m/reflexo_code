"""Register all tool modules."""

from __future__ import annotations

from mcp_servers.reflexo_core.tools import (
    config,
    conversations,
    customers,
    documents,
    dynamic,
    entities,
    gaps,
    knowledge,
    onboarding,
    workflows,
)


def register_all(mcp) -> None:
    knowledge.register(mcp)
    entities.register(mcp)
    config.register(mcp)
    customers.register(mcp)
    conversations.register(mcp)
    workflows.register(mcp)
    gaps.register(mcp)
    dynamic.register(mcp)
    documents.register(mcp)
    onboarding.register(mcp)
