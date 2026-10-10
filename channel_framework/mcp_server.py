"""Channel Framework MCP server on port 8022."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from channel_framework.credentials import get_valid_tokens
from channel_framework.senders.email import send_email as send_email_impl
from channel_framework.integrations.fast2sms import send_sms as send_sms_impl
from channel_framework.integrations.google_calendar import (
    check_availability as check_calendar_availability_impl,
    create_event as create_calendar_event_impl,
    list_events as list_calendar_events_impl,
)
from channel_framework.integrations.razorpay import (
    check_payment_status as check_payment_status_impl,
    create_order as create_order_impl,
    create_payment_link as create_payment_link_impl,
    initiate_refund as initiate_refund_impl,
)
from shared.db import close_pool, init_pool, tenant_connection
from shared.redis_client import close_redis, init_redis

load_dotenv()


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    await init_pool(os.environ["DATABASE_URL"], application_name="channel-mcp")
    await init_redis(os.getenv("REDIS_URL", "redis://localhost:6379"))
    try:
        yield
    finally:
        await close_pool()
        await close_redis()


mcp = FastMCP(
    "channel-framework",
    lifespan=lifespan,
    instructions="Non-Meta channel connections and message delivery.",
)


async def _resolve_mail_platform(business_id: str, platform: str | None) -> str:
    if platform:
        return platform
    async with tenant_connection(business_id) as conn:
        value = await conn.fetchval(
            """
            SELECT platform
            FROM connected_accounts
            WHERE business_id = $1::uuid
              AND platform IN ('gmail', 'outlook')
              AND is_active = true
            ORDER BY CASE platform WHEN 'gmail' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            business_id,
        )
    if not value:
        raise RuntimeError("No email account is connected")
    return str(value)


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def send_email(
    business_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    platform = await _resolve_mail_platform(business_id, platform)
    return await send_email_impl(
        business_id=business_id,
        platform=platform,
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
    )


async def _read_emails_impl(
    business_id: str,
    limit: int = 10,
    platform: str | None = None,
) -> dict[str, Any]:
    platform = await _resolve_mail_platform(business_id, platform)
    credentials = await get_valid_tokens(business_id, platform)
    if not credentials:
        raise RuntimeError(f"{platform} is not connected")
    async with httpx.AsyncClient(timeout=30.0) as client:
        if platform == "gmail":
            response = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
                params={"maxResults": max(1, min(limit, 100))},
            )
        else:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me/messages",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
                params={
                    "$top": max(1, min(limit, 100)),
                    "$select": "id,subject,from,receivedDateTime,bodyPreview",
                    "$orderby": "receivedDateTime desc",
                },
            )
        response.raise_for_status()
        data = response.json()
        return {"messages": data.get("messages") or data.get("value") or []}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def read_emails(
    business_id: str,
    limit: int = 10,
    platform: str | None = None,
) -> dict[str, Any]:
    return await _read_emails_impl(business_id, limit, platform)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_emails(
    business_id: str,
    limit: int = 10,
    platform: str | None = None,
) -> dict[str, Any]:
    return await _read_emails_impl(business_id, limit, platform)


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def create_payment_link(
    business_id: str,
    amount: float,
    description: str,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    customer_email: str | None = None,
    expire_hours: int | None = None,
) -> dict[str, Any]:
    del expire_hours
    customer = {
        key: value
        for key, value in {
            "name": customer_name,
            "contact": customer_phone,
            "email": customer_email,
        }.items()
        if value
    }
    result = await create_payment_link_impl(
        business_id,
        amount_paise=int(round(amount * 100)),
        description=description,
        customer=customer,
    )
    return {
        "payment_link_id": result.get("id"),
        "short_url": result.get("short_url"),
        "amount_rupees": amount,
        "status": result.get("status"),
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def check_payment_status(business_id: str, payment_id: str) -> dict[str, Any]:
    return await check_payment_status_impl(business_id, payment_id)


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def create_order(
    business_id: str,
    amount: float,
    receipt: str | None = None,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del notes
    return await create_order_impl(
        business_id,
        amount_paise=int(round(amount * 100)),
        receipt=receipt or f"reflexo-{secrets.token_hex(6)}",
    )


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def initiate_refund(
    business_id: str,
    payment_id: str,
    amount: float | None = None,
    speed: str = "normal",
    receipt: str | None = None,
) -> dict[str, Any]:
    del speed, receipt
    return await initiate_refund_impl(
        business_id,
        payment_id,
        amount_paise=int(round(amount * 100)) if amount is not None else None,
    )


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def send_sms(
    business_id: str,
    phone_number: str,
    message: str,
    route: str = "q",
) -> dict[str, Any]:
    result = await send_sms_impl(
        business_id, phone=phone_number, message=message, route=route
    )
    return {"sent": result.get("return", False), **result}


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def send_otp(
    business_id: str,
    phone_number: str,
    provider: str = "fast2sms",
    template_id: str | None = None,
    otp_length: int = 6,
    otp_expiry: int = 5,
) -> dict[str, Any]:
    del provider, template_id
    from shared.redis_client import get_redis

    otp = "".join(secrets.choice("0123456789") for _ in range(max(4, min(otp_length, 8))))
    request_id = secrets.token_urlsafe(16)
    await get_redis().setex(
        f"channel:otp:{business_id}:{request_id}",
        max(60, otp_expiry * 60),
        otp,
    )
    result = await send_sms_impl(
        business_id,
        phone=phone_number,
        message=f"Your verification code is {otp}",
        route="q",
    )
    return {"sent": result.get("return", False), "request_id": request_id, "provider": "fast2sms"}


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def verify_otp(
    business_id: str,
    phone_number: str,
    otp: str,
    provider: str = "fast2sms",
    request_id: str | None = None,
) -> dict[str, Any]:
    del phone_number, provider
    if not request_id:
        return {"verified": False, "error": "request_id is required"}
    from shared.redis_client import get_redis

    key = f"channel:otp:{business_id}:{request_id}"
    expected = await get_redis().get(key)
    verified = bool(expected and secrets.compare_digest(str(expected), otp))
    if verified:
        await get_redis().delete(key)
    return {"verified": verified}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_calendar_events(
    business_id: str,
    time_min: str,
    time_max: str,
    calendar_id: str | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    del calendar_id
    result = await list_calendar_events_impl(
        business_id, time_min, time_max, max_results
    )
    return {"events": result.get("items") or [], "next_page_token": result.get("nextPageToken")}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def check_availability(
    business_id: str,
    date: str,
    duration_minutes: int = 30,
    business_hours_start: str = "09:00",
    business_hours_end: str = "18:00",
    calendar_id: str | None = None,
) -> dict[str, Any]:
    del calendar_id
    return await check_calendar_availability_impl(
        business_id,
        date,
        duration_minutes,
        business_hours_start,
        business_hours_end,
    )


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def create_calendar_event(
    business_id: str,
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str = "Asia/Kolkata",
    calendar_id: str | None = None,
) -> dict[str, Any]:
    del location, timezone, calendar_id
    result = await create_calendar_event_impl(
        business_id,
        summary,
        start,
        end,
        attendee_email=(attendees or [None])[0],
        description=description,
    )
    return {"event_id": result.get("id"), "link": result.get("htmlLink"), **result}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def generate_connection_link(business_id: str) -> dict[str, str]:
    from channel_framework.connection_hub import connection_url

    return {"url": connection_url(business_id)}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_channel_status(business_id: str) -> dict[str, Any]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT platform, account_identifier, account_display_name,
                   capabilities, is_active, connection_error, token_expires_at
            FROM connected_accounts
            WHERE business_id = $1::uuid
            ORDER BY platform
            """,
            business_id,
        )
    return {"accounts": [dict(row) for row in rows]}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_connected_integrations(business_id: str) -> dict[str, Any]:
    return await get_channel_status(business_id)


def _normalize_oauth_integration_id(integration_id: str | None) -> str:
    normalized = (integration_id or "").strip().casefold()
    if normalized in {"calendar", "google-calendar", "google_calendar"}:
        return "gmail"
    return normalized


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_oauth_connect_url_tool(
    business_id: str,
    integration_id: str | None = None,
    redirect_url: str | None = None,
) -> dict[str, str]:
    del redirect_url
    normalized_integration = _normalize_oauth_integration_id(integration_id)
    result = await generate_connection_link(business_id)
    suffix = f"&connect={normalized_integration}" if normalized_integration else ""
    return {"oauth_url": result["url"] + suffix}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_integration_connect_instructions(
    business_id: str,
    integration_id: str,
) -> dict[str, str]:
    result = await generate_connection_link(business_id)
    return {
        "integration_id": integration_id,
        "connection_url": result["url"] + f"&connect={integration_id}",
        "instructions": "Open the secure link and complete the connection.",
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def connect_razorpay(business_id: str) -> dict[str, str]:
    return await get_integration_connect_instructions(business_id, "razorpay")


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def connect_fast2sms(business_id: str) -> dict[str, str]:
    return await get_integration_connect_instructions(business_id, "fast2sms")


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "server": "channel-framework"})


def main() -> None:
    mcp.run(
        transport="streamable-http",
        host=os.getenv("CHANNEL_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("CHANNEL_MCP_PORT", "8022")),
    )


if __name__ == "__main__":
    main()
