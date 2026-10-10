"""Admin mutations for setup links, catalog, seed, API keys, and disconnect."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin, SuperAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict
from admin.setup_links import (
    API_KEY_IDS,
    DEFAULT_TTL_HOURS,
    insert_setup_link,
    load_catalog_payload,
    mint_oauth_url,
)
from api.settings import get_api_settings

router = APIRouter(prefix="/api/admin/integrations", tags=["admin-integrations"])


class CreateLinkBody(BaseModel):
    ttl_hours: int = Field(default=DEFAULT_TTL_HOURS, ge=1, le=720)
    revoke_previous: bool = False


class ApiKeyBody(BaseModel):
    integration_id: str
    api_key: str = Field(min_length=4, max_length=512)
    api_secret: str | None = Field(default=None, max_length=512)


class OauthBody(BaseModel):
    integration_id: str
    mode: Literal["normal", "rerequest", "fresh"] = "normal"


class PageFieldsBody(BaseModel):
    channel: Literal["facebook_messenger"] = "facebook_messenger"
    fields: list[str] = Field(default=["feed", "messages"], min_length=1)


class InstagramFieldsBody(BaseModel):
    fields: list[str] = Field(default=["messages", "comments"], min_length=1)


@router.get("/{business_id}/catalog")
async def integration_catalog(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    return await load_catalog_payload(pool, business_id, public=False)


@router.get("/{business_id}/setup-links")
async def list_setup_links(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT id, created_by, expires_at, revoked_at, last_used_at, created_at,
               (revoked_at IS NULL AND expires_at > now()) AS active
        FROM integration_setup_links
        WHERE business_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT 50
        """,
        business_id,
    )
    origin = (get_api_settings().admin_frontend_origin or "http://localhost:3000").rstrip("/")
    items = []
    for row in rows:
        item = row_dict(row)
        item["page_path"] = f"{origin}/setup/"
        items.append(item)
    return {"items": items}


@router.post("/{business_id}/setup-links")
async def create_setup_link(
    business_id: str, body: CreateLinkBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    created = await insert_setup_link(
        pool,
        business_id=business_id,
        created_by=admin.id,
        ttl_hours=body.ttl_hours,
        revoke_previous=body.revoke_previous,
    )
    await audit(
        admin.id,
        "setup_link.create",
        business_id=business_id,
        entity_type="integration_setup_link",
        entity_id=created["id"],
        detail={"ttl_hours": body.ttl_hours, "revoke_previous": body.revoke_previous},
    )
    return created


@router.post("/{business_id}/setup-links/{link_id}/revoke")
async def revoke_setup_link(
    business_id: str, link_id: str, admin: AdminPlus
) -> dict[str, str]:
    await require_business(business_id)
    pool = await get_admin_pool()
    result = await pool.execute(
        """
        UPDATE integration_setup_links
        SET revoked_at = now()
        WHERE id = $1::uuid AND business_id = $2::uuid AND revoked_at IS NULL
        """,
        link_id,
        business_id,
    )
    if str(result).endswith("0"):
        raise HTTPException(status_code=404, detail="Setup link not found")
    await audit(
        admin.id,
        "setup_link.revoke",
        business_id=business_id,
        entity_type="integration_setup_link",
        entity_id=link_id,
    )
    return {"status": "revoked"}


@router.post("/{business_id}/seed-whatsapp")
async def seed_whatsapp(business_id: str, admin: AdminPlus) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    from onboarding.channel_seed import seed_whatsapp_channel_for_business

    async with pool.acquire() as conn:
        result = await seed_whatsapp_channel_for_business(conn, business_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Seed failed")
    await audit(
        admin.id,
        "channel.seed_whatsapp",
        business_id=business_id,
        entity_type="business_channels",
        detail={"phone_number_id": result.get("phone_number_id")},
    )
    return result


@router.post("/{business_id}/ensure-widget")
async def ensure_widget(business_id: str, admin: AdminPlus) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    await pool.execute(
        """
        INSERT INTO business_channels (business_id, channel, channel_account_id, channel_display_name, is_active)
        VALUES ($1::uuid, 'web_widget'::channel_type, $1::text, 'Web widget', true)
        ON CONFLICT (business_id, channel, channel_account_id) DO NOTHING
        """,
        business_id,
    )
    await audit(
        admin.id,
        "channel.ensure_widget",
        business_id=business_id,
        entity_type="business_channels",
    )
    from admin.setup_links import widget_embed_snippet

    return {"ok": True, "widget_embed": widget_embed_snippet(business_id)}


@router.post("/{business_id}/api-keys")
async def connect_api_keys(
    business_id: str, body: ApiKeyBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    integration_id = body.integration_id.strip()
    if integration_id not in API_KEY_IDS:
        raise HTTPException(status_code=400, detail="Not an API-key integration")
    secret = body.api_secret if integration_id == "razorpay" else None
    if integration_id == "razorpay" and not (body.api_secret or "").strip():
        raise HTTPException(status_code=400, detail="Razorpay requires api_secret")
    from channel_framework.connection_hub import ApiKeyRequest, connect_api_key
    from channel_framework.hub_tokens import issue_hub_token

    result = await connect_api_key(
        business_id,
        issue_hub_token(business_id),
        ApiKeyRequest(
            platform=integration_id,
            api_key=body.api_key.strip(),
            api_secret=secret,
        ),
    )
    await audit(
        admin.id,
        "channel.api_key_connect",
        business_id=business_id,
        entity_type="connected_accounts",
        entity_id=integration_id,
    )
    return result


@router.post("/{business_id}/accounts/{integration_id}/disconnect")
@router.post(
    "/{business_id}/nango/{integration_id}/disconnect",
    include_in_schema=False,
)
async def disconnect_account(
    business_id: str, integration_id: str, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    from channel_framework.adapters import get_adapter
    from channel_framework.credentials import delete_account

    if integration_id in {"gmail", "outlook", "slack"}:
        try:
            await get_adapter(integration_id).teardown_notifications(business_id)
        except Exception:
            pass
    disconnected = await delete_account(business_id, integration_id)
    result = {"disconnected": disconnected, "integration_id": integration_id}
    await audit(
        admin.id,
        "channel.disconnect",
        business_id=business_id,
        entity_type="connected_accounts",
        entity_id=integration_id,
    )
    return result


@router.post("/{business_id}/oauth-url")
async def admin_oauth_url(
    business_id: str, body: OauthBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    minted = await mint_oauth_url(
        business_id, body.integration_id, mode=body.mode
    )
    await audit(
        admin.id,
        "oauth.mint",
        business_id=business_id,
        entity_type="oauth",
        entity_id=body.integration_id,
    )
    return minted


@router.get("/{business_id}/capabilities")
async def integration_capabilities(
    business_id: str, admin: CurrentAdmin
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from integrations.capabilities import inspect_business_capabilities

    try:
        return await inspect_business_capabilities(business_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{business_id}/subscribe-instagram-comments")
async def subscribe_instagram_comments(
    business_id: str, admin: SuperAdmin
) -> dict[str, Any]:
    await require_business(business_id)
    from integrations.capabilities import subscribe_instagram_app_comments

    try:
        result = await subscribe_instagram_app_comments()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit(
        admin.id,
        "integrations.subscribe_instagram_comments",
        business_id=business_id,
        entity_type="meta_app_subscription",
        entity_id="instagram.comments",
    )
    return result


@router.post("/{business_id}/subscribe-page-fields")
async def subscribe_page_fields(
    business_id: str, body: PageFieldsBody, admin: SuperAdmin
) -> dict[str, Any]:
    await require_business(business_id)
    from integrations.capabilities import subscribe_page_webhook_fields

    try:
        result = await subscribe_page_webhook_fields(
            business_id, body.channel, body.fields
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit(
        admin.id,
        "integrations.subscribe_page_fields",
        business_id=business_id,
        entity_type="meta_page_subscription",
        entity_id=f"{body.channel}:{','.join(body.fields)}",
    )
    return result


@router.post("/{business_id}/subscribe-instagram-webhooks")
async def subscribe_instagram_webhooks(
    business_id: str, body: InstagramFieldsBody, admin: SuperAdmin
) -> dict[str, Any]:
    await require_business(business_id)
    from integrations.capabilities import subscribe_app_object_fields

    try:
        result = await subscribe_app_object_fields("instagram", body.fields)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit(
        admin.id,
        "integrations.subscribe_instagram_webhooks",
        business_id=business_id,
        entity_type="meta_app_subscription",
        entity_id=f"instagram.{','.join(body.fields)}",
    )
    return result
