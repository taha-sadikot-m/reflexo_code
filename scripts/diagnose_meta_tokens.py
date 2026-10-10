#!/usr/bin/env python3
"""Report what each stored Meta token actually is. Read-only, no token material.

Answers the question `refresh_meta_tokens.py` cannot: *why* a send fails with
`(#131005) Access denied` while the token still validates. A long-lived **user**
token passes debug_token yet loses WhatsApp access whenever the same admin
completes a fresh Facebook Login, which is exactly what a Meta OAuth connect
does. A System User token has no such coupling.

For every active row in business_channels this prints token type, validity,
expiry, and the scopes Meta reports — never the token, not even a prefix.

Required env:
  DATABASE_URL, ENCRYPTION_KEY, META_APP_ID, META_APP_SECRET

Usage:
  python -m scripts.diagnose_meta_tokens
  python -m scripts.diagnose_meta_tokens --channel whatsapp
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

from integrations.capabilities import REQUIRED_SCOPES

# A user token is revocable by login churn; WhatsApp must use a system token.
DURABLE_WHATSAPP_TOKEN_TYPES = ("SYSTEM_USER",)


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


def _fmt_expiry(raw: int) -> str:
    if not raw:
        return "never (permanent)"
    at = datetime.fromtimestamp(raw, tz=timezone.utc)
    days = (at - datetime.now(timezone.utc)).total_seconds() / 86400
    return f"{at.isoformat(timespec='seconds')} ({days:.1f} days)"


def _scopes(info: dict[str, Any]) -> list[str]:
    scopes = list(info.get("scopes") or [])
    for granular in info.get("granular_scopes") or []:
        scope = granular.get("scope")
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", default="", help="limit to one tenant")
    parser.add_argument("--channel", default="", help="limit to one channel")
    args = parser.parse_args()

    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    app_id = _require("META_APP_ID")
    app_secret = _require("META_APP_SECRET")

    from mcp_servers.meta_mcp.auth.tokens import decrypt_token, init_fernet
    from shared.db import close_pool, init_pool, service_connection

    init_fernet(encryption_key)
    await init_pool(database_url, application_name="diagnose-meta-tokens")

    where = ["bc.access_token_encrypted IS NOT NULL"]
    params: list[Any] = []
    if args.business_id:
        params.append(args.business_id)
        where.append(f"bc.business_id = ${len(params)}::uuid")
    if args.channel:
        params.append(args.channel)
        where.append(f"bc.channel = ${len(params)}::channel_type")

    async with service_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT bc.business_id, bc.channel, bc.channel_account_id,
                   bc.access_token_encrypted, bc.token_expires_at,
                   bc.is_active, bc.error_message,
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

    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as client:
        for row in rows:
            label = f"{row['business_name']} / {row['channel']}"
            print(f"\n{label}")
            print(f"  account_id  = {row['channel_account_id']}")
            print(f"  is_active   = {row['is_active']}")
            if row["error_message"]:
                print(f"  error       = {row['error_message']}")

            try:
                token = decrypt_token(row["access_token_encrypted"])
            except Exception as exc:
                print(f"  FAIL decrypt failed: {exc}")
                warnings.append(f"{label}: token cannot be decrypted")
                continue

            try:
                info = await debug_token(client, token, app_id, app_secret)
            except Exception as exc:
                print(f"  FAIL debug_token failed: {exc}")
                warnings.append(f"{label}: debug_token failed")
                continue

            token_type = str(info.get("type") or "unknown")
            is_valid = bool(info.get("is_valid"))
            scopes = _scopes(info)

            print(f"  token_type  = {token_type}")
            print(f"  is_valid    = {is_valid}")
            print(f"  expires     = {_fmt_expiry(int(info.get('expires_at') or 0))}")
            print(f"  data_access = {_fmt_expiry(int(info.get('data_access_expires_at') or 0))}")
            print(f"  scopes      = {', '.join(scopes) if scopes else '(none reported)'}")

            if not is_valid:
                reason = (info.get("error") or {}).get("message") or "token is not valid"
                print(f"  FAIL {reason}")
                warnings.append(f"{label}: invalid token — owner must reconnect")
                continue

            missing = [
                scope
                for scope in REQUIRED_SCOPES.get(str(row["channel"]), ())
                if scope not in scopes
            ]
            if missing:
                print(f"  FAIL missing scope(s): {', '.join(missing)}")
                warnings.append(f"{label}: missing {', '.join(missing)}")

            if (
                str(row["channel"]) == "whatsapp"
                and token_type not in DURABLE_WHATSAPP_TOKEN_TYPES
            ):
                print(
                    f"  WARN {token_type} tokens are revoked when the same admin "
                    "logs in again — a Meta OAuth connect does exactly that"
                )
                warnings.append(f"{label}: {token_type} token, replace with a System User token")

    await close_pool()

    print()
    if not warnings:
        print("All stored tokens are valid and scoped; WhatsApp tokens are durable.")
        return 0

    print("Needs attention:")
    for line in warnings:
        print(f"  {line}")
    print()
    print("Fix: Business Settings -> System Users -> Generate token, assign the")
    print("WABA / Page / IG asset and the scope above, no expiry. Then re-run")
    print("scripts/seed_whatsapp_channel.py (or seed_social_channels.py).")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
