#!/usr/bin/env python3
"""Post a signed, production-shaped comment webhook at the local Meta MCP server.

Meta only delivers dashboard test payloads for Page `feed` while the app is
unpublished, so real comments never reach the stack. This injects the payload
Meta *would* send, letting every hop after delivery be exercised for real:
signature check, business resolution, Redis handoff, agent, Graph reply.

Usage:
    python scripts/inject_meta_comment.py
    python scripts/inject_meta_comment.py --comment-id 1234_5678 --message "Price?"
    python scripts/inject_meta_comment.py --channel instagram_comment
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DEFAULT_PAGE_ID = "1094117093787703"
DEFAULT_IG_ACCOUNT_ID = "17841400000000000"


def facebook_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror a Page `feed` change for an added comment."""
    value: dict[str, Any] = {
        "item": "comment",
        "verb": "add",
        "comment_id": args.comment_id,
        "post_id": args.post_id,
        "created_time": int(time.time()),
        "message": args.message,
        "from": {"id": args.sender_id, "name": args.sender_name},
    }
    if args.parent_id:
        value["parent_id"] = args.parent_id
    return {
        "object": "page",
        "entry": [
            {
                "id": args.page_id,
                "time": int(time.time()),
                "changes": [{"field": "feed", "value": value}],
            }
        ],
    }


def instagram_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror an Instagram `comments` change."""
    value: dict[str, Any] = {
        "id": args.comment_id,
        "text": args.message,
        "timestamp": int(time.time()),
        "from": {"id": args.sender_id, "username": args.sender_name},
        "media": {"id": args.post_id},
    }
    if args.parent_id:
        value["parent_id"] = args.parent_id
    return {
        "object": "instagram",
        "entry": [
            {
                "id": args.page_id,
                "time": int(time.time()),
                "changes": [{"field": "comments", "value": value}],
            }
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        choices=("facebook_comment", "instagram_comment"),
        default="facebook_comment",
    )
    parser.add_argument(
        "--page-id",
        default="",
        help="Page ID for facebook_comment, IG account ID for instagram_comment",
    )
    parser.add_argument("--comment-id", default="")
    parser.add_argument("--post-id", default="")
    parser.add_argument("--parent-id", default="")
    parser.add_argument("--sender-id", default="")
    parser.add_argument("--sender-name", default="Injected Tester")
    parser.add_argument("--message", default="Is this still available?")
    parser.add_argument(
        "--url",
        default="",
        help="Webhook URL (defaults to local Meta MCP on META_MCP_PORT)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the signed request without sending it",
    )
    args = parser.parse_args()

    is_ig = args.channel == "instagram_comment"
    stamp = int(time.time())

    if not args.page_id:
        args.page_id = DEFAULT_IG_ACCOUNT_ID if is_ig else DEFAULT_PAGE_ID
    if not args.post_id:
        args.post_id = f"{args.page_id}_injectpost{stamp}"
    if not args.comment_id:
        args.comment_id = f"{args.post_id}_injectcomment{stamp}"
    if not args.sender_id:
        args.sender_id = "injected_sender_1"
    if not args.url:
        port = (os.getenv("META_MCP_PORT") or "8021").strip()
        args.url = f"http://127.0.0.1:{port}/webhook"

    return args


def main() -> int:
    args = parse_args()

    app_secret = (os.getenv("META_APP_SECRET") or "").strip()
    if not app_secret:
        print("META_APP_SECRET is not set in .env — the server would return 401")
        return 2

    builder = instagram_payload if args.channel == "instagram_comment" else facebook_payload
    payload = builder(args)

    # Sign the exact bytes sent; any re-serialization would break the digest.
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    signature = f"sha256={digest}"

    print(f"POST {args.url}")
    print(f"channel     {args.channel}")
    print(f"account     {args.page_id}")
    print(f"comment_id  {args.comment_id}")
    print(f"post_id     {args.post_id}")
    print(f"sender_id   {args.sender_id}")
    print(f"signature   {signature}")
    print(f"body        {body.decode()}")

    if args.dry_run:
        print("\ndry run — not sent")
        return 0

    try:
        response = httpx.post(
            args.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"\nrequest failed: {type(exc).__name__}: {exc}")
        print("Is Meta MCP running? python -m mcp_servers.meta_mcp.server")
        return 1

    print(f"\nHTTP {response.status_code} {response.text[:200]}")
    if response.status_code == 401:
        print("Signature rejected — META_APP_SECRET here differs from the server's")
        return 1
    if response.status_code != 200:
        return 1

    print("\nAccepted. Check for these hops:")
    print("  meta-mcp : Webhook POST ... fields=['feed'] / ['comments']")
    print(f"  meta-mcp : Queued event type={args.channel}")
    print("  worker   : logs/agent-worker.log ROUTE_COMPLETE then reply tool")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
