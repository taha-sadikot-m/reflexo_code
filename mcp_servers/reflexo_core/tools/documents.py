"""Document tools."""

from __future__ import annotations

from typing import Any

from mcp_servers.reflexo_core.documents.pipeline import run_document_pipeline
from mcp_servers.reflexo_core.utils.db_helpers import business_exists
from mcp_servers.reflexo_core.utils.errors import tool_error


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def process_document(
        business_id: str,
        media_id: str | None = None,
        media_url: str | None = None,
        channel: str = "whatsapp",
        filename: str | None = None,
        document_type_hint: str | None = None,
    ) -> dict[str, Any]:
        """Process a document and extract knowledge / entities."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        if media_id and not media_url:
            return tool_error(
                "MEDIA_DOWNLOAD_UNAVAILABLE",
                "media_id requires Meta MCP; pass media_url for now",
            )
        if not media_url:
            return tool_error("INVALID_INPUT", "Provide media_url (or media_id when Meta MCP is available)")

        try:
            return await run_document_pipeline(
                business_id,
                media_url=media_url,
                filename=filename,
                document_type_hint=document_type_hint,
            )
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Document processing failed: {exc}")
