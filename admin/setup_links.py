"""Hashed setup-link tokens and the owner/admin integration catalog."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote

from fastapi import HTTPException

from api.settings import get_api_settings

TOKEN_BYTES = 32
DEFAULT_TTL_HOURS = 7 * 24

CATALOG: list[dict[str, str]] = [
    {
        "id": "whatsapp",
        "label": "WhatsApp",
        "kind": "channel",
        "channel": "whatsapp",
        "connect": "seed",
        "note": "Uses the shared REFLEXO WhatsApp number. Per-business Cloud API is not available yet.",
    },
    {
        "id": "instagram",
        "label": "Instagram",
        "kind": "channel",
        "channel": "instagram_dm",
        "connect": "oauth_meta",
        "note": "Owner signs in with Meta and grants messaging access.",
    },
    {
        "id": "facebook",
        "label": "Facebook Messenger",
        "kind": "channel",
        "channel": "facebook_messenger",
        "connect": "oauth_meta",
        "note": "Owner signs in as a Page admin.",
    },
    {
        "id": "web_widget",
        "label": "Web widget",
        "kind": "channel",
        "channel": "web_widget",
        "connect": "embed",
        "note": "Paste the embed snippet on the business website.",
    },
    {
        "id": "gmail",
        "label": "Gmail",
        "kind": "connected_account",
        "connect": "oauth_channel",
        "note": "Google consent for View and Send.",
    },
    {
        "id": "outlook",
        "label": "Outlook",
        "kind": "connected_account",
        "connect": "oauth_channel",
        "note": "Microsoft consent for View and Send.",
    },
    {
        "id": "slack",
        "label": "Slack",
        "kind": "connected_account",
        "connect": "oauth_channel",
        "note": "Connect a Slack workspace.",
    },
    {
        "id": "razorpay",
        "label": "Razorpay",
        "kind": "connected_account",
        "connect": "api_key",
        "note": "Key ID and Key Secret from the Razorpay dashboard.",
    },
    {
        "id": "fast2sms",
        "label": "Fast2SMS",
        "kind": "connected_account",
        "connect": "api_key",
        "note": "API authorization key from Fast2SMS Dev API.",
    },
]

META_OAUTH_IDS = {"instagram", "facebook"}
CHANNEL_OAUTH_IDS = {"gmail", "outlook", "slack"}
API_KEY_IDS = {"razorpay", "fast2sms"}
OAUTH_IDS = META_OAUTH_IDS | CHANNEL_OAUTH_IDS


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def setup_page_url(raw_token: str, *, connect: str | None = None) -> str:
    origin = (get_api_settings().admin_frontend_origin or "http://localhost:3000").rstrip("/")
    url = f"{origin}/setup/{quote(raw_token, safe='')}"
    if connect:
        url = f"{url}?connect={quote(connect, safe='')}"
    return url


def public_api_origin() -> str:
    settings = get_api_settings()
    return f"http://localhost:{int(settings.port or 8030)}"


def widget_embed_snippet(business_id: str) -> str:
    origin = public_api_origin()
    return (
        f'<script src="{origin}/widget/widget.js" '
        f'data-business-id="{business_id}" '
        f'data-api-base="{origin}" '
        f'data-title="Chat"></script>'
    )


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def assemble_catalog(
    *,
    channels: list[dict[str, Any]],
    account_rows: list[dict[str, Any]],
    public: bool = False,
) -> list[dict[str, Any]]:
    channel_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in channels:
        channel_by_type.setdefault(str(row.get("channel") or ""), []).append(row)
    accounts_by_id = {str(row.get("integration_id")): row for row in account_rows}

    items: list[dict[str, Any]] = []
    for spec in CATALOG:
        item: dict[str, Any] = {
            "id": spec["id"],
            "label": spec["label"],
            "kind": spec["kind"],
            "connect": spec["connect"],
            "note": spec["note"],
            "connected": False,
            "status": "missing",
        }
        if spec["kind"] == "channel":
            matches = channel_by_type.get(spec["channel"], [])
            active = [m for m in matches if m.get("is_active")]
            errored = [m for m in matches if m.get("error_message")]
            item["connected"] = bool(active)
            if errored and not active:
                item["status"] = "error"
                item["error"] = errored[0].get("error_message")
            elif active:
                item["status"] = "connected"
                display = active[0].get("channel_display_name") or active[0].get(
                    "channel_account_id"
                )
                item["account"] = display
            else:
                item["status"] = "missing"
        else:
            row = accounts_by_id.get(spec["id"])
            if row:
                item["connected"] = True
                item["status"] = "connected"
                if not public:
                    item["connected_account_id"] = row.get("connected_account_id")
            else:
                item["status"] = "missing"
        items.append(item)
    return items


async def insert_setup_link(
    pool: Any,
    *,
    business_id: str,
    created_by: str,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    revoke_previous: bool = False,
) -> dict[str, Any]:
    raw = new_raw_token()
    digest = hash_token(raw)
    hours = max(1, min(int(ttl_hours), 30 * 24))
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    if revoke_previous:
        await pool.execute(
            """
            UPDATE integration_setup_links
            SET revoked_at = now()
            WHERE business_id = $1::uuid AND revoked_at IS NULL
            """,
            business_id,
        )
    row = await pool.fetchrow(
        """
        INSERT INTO integration_setup_links (business_id, created_by, token_hash, expires_at)
        VALUES ($1::uuid, $2::uuid, $3, $4)
        RETURNING id, expires_at, created_at
        """,
        business_id,
        created_by,
        digest,
        expires,
    )
    return {
        "id": str(row["id"]),
        "token": raw,
        "url": setup_page_url(raw),
        "expires_at": encode(row["expires_at"]),
        "created_at": encode(row["created_at"]),
    }


async def lookup_active_link(pool: Any, raw_token: str) -> dict[str, Any] | None:
    digest = hash_token((raw_token or "").strip())
    if not digest or len((raw_token or "").strip()) < 16:
        return None
    row = await pool.fetchrow(
        """
        SELECT l.id, l.business_id, l.expires_at, l.revoked_at, l.last_used_at,
               b.name AS business_name
        FROM integration_setup_links l
        JOIN businesses b ON b.id = l.business_id
        WHERE l.token_hash = $1
        """,
        digest,
    )
    if not row:
        return None
    if row["revoked_at"] is not None:
        return None
    expires = row["expires_at"]
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
    return {
        "id": str(row["id"]),
        "business_id": str(row["business_id"]),
        "business_name": row["business_name"],
        "expires_at": encode(row["expires_at"]),
    }


async def touch_setup_link(pool: Any, link_id: str) -> None:
    await pool.execute(
        "UPDATE integration_setup_links SET last_used_at = now() WHERE id = $1::uuid",
        link_id,
    )


async def load_catalog_payload(
    pool: Any,
    business_id: str,
    *,
    public: bool = False,
    expires_at: Any = None,
    business_name: str | None = None,
) -> dict[str, Any]:
    if not business_name:
        row = await pool.fetchrow(
            "SELECT name FROM businesses WHERE id = $1::uuid",
            business_id,
        )
        business_name = row["name"] if row else "Business"
    channels = await pool.fetch(
        """
        SELECT channel::text AS channel, channel_account_id, channel_display_name,
               is_active, error_message
        FROM business_channels
        WHERE business_id = $1::uuid
        """,
        business_id,
    )
    account_rows = await pool.fetch(
        """
        SELECT platform AS integration_id, id AS connected_account_id, updated_at
        FROM connected_accounts
        WHERE business_id = $1::uuid AND is_active = true
        """,
        business_id,
    )
    channel_dicts = [dict(r) for r in channels]
    account_dicts = [dict(r) for r in account_rows]
    payload: dict[str, Any] = {
        "business_id": business_id,
        "business_name": business_name,
        "widget_embed": widget_embed_snippet(business_id),
        "items": assemble_catalog(
            channels=channel_dicts, account_rows=account_dicts, public=public
        ),
    }
    if expires_at is not None:
        payload["expires_at"] = encode(expires_at)
    return payload


async def mint_oauth_url(
    business_id: str,
    integration_id: str,
    *,
    setup_token: str | None = None,
    mode: Literal["normal", "rerequest", "fresh"] = "normal",
) -> dict[str, Any]:
    integration_id = (integration_id or "").strip()
    if integration_id not in OAUTH_IDS:
        raise HTTPException(status_code=400, detail="Integration is not OAuth")
    if integration_id in META_OAUTH_IDS:
        from onboarding.integrations import (
            generate_facebook_oauth_url,
            generate_instagram_oauth_url,
        )

        try:
            if integration_id == "instagram":
                url = generate_instagram_oauth_url(
                    business_id, setup_token=setup_token, mode=mode
                )
            else:
                url = generate_facebook_oauth_url(
                    business_id, setup_token=setup_token, mode=mode
                )
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"integration_id": integration_id, "oauth_url": url, "mode": mode}

    from channel_framework.connection_hub import start_oauth
    from channel_framework.hub_tokens import issue_hub_token

    token = issue_hub_token(business_id)
    result = await start_oauth(
        business_id,
        integration_id,
        token,
        setup_token=setup_token,
    )
    return {
        "integration_id": integration_id,
        "oauth_url": result["url"],
    }
