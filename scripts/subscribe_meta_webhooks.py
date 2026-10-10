#!/usr/bin/env python3
"""Re-subscribe already-connected Instagram / Facebook channels to webhooks.

A channel can hold a perfectly valid token and still receive nothing, because
storing the token and subscribing the Page to webhook fields are two separate
Meta calls. When the subscribe call fails, the connect still reports success
(by design, so the token is not thrown away) and the channel goes quiet.

This repairs that without making the owner redo OAuth.

Required env:
  DATABASE_URL
  ENCRYPTION_KEY

Optional:
  --business-id   limit to one tenant
  --dry-run       resolve Pages and report, subscribe nothing

Tokens are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import httpx

CHANNEL_FIELDS = {
    "instagram_dm": "INSTAGRAM_WEBHOOK_FIELDS",
    "facebook_messenger": "FACEBOOK_WEBHOOK_FIELDS",
}


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"ERROR: {name} is missing in .env")
        sys.exit(1)
    return value


async def _resolve_page(
    client: httpx.AsyncClient,
    version: str,
    token: str,
    channel: str,
    account_id: str,
) -> tuple[str | None, str | None]:
    """Return (page_id, page_token) for a stored channel row.

    Facebook stores the Page id directly. Instagram stores the IG account id, so
    the linked Page has to be found: webhook subscription happens on the Page,
    not on the IG account.
    """
    if channel == "facebook_messenger":
        return account_id, token

    # A Page token identifies the Page itself, which is the common case after
    # OAuth since we store the per-Page token.
    me = await client.get(
        f"https://graph.facebook.com/{version}/me",
        params={"fields": "id,name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if me.status_code == 200:
        me_id = str((me.json() or {}).get("id") or "")
        if me_id and me_id != account_id:
            return me_id, token

    # Otherwise it is a user token: find the Page whose linked IG account matches.
    accounts = await client.get(
        f"https://graph.facebook.com/{version}/me/accounts",
        params={"fields": "id,name,access_token,instagram_business_account{id,username}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if accounts.status_code != 200:
        return None, None

    for page in (accounts.json().get("data") or []):
        linked = (page.get("instagram_business_account") or {}).get("id")
        if linked and str(linked) == account_id:
            return str(page["id"]), page.get("access_token") or token
    return None, None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")

    from mcp_servers.meta_mcp.auth.tokens import decrypt_token, init_fernet
    from onboarding import integrations
    from shared.db import close_pool, init_pool, service_connection

    init_fernet(encryption_key)
    await init_pool(database_url)

    version = os.getenv("META_API_VERSION", "v22.0")
    verify = os.getenv("META_HTTP_VERIFY", "true").lower() not in ("0", "false", "no")

    channels = list(CHANNEL_FIELDS)
    query = """
        SELECT c.business_id, b.name AS business_name, c.channel,
               c.channel_account_id, c.access_token_encrypted
        FROM business_channels c
        JOIN businesses b ON b.id = c.business_id
        WHERE c.channel = ANY($1::channel_type[])
          AND c.is_active = true
          AND ($2::uuid IS NULL OR c.business_id = $2::uuid)
        ORDER BY b.name, c.channel
    """

    try:
        async with service_connection() as conn:
            rows = await conn.fetch(query, channels, args.business_id)

        ok, failed = 0, 0
        if not rows:
            print("No active Instagram or Facebook Page channels found.")
        else:
            async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
                for row in rows:
                    label = f"{row['business_name']} / {row['channel']}"
                    fields = getattr(integrations, CHANNEL_FIELDS[row["channel"]])

                    try:
                        token = decrypt_token(row["access_token_encrypted"])
                    except Exception as exc:
                        print(f"  {label}: cannot decrypt stored token ({exc})")
                        failed += 1
                        continue

                    page_id, page_token = await _resolve_page(
                        client, version, token, row["channel"], str(row["channel_account_id"])
                    )
                    if not page_id or not page_token:
                        print(
                            f"  {label}: could not resolve the Page for account "
                            f"{row['channel_account_id']} — owner may need to reconnect"
                        )
                        failed += 1
                        continue

                    if args.dry_run:
                        print(f"  {label}: would subscribe page {page_id} to [{fields}]")
                        continue

                    try:
                        await integrations.subscribe_page_webhooks(page_id, page_token, fields)
                        print(f"  {label}: subscribed page {page_id} to [{fields}]")
                        ok += 1
                    except Exception as exc:
                        print(f"  {label}: subscribe failed for page {page_id} — {exc}")
                        failed += 1

        from integrations.capabilities import subscribe_instagram_app_comments

        if args.dry_run:
            print("  app/instagram: would subscribe fields [comments]")
            print("\nDry run only, nothing changed.")
            return 0

        try:
            app_result = await subscribe_instagram_app_comments()
            print(
                f"  app/instagram: subscribed comments "
                f"(callback={app_result.get('callback_url')})"
            )
        except Exception as exc:
            print(f"  app/instagram: comments subscribe failed — {exc}")
            failed += 1

        print(f"\nSubscribed {ok} Page channel(s); {failed} failed.")
        if failed:
            print(
                "A failure here usually means a missing permission "
                "(pages_manage_metadata) or that the token no longer grants Page "
                "access. Reconnect that channel and re-run. Instagram comments "
                "also need the app-level instagram/comments subscription."
            )
        return 1 if failed else 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
