"""Instagram DM and comment tools."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.redis_keys import ig_post_key
from mcp_servers.meta_mcp.tools.helpers import business_exists
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_get, graph_post
from mcp_servers.meta_mcp.utils.rate_limiter import check_rate_limit
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_instagram_dm(
        business_id: str,
        to_ig_user_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Send an Instagram DM (within the 24h messaging window)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not to_ig_user_id:
            return tool_error("INVALID_RECIPIENT", "to_ig_user_id is required")
        if not text or len(text) > 1000:
            return tool_error("INVALID_INPUT", "text is required (max 1000 chars)")

        try:
            await check_rate_limit(business_id, "instagram_api")
            creds = await ensure_valid_token(business_id, "instagram_dm")
            ig_page_id = creds.get("ig_account_id") or creds["channel_account_id"]

            data = await graph_post(
                f"{ig_page_id}/messages",
                creds["access_token"],
                {
                    "recipient": {"id": to_ig_user_id},
                    "message": {"text": text},
                },
                business_id=business_id,
                channel="instagram_dm",
            )
            return {
                "message_id": data.get("message_id") or data.get("id"),
                "status": "sent",
                "channel": "instagram_dm",
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_instagram_dm failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def reply_instagram_comment(
        business_id: str,
        comment_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Reply to a comment on the business's Instagram post."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not comment_id:
            return tool_error("INVALID_RECIPIENT", "comment_id is required")
        if not text or len(text) > 300:
            return tool_error("INVALID_INPUT", "text is required (max 300 chars for comment replies)")

        try:
            await check_rate_limit(business_id, "instagram_api")
            creds = await ensure_valid_token(business_id, "instagram_dm")

            data = await graph_post(
                f"{comment_id}/replies",
                creds["access_token"],
                {"message": text},
                business_id=business_id,
                channel="instagram_dm",
            )
            return {
                "comment_id": data.get("id"),
                "status": "sent",
                "channel": "instagram_comment",
                "in_reply_to": comment_id,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("reply_instagram_comment failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_instagram_post_info(
        business_id: str,
        post_id: str,
    ) -> dict[str, Any]:
        """Get Instagram post details for comment context."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not post_id:
            return tool_error("ENTRY_NOT_FOUND", "post_id is required")

        cache_key = ig_post_key(post_id)
        try:
            cached = await get_redis().get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        try:
            await check_rate_limit(business_id, "instagram_api")
            creds = await ensure_valid_token(business_id, "instagram_dm")
            data = await graph_get(
                post_id,
                creds["access_token"],
                params={
                    "fields": "caption,media_type,permalink,timestamp,like_count,comments_count",
                },
                business_id=business_id,
                channel="instagram_dm",
            )
            result = {
                "post_id": data.get("id", post_id),
                "media_type": data.get("media_type"),
                "caption": data.get("caption"),
                "permalink": data.get("permalink"),
                "timestamp": data.get("timestamp"),
                "like_count": data.get("like_count"),
                "comments_count": data.get("comments_count"),
            }
            try:
                await get_redis().setex(cache_key, 600, json.dumps(result, default=str))
            except Exception:
                pass
            return result
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("get_instagram_post_info failed")
            return tool_error("ENTRY_NOT_FOUND", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_instagram_comments(
        business_id: str,
        post_id: str,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Get recent comments on an Instagram post."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not post_id:
            return tool_error("ENTRY_NOT_FOUND", "post_id is required")

        limit = max(1, min(int(limit or 25), 100))
        try:
            await check_rate_limit(business_id, "instagram_api")
            creds = await ensure_valid_token(business_id, "instagram_dm")
            data = await graph_get(
                f"{post_id}/comments",
                creds["access_token"],
                params={
                    "fields": "id,text,username,timestamp,replies{id,text,username,timestamp}",
                    "limit": limit,
                },
                business_id=business_id,
                channel="instagram_dm",
            )
            comments = []
            for c in data.get("data", []):
                replies = (c.get("replies") or {}).get("data", [])
                comments.append(
                    {
                        "comment_id": c.get("id"),
                        "text": c.get("text"),
                        "username": c.get("username"),
                        "timestamp": c.get("timestamp"),
                        "replies_count": len(replies),
                    }
                )
            return {
                "post_id": post_id,
                "comments": comments,
                "total_count": len(comments),
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("get_instagram_comments failed")
            return tool_error("ENTRY_NOT_FOUND", str(exc))
