"""httpx client helpers for Meta Graph API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from mcp_servers.meta_mcp.settings import get_settings
from mcp_servers.meta_mcp.utils.errors import MessageFailedError, MetaAuthError, RateLimitedError

logger = logging.getLogger(__name__)

# Meta OAuth failure codes: invalid/expired token, session invalidated,
# password changed, user deauthorized the app. 131005 is WhatsApp's "Access
# denied", which in practice means the token lost its WABA permission and the
# channel needs reconnecting, not that this one message was unsendable.
AUTH_ERROR_CODES = (190, 102, 463, 467, 131005)

_verify_warning_logged = False


def graph_url(path: str) -> str:
    settings = get_settings()
    path = path.lstrip("/")
    return f"https://graph.facebook.com/{settings.meta_api_version}/{path}"


def _ssl_verify() -> bool:
    global _verify_warning_logged
    verify = bool(get_settings().meta_http_verify)
    if not verify and not _verify_warning_logged:
        logger.warning(
            "META_HTTP_VERIFY=false — Graph API TLS verification disabled "
            "(dev only; prefer trusting your corp/AV CA in production)"
        )
        _verify_warning_logged = True
    return verify


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _raise_for_meta_error(response: httpx.Response) -> None:
    try:
        data = response.json()
    except Exception:
        data = {}

    err = data.get("error") if isinstance(data, dict) else None
    logger.error(
        "Meta Graph error HTTP %s body=%s",
        response.status_code,
        (response.text or "")[:1000],
    )
    if response.status_code == 429:
        raise RateLimitedError(
            endpoint="meta_graph",
            limit=0,
            window_seconds=60,
            retry_after_seconds=int(response.headers.get("Retry-After", "60")),
        )

    if err:
        message = err.get("message", response.text)
        code = err.get("code")
        if code in AUTH_ERROR_CODES or response.status_code == 401:
            raise MetaAuthError(f"code {code}: {message}")
        raise MessageFailedError(
            f"Meta API error ({code}): {message}",
            graph_code=code,
            http_status=response.status_code,
            retryable=response.status_code >= 500,
        )

    if response.status_code == 401:
        raise MetaAuthError(f"HTTP 401: {response.text[:500]}")

    if response.status_code >= 400:
        raise MessageFailedError(
            f"HTTP {response.status_code}: {response.text[:500]}",
            http_status=response.status_code,
            retryable=response.status_code >= 500,
        )


async def _with_auth_retry(
    attempt: Callable[[str], Awaitable[dict[str, Any]]],
    access_token: str,
    business_id: str | None,
    channel: str | None,
) -> dict[str, Any]:
    """Run attempt(token); on a Meta auth error, refetch the token once and retry.

    A 401 usually means the 5-minute Redis creds cache is serving a token the
    owner has already rotated. Dropping the cache and retrying repairs that
    without downtime. Only a second failure — on a token read straight from the
    database — is treated as a genuinely dead credential worth deactivating.
    """
    try:
        return await attempt(access_token)
    except MetaAuthError as first:
        if not business_id or not channel:
            raise

        from mcp_servers.meta_mcp.auth.tokens import (
            ensure_valid_token,
            invalidate_channel_token,
            invalidate_creds_cache,
        )

        await invalidate_creds_cache(business_id, channel)
        try:
            fresh = (await ensure_valid_token(business_id, channel)).get("access_token") or ""
        except Exception as exc:
            logger.warning(
                "Auth retry could not reload %s token for business=%s: %s",
                channel,
                business_id,
                exc,
            )
            fresh = ""

        if not fresh or fresh == access_token:
            await invalidate_channel_token(business_id, channel, str(first))
            raise

        logger.info(
            "Meta auth error on %s for business=%s — retrying with refreshed token",
            channel,
            business_id,
        )
        try:
            return await attempt(fresh)
        except MetaAuthError as second:
            await invalidate_channel_token(business_id, channel, str(second))
            raise


async def graph_post(
    path: str,
    access_token: str,
    json_body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    business_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    async def attempt(token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout, verify=_ssl_verify()) as client:
            response = await client.post(
                graph_url(path),
                headers=_auth_headers(token),
                json=json_body or {},
            )
        if response.status_code >= 400:
            _raise_for_meta_error(response)
        data = response.json()
        if isinstance(data, dict) and "error" in data:
            _raise_for_meta_error(response)
        return data if isinstance(data, dict) else {"data": data}

    return await _with_auth_retry(attempt, access_token, business_id, channel)


async def graph_get(
    path: str,
    access_token: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    business_id: str | None = None,
    channel: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    async def attempt(token: str) -> dict[str, Any]:
        query = dict(params or {})
        # Preserve an explicitly supplied token (e.g. app token for debug_token),
        # but let the auth retry swap in the refreshed one.
        if query.get("access_token") in (None, access_token):
            query["access_token"] = token
        if client is not None:
            response = await client.get(graph_url(path), params=query, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout, verify=_ssl_verify()) as http:
                response = await http.get(graph_url(path), params=query)
        if response.status_code >= 400:
            _raise_for_meta_error(response)
        data = response.json()
        if isinstance(data, dict) and "error" in data:
            _raise_for_meta_error(response)
        return data if isinstance(data, dict) else {"data": data}

    return await _with_auth_retry(attempt, access_token, business_id, channel)


async def graph_post_multipart(
    path: str,
    access_token: str,
    *,
    files: dict[str, Any],
    data: dict[str, str],
    timeout: float = 60.0,
    business_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    async def attempt(token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout, verify=_ssl_verify()) as client:
            response = await client.post(
                graph_url(path),
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                data=data,
            )
        if response.status_code >= 400:
            _raise_for_meta_error(response)
        result = response.json()
        if isinstance(result, dict) and "error" in result:
            _raise_for_meta_error(response)
        return result if isinstance(result, dict) else {"data": result}

    return await _with_auth_retry(attempt, access_token, business_id, channel)
