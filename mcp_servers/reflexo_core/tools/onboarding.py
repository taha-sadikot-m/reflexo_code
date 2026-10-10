"""Onboarding progress and completion MCP tools."""

from __future__ import annotations

from typing import Any

from mcp_servers.reflexo_core.utils.db_helpers import business_exists
from mcp_servers.reflexo_core.utils.errors import tool_error
from onboarding.flow import complete_onboarding as complete_onboarding_impl
from onboarding.progress import (
    apply_progress_updates,
    load_onboarding_progress,
    save_onboarding_progress,
)


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_onboarding_progress(business_id: str) -> dict[str, Any]:
        """Get current onboarding progress checklist for this business."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        progress = await load_onboarding_progress(business_id)
        return {
            "progress": progress.to_dict(),
            "completion_percentage": progress.get_completion_percentage(),
            "required_remaining": progress.get_required_remaining(),
            "recommended_remaining": progress.get_recommended_remaining(),
            "is_essential_complete": progress.is_essential_complete(),
            "summary": progress.generate_progress_summary(),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def update_onboarding_progress(
        business_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Update the onboarding progress tracker after saving each piece of information.

        Valid keys include business_name, vertical, owner_name, what_they_sell,
        business_hours, return_policy, delivery_info, payment_methods, entities_added,
        entity_names, faqs_added, faq_topics, persona_*, *_connected, skipped_steps, etc.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not isinstance(updates, dict) or not updates:
            return tool_error("INVALID_INPUT", "updates must be a non-empty object")

        progress = await load_onboarding_progress(business_id)
        apply_progress_updates(progress, updates)
        await save_onboarding_progress(business_id, progress)

        return {
            "status": "updated",
            "completion_percentage": progress.get_completion_percentage(),
            "required_remaining": progress.get_required_remaining(),
            "recommended_remaining": progress.get_recommended_remaining(),
            "is_essential_complete": progress.is_essential_complete(),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def complete_onboarding(business_id: str) -> dict[str, Any]:
        """
        Mark onboarding as complete. Call ONLY when all required info is collected
        and the owner confirms they are done. Seeds vertical knowledge and activates
        the AI employee for customers.
        """
        return await complete_onboarding_impl(business_id)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def generate_instagram_oauth_url(business_id: str) -> dict[str, Any]:
        """
        Generate a Meta Login URL so the owner can connect Instagram DMs.
        Paste the returned URL in your reply on the current channel.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            from onboarding.integrations import generate_instagram_oauth_url as build_url

            url = build_url(business_id)
            return {
                "oauth_url": url,
                "integration": "instagram",
                "agent_guidance": (
                    "Paste this FULL oauth_url in your reply on the current channel. After they "
                    "finish Meta Login, confirm Instagram is connected and continue onboarding."
                ),
            }
        except Exception as exc:
            return tool_error("OAUTH_URL_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def generate_facebook_oauth_url(business_id: str) -> dict[str, Any]:
        """
        Generate a Meta Login URL so the owner can connect Facebook Messenger.
        Paste the returned URL in your reply on the current channel.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            from onboarding.integrations import generate_facebook_oauth_url as build_url

            url = build_url(business_id)
            return {
                "oauth_url": url,
                "integration": "facebook",
                "agent_guidance": (
                    "Paste this FULL oauth_url in your reply on the current channel. After they "
                    "finish Meta Login, confirm Facebook Messenger is connected and continue "
                    "onboarding."
                ),
            }
        except Exception as exc:
            return tool_error("OAUTH_URL_FAILED", str(exc))
