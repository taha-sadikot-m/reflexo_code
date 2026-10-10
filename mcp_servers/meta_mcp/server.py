"""REFLEXO Meta MCP server — FastMCP streamable-http on port 8021 + webhook routes."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from shared.logging_filters import install_httpx_credential_filter

install_httpx_credential_filter()

from starlette.requests import Request
from starlette.responses import Response

from fastmcp import FastMCP

from mcp_servers.meta_mcp.auth.signature import verify_signature
from mcp_servers.meta_mcp.auth.tokens import init_fernet
from mcp_servers.meta_mcp.resources import register as register_resources
from mcp_servers.meta_mcp.settings import get_settings
from mcp_servers.meta_mcp.tools import register_all
from mcp_servers.meta_mcp.webhooks.handler import process_webhook_payload
from shared.db import close_pool, init_pool
from shared.redis_client import close_redis, init_redis

logger = logging.getLogger("meta-mcp")


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    await init_pool(settings.database_url, application_name="meta-mcp")
    await init_redis(settings.redis_url)
    init_fernet(settings.encryption_key)
    logger.info("meta-mcp ready (db + redis + fernet) on port %s", settings.port)
    try:
        yield
    finally:
        await close_pool()
        await close_redis()
        logger.info("meta-mcp shut down")


mcp = FastMCP(
    "meta-mcp",
    lifespan=lifespan,
    instructions="""
    REFLEXO Meta MCP -- the channel delivery layer.

    RULES:
    - business_id is ALWAYS the first parameter on every tool.
    - Before sending any message, the agent MUST have resolved the business_id
      and verified the channel is connected via get_business_config on Core MCP.
    - For WhatsApp business-initiated conversations (outside the 24-hour window),
      you MUST use send_whatsapp_template. Regular text messages will fail.
    - Never send more than 3 messages in rapid succession to the same recipient
      without a pause or user response.
    - mark_as_read should be called on every incoming message before responding.
    - For Instagram comments, always reply contextually -- read the post info first.
    """,
)

register_all(mcp)
register_resources(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    # Connection: close — ngrok on Windows often delivers headers but stalls the
    # body when keep-alive is used; Meta webhook verify needs the full body.
    return Response(
        content=b'{"status":"healthy","server":"meta-mcp"}',
        status_code=200,
        media_type="application/json",
        headers={"Connection": "close"},
    )


@mcp.custom_route("/webhook", methods=["GET", "POST"])
async def webhook(request: Request) -> Response:
    settings = get_settings()
    close = {"Connection": "close"}

    if request.method == "GET":
        params = request.query_params
        mode = params.get("hub.mode") or params.get("hub_mode")
        token = params.get("hub.verify_token") or params.get("hub_verify_token")
        challenge = params.get("hub.challenge") or params.get("hub_challenge")

        if mode == "subscribe" and token == settings.meta_verify_token and challenge:
            logger.info("Webhook verification challenge accepted")
            # Raw text/plain body only — Meta compares this to hub.challenge.
            return Response(
                content=str(challenge).encode("utf-8"),
                status_code=200,
                media_type="text/plain",
                headers=close,
            )

        logger.warning("Webhook verification failed")
        return Response(
            content=b"Forbidden",
            status_code=403,
            media_type="text/plain",
            headers=close,
        )

    # POST
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(body, signature, settings.meta_app_secret):
        logger.warning("Invalid webhook signature")
        return Response(
            content=b"Invalid signature",
            status_code=401,
            media_type="text/plain",
            headers=close,
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(
            content=b"Invalid JSON",
            status_code=400,
            media_type="text/plain",
            headers=close,
        )

    entries = payload.get("entry") or []
    fields = [
        str(change.get("field") or "")
        for entry in entries
        for change in (entry.get("changes") or [])
    ]
    logger.info(
        "Webhook POST object=%s entries=%s fields=%s messaging=%s",
        payload.get("object"),
        [entry.get("id") for entry in entries],
        [field for field in fields if field],
        any(bool(entry.get("messaging")) for entry in entries),
    )
    asyncio.create_task(_safe_process(payload))
    # Meta docs commonly expect EVENT_RECEIVED; 200 + plain body is enough.
    return Response(
        content=b"EVENT_RECEIVED",
        status_code=200,
        media_type="text/plain",
        headers=close,
    )


def _json(payload: dict, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(payload).encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
        headers={"Connection": "close"},
    )


@mcp.custom_route("/internal/voice/outbound-call", methods=["POST"])
async def internal_outbound_call(request: Request) -> Response:
    """Place a business-initiated WhatsApp call from this process.

    The SDP offer and its peer connection must live wherever the `calls`
    webhook lands, so out-of-process callers (CLI, workflows) come through here.
    """
    settings = get_settings()
    expected = settings.voice_internal_token.strip()
    if not expected:
        logger.warning("Internal outbound-call route disabled — no VOICE_INTERNAL_TOKEN")
        return _json({"error": "internal routes are disabled"}, 404)

    supplied = request.headers.get("Authorization", "")
    prefix = "Bearer "
    supplied = supplied[len(prefix):] if supplied.startswith(prefix) else supplied
    if not hmac.compare_digest(supplied.strip(), expected):
        logger.warning("Internal outbound-call rejected — bad token")
        return _json({"error": "unauthorized"}, 401)

    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _json({"error": "invalid JSON"}, 400)
    if not isinstance(body, dict):
        return _json({"error": "invalid JSON"}, 400)

    business_id = str(body.get("business_id") or "").strip()
    to_phone = str(body.get("to") or "").strip()
    if not business_id or not to_phone:
        return _json({"error": "business_id and to are required"}, 400)

    from voice.outbound import place_outbound_call

    try:
        call_id = await place_outbound_call(
            business_id,
            to_phone,
            reason=str(body.get("reason") or ""),
            customer_name=str(body.get("customer_name") or ""),
        )
    except Exception as exc:
        logger.exception("Internal outbound call failed")
        return _json({"error": str(exc), "type": type(exc).__name__}, 502)
    return _json({"call_id": call_id, "status": "ringing"})


def _oauth_page(heading: str, message: str, status_code: int = 200) -> Response:
    """Browser-facing result page for the OAuth round-trip."""
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reflexo</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #0f1115; color: #e8eaed; display: flex;
         align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #181b22; border: 1px solid #2a2f3a; border-radius: 14px;
           padding: 32px 36px; max-width: 460px; }}
  h1 {{ font-size: 20px; margin: 0 0 12px; }}
  p {{ font-size: 15px; line-height: 1.55; color: #b6bcc8; margin: 0; }}
</style></head>
<body><div class="card"><h1>{heading}</h1><p>{message}</p></div></body></html>"""
    return Response(
        content=html.encode("utf-8"),
        status_code=status_code,
        media_type="text/html",
        headers={"Connection": "close"},
    )


@mcp.custom_route("/api/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> Response:
    from onboarding.integrations import complete_meta_oauth

    params = request.query_params

    error = params.get("error") or params.get("error_reason")
    if error:
        detail = params.get("error_description") or error
        logger.info("Meta OAuth cancelled or denied: %s", detail)
        return _oauth_page(
            "Connection cancelled",
            "Nothing was changed. Message your assistant on WhatsApp again "
            "if you want a fresh link.",
        )

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _oauth_page(
            "Invalid link",
            "This callback is missing its authorization code. Request a new "
            "connection link from your assistant.",
            status_code=400,
        )

    try:
        result = await complete_meta_oauth(code, state)
    except ValueError as exc:
        # Tampered or stale signed state — never proceed to a token exchange.
        logger.warning("Rejected Meta OAuth callback: %s", exc)
        return _oauth_page(
            "Link could not be verified",
            "This link is invalid or has already been used. Request a new one "
            "from your assistant.",
            status_code=400,
        )
    except Exception:
        logger.exception("Meta OAuth callback failed")
        return _oauth_page(
            "Something went wrong",
            "We could not finish connecting your account. Please try the link "
            "again, or ask your assistant for a new one.",
            status_code=500,
        )

    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "no_instagram_account":
            return _oauth_page(
                "No Instagram account found",
                "No Instagram professional account is linked to a Facebook Page "
                "you manage. In the Instagram app, switch to a Business or Creator "
                "account and link it to your Page, then try again.",
                status_code=400,
            )
        if reason == "no_facebook_page":
            return _oauth_page(
                "No Facebook Page found",
                "This account does not manage any Facebook Page. Create or get "
                "admin access to a Page, then try again.",
                status_code=400,
            )
        logger.warning("Meta OAuth callback rejected: %s", reason)
        return _oauth_page(
            "Could not connect",
            "We could not finish connecting your account. Ask your assistant "
            "for a new link.",
            status_code=400,
        )

    names = ", ".join(result.get("accounts") or []) or "your account"
    channel = "Instagram" if result["integration"] == "instagram" else "Facebook Messenger"
    message = f"{channel} is now connected for {names}. You can close this tab and go back to WhatsApp."
    if result.get("subscription_warnings"):
        message += (
            " One thing to check: we could not subscribe to message webhooks "
            "automatically, so incoming messages may not arrive until that is "
            "enabled in your Meta app settings."
        )
    return _oauth_page(f"{channel} connected", message)


async def _safe_process(payload: dict) -> None:
    try:
        await process_webhook_payload(payload)
    except Exception:
        logger.exception("Webhook processing failed")


def main() -> None:
    settings = get_settings()
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        # Avoid keep-alive stalls through ngrok (headers OK, body never arrives).
        uvicorn_config={"timeout_keep_alive": 1},
    )


if __name__ == "__main__":
    main()
