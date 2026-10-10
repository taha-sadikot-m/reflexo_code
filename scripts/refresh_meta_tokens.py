#!/usr/bin/env python3
"""Inspect stored Meta tokens, record their expiry, and upgrade short-lived ones.

Seeded channel rows carry token_expires_at = NULL. is_token_expiring_soon(None)
returns False, so refresh_and_cache() never runs and a temporary token simply
dies mid-conversation with a 401. This script fixes that by asking Graph what
each stored token actually is, then writing the real expiry back.

For every active row in business_channels:
  1. debug_token  -> is the token valid, when does it expire, what type is it
  2. short-lived  -> exchange via fb_exchange_token for a ~60 day token
  3. store        -> re-encrypt the new token and persist token_expires_at
  4. invalidate   -> drop the Redis creds cache so the next send re-reads

A token reported with expires_at = 0 never expires (System User token). That is
the durable setup: generate one in Business Settings -> System Users, give it
the Page/IG/WhatsApp assets, and it survives password changes and re-auths.

Required env:
  DATABASE_URL
  ENCRYPTION_KEY
  META_APP_ID
  META_APP_SECRET

Optional:
  REDIS_URL

Usage:
  python -m scripts.refresh_meta_tokens                    # all businesses
  python -m scripts.refresh_meta_tokens --business-id ...  # one tenant
  python -m scripts.refresh_meta_tokens --dry-run          # report only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# Meta returns 60 days for a long-lived exchange. Anything under this is worth
# upgrading; anything above is already long-lived.
LONG_LIVED_THRESHOLD_DAYS = 30


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"ERROR: {name} is missing in .env")
        sys.exit(1)
    return value


def _api_version() -> str:
    return (os.getenv("META_API_VERSION") or "v22.0").strip()


def _verify() -> bool:
    return (os.getenv("META_HTTP_VERIFY") or "true").strip().lower() not in ("false", "0", "no")


def _fmt(expires_at: datetime | None) -> str:
    if expires_at is None:
        return "never"
    delta = expires_at - datetime.now(timezone.utc)
    days = delta.total_seconds() / 86400
    return f"{expires_at.isoformat(timespec='seconds')} ({days:.1f} days)"


async def debug_token(
    client: httpx.AsyncClient,
    token: str,
    app_id: str,
    app_secret: str,
) -> dict[str, Any]:
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/debug_token",
        params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
    )
    response.raise_for_status()
    return (response.json() or {}).get("data") or {}


async def exchange_for_long_lived(
    client: httpx.AsyncClient,
    token: str,
    app_id: str,
    app_secret: str,
) -> tuple[str, int]:
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        },
    )
    response.raise_for_status()
    data = response.json()
    return data["access_token"], int(data.get("expires_in") or 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", default="", help="limit to one tenant")
    parser.add_argument("--channel", default="", help="limit to one channel")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    app_id = _require("META_APP_ID")
    app_secret = _require("META_APP_SECRET")

    from mcp_servers.meta_mcp.auth.tokens import decrypt_token, encrypt_token, init_fernet
    from mcp_servers.meta_mcp.redis_keys import meta_creds_key
    from shared.db import close_pool, init_pool, service_connection
    from shared.redis_client import close_redis, get_redis, init_redis

    init_fernet(encryption_key)
    await init_pool(database_url, application_name="refresh-meta-tokens")

    where = ["access_token_encrypted IS NOT NULL"]
    params: list[Any] = []
    if args.business_id:
        params.append(args.business_id)
        where.append(f"business_id = ${len(params)}::uuid")
    if args.channel:
        params.append(args.channel)
        where.append(f"channel = ${len(params)}::channel_type")

    async with service_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT bc.id, bc.business_id, bc.channel, bc.channel_account_id,
                   bc.access_token_encrypted, bc.token_expires_at, bc.is_active,
                   b.name AS business_name
            FROM business_channels bc
            JOIN businesses b ON b.id = bc.business_id
            WHERE {" AND ".join(where)}
            ORDER BY b.name, bc.channel
            """,
            *params,
        )

    if not rows:
        print("No channel rows matched.")
        await close_pool()
        return 0

    updated: list[tuple[str, str]] = []
    problems: list[tuple[str, str]] = []

    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as client:
        for row in rows:
            label = f"{row['business_name']} / {row['channel']}"
            try:
                token = decrypt_token(row["access_token_encrypted"])
            except Exception as exc:
                problems.append((label, f"decrypt failed: {exc}"))
                continue

            try:
                info = await debug_token(client, token, app_id, app_secret)
            except Exception as exc:
                problems.append((label, f"debug_token failed: {exc}"))
                continue

            if not info.get("is_valid"):
                reason = (info.get("error") or {}).get("message") or "token is not valid"
                problems.append((label, f"{reason} — owner must reconnect this channel"))
                continue

            raw_expiry = int(info.get("expires_at") or 0)
            token_type = info.get("type") or "unknown"
            expires_at = (
                datetime.fromtimestamp(raw_expiry, tz=timezone.utc) if raw_expiry else None
            )
            remaining_days = (
                (expires_at - datetime.now(timezone.utc)).total_seconds() / 86400
                if expires_at
                else None
            )

            print(f"{label}: type={token_type} expires={_fmt(expires_at)}")

            new_token = token
            if token_type == "SYSTEM_USER":
                # fb_exchange_token hands back a USER token, which login churn can
                # revoke. Downgrading a permanent System User token is how WhatsApp
                # lost access after an unrelated Instagram connect.
                print("  permanent System User token — left untouched")
            elif remaining_days is not None and remaining_days < LONG_LIVED_THRESHOLD_DAYS:
                try:
                    new_token, expires_in = await exchange_for_long_lived(
                        client, token, app_id, app_secret
                    )
                    expires_at = (
                        datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + expires_in,
                            tz=timezone.utc,
                        )
                        if expires_in
                        else None
                    )
                    print(f"  exchanged for long-lived token, expires={_fmt(expires_at)}")
                except Exception as exc:
                    problems.append((label, f"exchange failed: {exc}"))

            if args.dry_run:
                continue

            async with service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE business_channels
                    SET access_token_encrypted = $1,
                        token_expires_at = $2,
                        is_active = true,
                        error_message = NULL,
                        updated_at = now()
                    WHERE id = $3
                    """,
                    encrypt_token(new_token),
                    expires_at,
                    row["id"],
                )
            updated.append((label, _fmt(expires_at)))

    if not args.dry_run and updated:
        try:
            await init_redis(os.getenv("REDIS_URL", "redis://localhost:6379"))
            redis = get_redis()
            for row in rows:
                await redis.delete(meta_creds_key(str(row["business_id"]), row["channel"]))
            await close_redis()
            print("OK Redis creds caches invalidated")
        except Exception as exc:
            print(f"WARN Redis cache invalidate skipped: {exc}")

    await close_pool()

    print()
    if args.dry_run:
        print("Dry run — nothing was written.")
    else:
        print(f"Updated {len(updated)} channel row(s):")
        for label, expiry in updated:
            print(f"  {label} -> expires {expiry}")

    if problems:
        print()
        print("Needs attention:")
        for label, detail in problems:
            print(f"  {label}: {detail}")
        print()
        print("A token that cannot be revived must be replaced. Prefer a System User")
        print("token (Business Settings -> System Users -> Generate token, no expiry)")
        print("so this stops recurring, then re-run scripts/seed_social_channels.py.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
