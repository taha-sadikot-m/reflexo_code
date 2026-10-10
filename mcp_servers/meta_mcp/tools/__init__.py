"""Register all Meta MCP tool modules."""

from __future__ import annotations

from mcp_servers.meta_mcp.tools import (
    calling,
    common,
    facebook,
    instagram,
    templates,
    whatsapp,
)


def register_all(mcp) -> None:
    whatsapp.register(mcp)
    instagram.register(mcp)
    facebook.register(mcp)
    common.register(mcp)
    calling.register(mcp)
    templates.register(mcp)
