"""GET /api/oauth/callback — Meta Instagram / Facebook Login return.

The exchange itself lives in onboarding.integrations.complete_meta_oauth, which
Meta MCP also serves on its own port. This module is only the redirect-style
presentation layer for the hosted API server.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from api.settings import get_api_settings
from onboarding.integrations import (
    complete_meta_oauth,
    decode_oauth_state,
    notify_owner_text,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/oauth/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Handle Meta OAuth callback for Instagram / Facebook connections."""
    settings = get_api_settings()
    setup_token = ""
    if state:
        try:
            setup_token = str(decode_oauth_state(state).get("setup_token") or "").strip()
        except Exception:
            setup_token = ""

    origin = (settings.admin_frontend_origin or "http://localhost:3000").rstrip("/")
    setup_base = f"{origin}/setup/{setup_token}" if setup_token else ""
    error_base = setup_base or settings.meta_oauth_error_url
    success_base = setup_base or settings.meta_oauth_success_url

    def _redirect(base: str, params: dict[str, str]) -> RedirectResponse:
        sep = "&" if "?" in base else "?"
        return RedirectResponse(f"{base}{sep}{urlencode(params)}", status_code=302)

    def _err_redirect(reason: str) -> RedirectResponse:
        params: dict[str, str] = {"error": reason}
        if setup_token:
            params["connected"] = "failed"
        return _redirect(error_base, params)

    if error:
        return _err_redirect(error_description or error)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    try:
        result = await complete_meta_oauth(code, state)
    except ValueError as exc:
        return _err_redirect(str(exc))
    except Exception as exc:
        logger.exception("Meta OAuth callback failed")
        return _err_redirect(f"connect_failed:{exc}")

    if not result.get("ok"):
        return _err_redirect(str(result.get("reason") or "connect_failed"))

    if result.get("subscription_warnings"):
        await notify_owner_text(
            result["business_id"],
            "Note: message webhooks subscribe nahi ho paye, incoming messages "
            "aane mein dikkat ho sakti hai. Meta app settings check karo.",
        )

    success_params: dict[str, str] = {
        "integration": result["integration"],
        "business_id": result["business_id"],
    }
    if setup_token:
        success_params["connected"] = result["integration"]
    return _redirect(success_base, success_params)
