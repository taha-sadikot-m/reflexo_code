"""Meta integration capabilities: scopes, webhook fields, and feature labels."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetaFeature:
    id: str
    label: str
    integration: str
    scopes: tuple[str, ...]
    page_fields: tuple[str, ...]
    app_object: str | None
    app_fields: tuple[str, ...]


FEATURES: tuple[MetaFeature, ...] = (
    MetaFeature(
        id="instagram_dm",
        label="Instagram DMs",
        integration="instagram",
        scopes=("instagram_manage_messages",),
        page_fields=("messages",),
        app_object=None,
        app_fields=(),
    ),
    MetaFeature(
        id="instagram_comment",
        label="Instagram comment replies",
        integration="instagram",
        scopes=("instagram_manage_comments",),
        page_fields=(),
        app_object="instagram",
        app_fields=("comments",),
    ),
    MetaFeature(
        id="instagram_post_context",
        label="Instagram post context",
        integration="instagram",
        scopes=("instagram_basic", "instagram_manage_comments"),
        page_fields=(),
        app_object="instagram",
        app_fields=("comments",),
    ),
    MetaFeature(
        id="facebook_messenger",
        label="Facebook Messenger",
        integration="facebook",
        scopes=("pages_messaging",),
        page_fields=("messages",),
        app_object=None,
        app_fields=(),
    ),
    MetaFeature(
        id="facebook_comment",
        label="Facebook comment replies",
        integration="facebook",
        scopes=("pages_read_engagement", "pages_manage_engagement", "pages_manage_metadata"),
        page_fields=("feed",),
        app_object=None,
        app_fields=(),
    ),
    MetaFeature(
        id="facebook_post_context",
        label="Facebook post context",
        integration="facebook",
        scopes=("pages_read_engagement",),
        page_fields=("feed",),
        app_object=None,
        app_fields=(),
    ),
)

REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    "instagram_dm": ("instagram_manage_messages",),
    "instagram_comment": ("instagram_manage_comments",),
    "facebook_messenger": ("pages_messaging",),
    "facebook_comment": ("pages_manage_engagement",),
    "whatsapp": ("whatsapp_business_messaging",),
    "whatsapp_voice": ("whatsapp_business_messaging",),
}

_CHANNEL_TO_INTEGRATION = {
    "instagram_dm": "instagram",
    "facebook_messenger": "facebook",
}


def scopes_from_debug_token(info: dict[str, Any]) -> list[str]:
    scopes = list(info.get("scopes") or [])
    for granular in info.get("granular_scopes") or []:
        scope = granular.get("scope")
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


def derive_feature_status(
    *,
    integration: str,
    granted_scopes: Iterable[str],
    page_fields: Iterable[str],
    app_subscriptions: dict[str, Iterable[str]],
    app_permissions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return live vs blocked features without calling Graph."""
    granted = {str(s) for s in granted_scopes}
    page = {str(f) for s in page_fields for f in [s]}
    approval_checked = app_permissions is not None
    approval = app_permissions or {}
    items: list[dict[str, Any]] = []
    for feature in FEATURES:
        if feature.integration != integration:
            continue
        missing_scopes = [s for s in feature.scopes if s not in granted]
        unapproved_app_permissions = [
            scope
            for scope in feature.scopes
            if approval_checked and approval.get(scope) != "live"
        ]
        missing_page = [f for f in feature.page_fields if f not in page]
        missing_app: list[str] = []
        if feature.app_object:
            subscribed = {str(x) for x in (app_subscriptions.get(feature.app_object) or [])}
            missing_app = [f for f in feature.app_fields if f not in subscribed]
        blocked = bool(
            missing_scopes
            or unapproved_app_permissions
            or missing_page
            or missing_app
        )
        reason_parts: list[str] = []
        if missing_scopes:
            reason_parts.append("missing scopes: " + ", ".join(missing_scopes))
        if unapproved_app_permissions:
            reason_parts.append(
                "app permissions not approved: "
                + ", ".join(unapproved_app_permissions)
            )
        if missing_page:
            reason_parts.append("missing Page webhook fields: " + ", ".join(missing_page))
        if missing_app:
            reason_parts.append(
                f"missing app-level {feature.app_object} fields: " + ", ".join(missing_app)
            )
        items.append(
            {
                "id": feature.id,
                "label": feature.label,
                "live": not blocked,
                "blocked": blocked,
                "missing_scopes": missing_scopes,
                "missing_token_scopes": missing_scopes,
                "unapproved_app_permissions": unapproved_app_permissions,
                "missing_page_fields": missing_page,
                "missing_app_fields": missing_app,
                "reason": "; ".join(reason_parts) if blocked else "",
            }
        )
    return items


def _api_version() -> str:
    return (os.getenv("META_API_VERSION") or "v22.0").strip()


def _verify() -> bool:
    return (os.getenv("META_HTTP_VERIFY") or "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def webhook_callback_url() -> str:
    explicit = (
        os.getenv("META_WEBHOOK_URL") or os.getenv("META_WEBHOOK_CALLBACK_URL") or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from onboarding.integrations import _redirect_uri

        redirect = _redirect_uri()
    except Exception:
        redirect = ""
    if "/api/oauth/callback" in redirect:
        return redirect.replace("/api/oauth/callback", "/webhook")
    return redirect


async def debug_token(
    client: httpx.AsyncClient,
    token: str,
    app_id: str,
    app_secret: str,
) -> dict[str, Any]:
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/debug_token",
        params={"input_token": token},
        headers={"Authorization": f"Bearer {app_id}|{app_secret}"},
    )
    if response.status_code >= 400:
        raise RuntimeError(f"debug_token HTTP {response.status_code}")
    return (response.json() or {}).get("data") or {}


async def fetch_app_subscriptions(
    client: httpx.AsyncClient,
    app_id: str,
    app_secret: str,
) -> dict[str, list[str]]:
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/{app_id}/subscriptions",
        headers={"Authorization": f"Bearer {app_id}|{app_secret}"},
    )
    if response.status_code >= 400:
        logger.warning("app subscriptions HTTP %s: %s", response.status_code, response.text[:300])
        return {}
    out: dict[str, list[str]] = {}
    for row in (response.json() or {}).get("data") or []:
        obj = str(row.get("object") or "")
        fields: list[str] = []
        for field in row.get("fields") or []:
            if isinstance(field, dict):
                name = str(field.get("name") or "")
            else:
                name = str(field)
            if name and name not in fields:
                fields.append(name)
        if obj:
            out[obj] = fields
    return out


async def fetch_app_permission_status(
    client: httpx.AsyncClient,
    app_id: str,
    app_secret: str,
) -> dict[str, str]:
    """Return Meta App Review status without exposing the app token in URLs."""
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/{app_id}/permissions",
        headers={"Authorization": f"Bearer {app_id}|{app_secret}"},
    )
    if response.status_code >= 400:
        raise RuntimeError(f"app permissions HTTP {response.status_code}")
    return {
        str(row["permission"]): str(row.get("status") or "")
        for row in (response.json() or {}).get("data") or []
        if row.get("permission")
    }


async def fetch_page_subscribed_fields(
    client: httpx.AsyncClient,
    page_id: str,
    page_token: str,
) -> list[str]:
    response = await client.get(
        f"https://graph.facebook.com/{_api_version()}/{page_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {page_token}"},
    )
    if response.status_code >= 400:
        logger.warning(
            "subscribed_apps list HTTP %s page=%s",
            response.status_code,
            page_id,
        )
        return []
    fields: list[str] = []
    app_id = (os.getenv("META_APP_ID") or "").strip()
    for row in (response.json() or {}).get("data") or []:
        if app_id and str(row.get("id") or "") not in ("", app_id):
            continue
        for field in row.get("subscribed_fields") or []:
            name = str(field)
            if name and name not in fields:
                fields.append(name)
    return fields


async def resolve_page_for_channel(
    client: httpx.AsyncClient,
    channel: str,
    account_id: str,
    token: str,
) -> tuple[str | None, str | None]:
    if channel == "facebook_messenger":
        return account_id, token
    me = await client.get(
        f"https://graph.facebook.com/{_api_version()}/me",
        params={"fields": "id,name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if me.status_code == 200:
        me_id = str((me.json() or {}).get("id") or "")
        if me_id and me_id != account_id:
            return me_id, token
    accounts = await client.get(
        f"https://graph.facebook.com/{_api_version()}/me/accounts",
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


async def _fetch_channel_rows(business_id: str, channels: Sequence[str]) -> list[Any]:
    """Load Meta channel rows for a business with Fernet ready for decryption."""
    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required")

    from mcp_servers.meta_mcp.auth.tokens import init_fernet
    from shared.db import service_connection

    init_fernet(encryption_key)
    async with service_connection() as conn:
        return list(
            await conn.fetch(
                """
                SELECT channel::text AS channel, channel_account_id, channel_display_name,
                       access_token_encrypted, is_active, error_message
                FROM business_channels
                WHERE business_id = $1::uuid
                  AND channel = ANY($2::channel_type[])
                ORDER BY channel
                """,
                business_id,
                list(channels),
            )
        )


async def _load_channel_token(business_id: str, channel: str) -> tuple[str, str]:
    """Return (channel_account_id, decrypted access token) for one Meta channel."""
    from mcp_servers.meta_mcp.auth.tokens import decrypt_token

    for row in await _fetch_channel_rows(business_id, [channel]):
        if str(row["channel"]) != channel:
            continue
        if not row["is_active"]:
            raise RuntimeError(f"{channel} is connected but not active for this business")
        return str(row["channel_account_id"]), decrypt_token(row["access_token_encrypted"])
    raise RuntimeError(f"{channel} is not connected for this business")


async def inspect_business_capabilities(business_id: str) -> dict[str, Any]:
    """Live Graph inspection of Meta channels. Never returns token material."""
    app_id = (os.getenv("META_APP_ID") or "").strip()
    app_secret = (os.getenv("META_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("META_APP_ID and META_APP_SECRET are required")

    from mcp_servers.meta_mcp.auth.tokens import decrypt_token

    rows = await _fetch_channel_rows(business_id, ["instagram_dm", "facebook_messenger"])

    items: list[dict[str, Any]] = []
    app_subs: dict[str, list[str]] = {}
    app_permissions: dict[str, str] | None = None
    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as client:
        try:
            app_subs = await fetch_app_subscriptions(client, app_id, app_secret)
        except Exception as exc:
            logger.warning(
                "app subscriptions failed: %s: %s", type(exc).__name__, exc
            )
            app_subs = {}
        try:
            app_permissions = await fetch_app_permission_status(
                client, app_id, app_secret
            )
        except Exception as exc:
            logger.warning("app permissions failed: %s: %s", type(exc).__name__, exc)
            app_permissions = None

        for row in rows:
            channel = str(row["channel"])
            integration = _CHANNEL_TO_INTEGRATION.get(channel)
            account = {
                "channel": channel,
                "account": row["channel_display_name"] or row["channel_account_id"],
                "account_id": str(row["channel_account_id"]),
                "is_active": bool(row["is_active"]),
                "error": row["error_message"],
                "integration": integration,
                "granted_scopes": [],
                "token_valid": False,
                "page_id": None,
                "page_fields": [],
                "features": [],
            }
            if not integration:
                items.append(account)
                continue
            try:
                token = decrypt_token(row["access_token_encrypted"])
            except Exception as exc:
                account["error"] = f"token cannot be decrypted ({exc})"
                items.append(account)
                continue
            try:
                info = await debug_token(client, token, app_id, app_secret)
            except Exception as exc:
                account["error"] = (
                    f"debug_token failed ({type(exc).__name__}: {exc})"
                )
                items.append(account)
                continue
            account["token_valid"] = bool(info.get("is_valid"))
            account["granted_scopes"] = scopes_from_debug_token(info)
            page_id, page_token = await resolve_page_for_channel(
                client, channel, str(row["channel_account_id"]), token
            )
            account["page_id"] = page_id
            page_fields: list[str] = []
            if page_id and page_token:
                page_fields = await fetch_page_subscribed_fields(client, page_id, page_token)
            account["page_fields"] = page_fields
            account["features"] = derive_feature_status(
                integration=integration,
                granted_scopes=account["granted_scopes"],
                page_fields=page_fields,
                app_subscriptions=app_subs,
                app_permissions=app_permissions,
            )
            items.append(account)

    return {
        "business_id": business_id,
        "app_subscriptions": {k: list(v) for k, v in app_subs.items()},
        "app_permissions": dict(app_permissions or {}),
        "app_permissions_checked": app_permissions is not None,
        "instagram_comments_subscribed": "comments"
        in {str(x) for x in (app_subs.get("instagram") or [])},
        "instagram_messages_subscribed": "messages"
        in {str(x) for x in (app_subs.get("instagram") or [])},
        "items": items,
    }


def _graph_error(response: httpx.Response, label: str) -> RuntimeError:
    detail = response.text
    try:
        detail = (response.json().get("error") or {}).get("message") or detail
    except Exception:
        pass
    return RuntimeError(f"{label} HTTP {response.status_code}: {detail}")


async def subscribe_app_object_fields(
    object_name: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    """App-level webhook subscription for a Meta object (instagram, page, ...)."""
    requested = [str(f).strip() for f in fields if str(f).strip()]
    if not requested:
        raise RuntimeError("At least one webhook field is required")

    app_id = (os.getenv("META_APP_ID") or "").strip()
    app_secret = (os.getenv("META_APP_SECRET") or "").strip()
    verify_token = (os.getenv("META_VERIFY_TOKEN") or "").strip()
    callback = webhook_callback_url()
    if not app_id or not app_secret:
        raise RuntimeError("META_APP_ID and META_APP_SECRET are required")
    if not verify_token:
        raise RuntimeError("META_VERIFY_TOKEN is required")
    if not callback:
        raise RuntimeError("META_WEBHOOK_URL (or META_OAUTH_REDIRECT_URI) is required")

    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as client:
        response = await client.post(
            f"https://graph.facebook.com/{_api_version()}/{app_id}/subscriptions",
            data={
                "object": object_name,
                "callback_url": callback,
                "fields": ",".join(requested),
                "verify_token": verify_token,
                "access_token": f"{app_id}|{app_secret}",
            },
        )
    if response.status_code >= 400:
        raise _graph_error(response, "app subscriptions")
    data = response.json() if response.content else {}
    return {
        "ok": True,
        "object": object_name,
        "fields": requested,
        "callback_url": callback,
        "response": data if isinstance(data, dict) else {"data": data},
    }


async def subscribe_instagram_app_comments() -> dict[str, Any]:
    """One-time app-level Instagram comments webhook subscription."""
    return await subscribe_app_object_fields("instagram", ["comments"])


async def subscribe_page_webhook_fields(
    business_id: str,
    channel: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    """Subscribe this app to Page webhook fields (for example `feed` for comments)."""
    requested = [str(f).strip() for f in fields if str(f).strip()]
    if not requested:
        raise RuntimeError("At least one webhook field is required")

    account_id, token = await _load_channel_token(business_id, channel)

    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as client:
        page_id, page_token = await resolve_page_for_channel(
            client, channel, account_id, token
        )
        if not page_id or not page_token:
            raise RuntimeError(
                f"Could not resolve a Facebook Page for {channel}. "
                "Reconnect the account and grant pages_show_list."
            )
        response = await client.post(
            f"https://graph.facebook.com/{_api_version()}/{page_id}/subscribed_apps",
            data={"subscribed_fields": ",".join(requested)},
            headers={"Authorization": f"Bearer {page_token}"},
        )
        if response.status_code >= 400:
            raise _graph_error(response, "page subscribed_apps")
        subscribed = await fetch_page_subscribed_fields(client, page_id, page_token)

    return {
        "ok": True,
        "channel": channel,
        "page_id": page_id,
        "requested_fields": requested,
        "subscribed_fields": subscribed,
    }
