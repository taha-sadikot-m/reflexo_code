"""Facebook Messenger and Page comment tools."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.redis_keys import fb_post_key
from mcp_servers.meta_mcp.tools.helpers import business_exists
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_get, graph_post
from mcp_servers.meta_mcp.utils.rate_limiter import check_rate_limit
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def send_facebook_message(
        business_id: str,
        to_psid: str,
        text: str,
        human_agent: bool = False,
    ) -> dict[str, Any]:
        """Send a Facebook Messenger message.

        Defaults to messaging_type=RESPONSE, valid when replying inside the
        24-hour window (every webhook-triggered reply). Set human_agent=True to
        send under the HUMAN_AGENT tag instead, which extends the window to 7
        days but requires the Human Agent feature approved on the Meta app.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not to_psid:
            return tool_error("INVALID_RECIPIENT", "to_psid is required")
        if not text or len(text) > 2000:
            return tool_error("INVALID_INPUT", "text is required (max 2000 chars)")

        try:
            await check_rate_limit(business_id, "facebook_send")
            creds = await ensure_valid_token(business_id, "facebook_messenger")
            page_id = creds.get("page_id") or creds["channel_account_id"]

            body: dict[str, Any] = {
                "recipient": {"id": to_psid},
                "message": {"text": text},
            }
            # A tag is only legal with MESSAGE_TAG; pairing it with RESPONSE is
            # rejected as "(#100) Messaging type should be MESSAGE_TAG".
            if human_agent:
                body["messaging_type"] = "MESSAGE_TAG"
                body["tag"] = "HUMAN_AGENT"
            else:
                body["messaging_type"] = "RESPONSE"

            data = await graph_post(
                f"{page_id}/messages",
                creds["access_token"],
                body,
                business_id=business_id,
                channel="facebook_messenger",
            )
            return {
                "message_id": data.get("message_id") or data.get("id"),
                "status": "sent",
                "channel": "facebook_messenger",
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_facebook_message failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def reply_facebook_comment(
        business_id: str,
        comment_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Reply to a comment on the business's Facebook Page post."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not comment_id:
            return tool_error("INVALID_RECIPIENT", "comment_id is required")
        if not text or len(text) > 8000:
            return tool_error("INVALID_INPUT", "text is required (max 8000 chars)")

        try:
            await check_rate_limit(business_id, "facebook_send")
            creds = await ensure_valid_token(business_id, "facebook_messenger")

            data = await graph_post(
                f"{comment_id}/comments",
                creds["access_token"],
                {"message": text},
                business_id=business_id,
                channel="facebook_messenger",
            )
            return {
                "comment_id": data.get("id"),
                "status": "sent",
                "channel": "facebook_comment",
                "in_reply_to": comment_id,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("reply_facebook_comment failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_facebook_post_info(
        business_id: str,
        post_id: str,
    ) -> dict[str, Any]:
        """Get Facebook Page post details for comment context."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not post_id:
            return tool_error("ENTRY_NOT_FOUND", "post_id is required")

        cache_key = fb_post_key(post_id)
        try:
            cached = await get_redis().get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        try:
            await check_rate_limit(business_id, "facebook_send")
            creds = await ensure_valid_token(business_id, "facebook_messenger")
            data = await graph_get(
                post_id,
                creds["access_token"],
                params={
                    "fields": (
                        "message,type,permalink_url,created_time,"
                        "likes.summary(true),comments.summary(true),shares,"
                        "attachments{type,url,media}"
                    ),
                },
                business_id=business_id,
                channel="facebook_messenger",
            )
            likes = (data.get("likes") or {}).get("summary", {})
            comments = (data.get("comments") or {}).get("summary", {})
            attachments = []
            for att in (data.get("attachments") or {}).get("data", []):
                attachments.append(
                    {
                        "type": att.get("type"),
                        "url": att.get("url")
                        or (att.get("media") or {}).get("image", {}).get("src"),
                    }
                )
            result = {
                "post_id": data.get("id", post_id),
                "message": data.get("message"),
                "type": data.get("type"),
                "permalink_url": data.get("permalink_url"),
                "created_time": data.get("created_time"),
                "likes_count": likes.get("total_count", 0),
                "comments_count": comments.get("total_count", 0),
                "shares_count": (data.get("shares") or {}).get("count", 0),
                "attachments": attachments,
            }
            try:
                await get_redis().setex(cache_key, 600, json.dumps(result, default=str))
            except Exception:
                pass
            return result
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("get_facebook_post_info failed")
            return tool_error("ENTRY_NOT_FOUND", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_facebook_comments(
        business_id: str,
        post_id: str,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Get recent comments on a Facebook Page post."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not post_id:
            return tool_error("ENTRY_NOT_FOUND", "post_id is required")

        limit = max(1, min(int(limit or 25), 100))
        try:
            await check_rate_limit(business_id, "facebook_send")
            creds = await ensure_valid_token(business_id, "facebook_messenger")
            data = await graph_get(
                f"{post_id}/comments",
                creds["access_token"],
                params={
                    "fields": (
                        "id,message,from,created_time,comment_count,like_count,"
                        "comments{id,message,from,created_time}"
                    ),
                    "limit": limit,
                },
                business_id=business_id,
                channel="facebook_messenger",
            )
            comments = []
            for c in data.get("data", []):
                comments.append(
                    {
                        "comment_id": c.get("id"),
                        "message": c.get("message"),
                        "from": c.get("from"),
                        "created_time": c.get("created_time"),
                        "comment_count": c.get("comment_count", 0),
                        "like_count": c.get("like_count", 0),
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
            logger.exception("get_facebook_comments failed")
            return tool_error("ENTRY_NOT_FOUND", str(exc))
