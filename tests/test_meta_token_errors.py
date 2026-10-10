"""Meta auth failures must read as TOKEN_EXPIRED, and survive a stale cache.

A 401 from Graph used to surface as MESSAGE_FAILED, whose guidance blames the
24h window or a blocked recipient. Nothing dropped the 5-minute Redis creds
cache, so a rotated token kept failing until the entry aged out on its own.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_servers.meta_mcp.utils import graph_client
from mcp_servers.meta_mcp.utils.errors import MessageFailedError, MetaAuthError

BUSINESS = "918750d7-2254-4cfd-bd02-c22c3ab708ec"

AUTH_BODY = {
    "error": {
        "message": "Authentication Error",
        "code": 190,
        "type": "OAuthException",
        "fbtrace_id": "AmsW8808CsyvkOv1c67i_CK",
    }
}


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body,
        request=httpx.Request("POST", "https://graph.facebook.com/v22.0/123/messages"),
    )


class _Recorder:
    """Stands in for the auth module the retry path imports lazily."""

    def __init__(self, fresh_token: str | None) -> None:
        self.fresh_token = fresh_token
        self.cache_drops: list[tuple[str, str]] = []
        self.invalidations: list[tuple[str, str, str]] = []
        self.reloads = 0

    async def invalidate_creds_cache(self, business_id: str, channel: str) -> None:
        self.cache_drops.append((business_id, channel))

    async def invalidate_channel_token(self, business_id: str, channel: str, reason: str) -> None:
        self.invalidations.append((business_id, channel, reason))

    async def ensure_valid_token(self, business_id: str, channel: str) -> dict:
        self.reloads += 1
        if self.fresh_token is None:
            raise RuntimeError("channel not connected")
        return {"access_token": self.fresh_token}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Install a fake mcp_servers.meta_mcp.auth.tokens for the lazy import."""
    import sys
    import types

    def _install(fresh_token: str | None) -> _Recorder:
        rec = _Recorder(fresh_token)
        module = types.ModuleType("mcp_servers.meta_mcp.auth.tokens")
        module.invalidate_creds_cache = rec.invalidate_creds_cache
        module.invalidate_channel_token = rec.invalidate_channel_token
        module.ensure_valid_token = rec.ensure_valid_token
        monkeypatch.setitem(sys.modules, "mcp_servers.meta_mcp.auth.tokens", module)
        return rec

    return _install


def test_code_190_raises_auth_error_not_message_failed() -> None:
    with pytest.raises(MetaAuthError) as exc_info:
        graph_client._raise_for_meta_error(_response(401, AUTH_BODY))
    assert exc_info.value.code == "TOKEN_EXPIRED"


def test_bare_http_401_raises_auth_error() -> None:
    with pytest.raises(MetaAuthError):
        graph_client._raise_for_meta_error(_response(401, {}))


@pytest.mark.parametrize("code", graph_client.AUTH_ERROR_CODES)
def test_all_oauth_codes_map_to_token_expired(code: int) -> None:
    body = {"error": {"message": "bad token", "code": code}}
    with pytest.raises(MetaAuthError):
        graph_client._raise_for_meta_error(_response(400, body))


def test_non_auth_error_still_message_failed() -> None:
    body = {"error": {"message": "outside 24h window", "code": 131047}}
    with pytest.raises(MessageFailedError) as exc_info:
        graph_client._raise_for_meta_error(_response(400, body))
    assert exc_info.value.code == "MESSAGE_FAILED"


def test_auth_error_dict_tells_agent_to_reconnect() -> None:
    payload = MetaAuthError("code 190: Authentication Error").to_dict()
    assert payload["error"]["code"] == "TOKEN_EXPIRED"
    assert "re-authorized" in payload["error"]["agent_guidance"]


@pytest.mark.asyncio
async def test_stale_cache_401_retries_with_fresh_token(recorder) -> None:
    rec = recorder("fresh-token")
    seen: list[str] = []

    async def attempt(token: str) -> dict:
        seen.append(token)
        if token == "stale-token":
            raise MetaAuthError("code 190: Authentication Error")
        return {"messages": [{"id": "wamid.OK"}]}

    result = await graph_client._with_auth_retry(attempt, "stale-token", BUSINESS, "whatsapp")

    assert result == {"messages": [{"id": "wamid.OK"}]}
    assert seen == ["stale-token", "fresh-token"]
    assert rec.cache_drops == [(BUSINESS, "whatsapp")]
    assert rec.invalidations == [], "a recoverable 401 must not deactivate the channel"


@pytest.mark.asyncio
async def test_second_failure_invalidates_channel_once(recorder) -> None:
    rec = recorder("fresh-token")
    attempts = 0

    async def attempt(token: str) -> dict:
        nonlocal attempts
        attempts += 1
        raise MetaAuthError("code 190: Authentication Error")

    with pytest.raises(MetaAuthError):
        await graph_client._with_auth_retry(attempt, "dead-token", BUSINESS, "whatsapp")

    assert attempts == 2
    assert len(rec.invalidations) == 1
    assert rec.invalidations[0][:2] == (BUSINESS, "whatsapp")


@pytest.mark.asyncio
async def test_unchanged_token_skips_pointless_retry(recorder) -> None:
    rec = recorder("same-token")
    attempts = 0

    async def attempt(token: str) -> dict:
        nonlocal attempts
        attempts += 1
        raise MetaAuthError("code 190: Authentication Error")

    with pytest.raises(MetaAuthError):
        await graph_client._with_auth_retry(attempt, "same-token", BUSINESS, "whatsapp")

    assert attempts == 1, "database returned the same token — retrying cannot help"
    assert len(rec.invalidations) == 1


@pytest.mark.asyncio
async def test_reload_failure_invalidates_and_reraises(recorder) -> None:
    rec = recorder(None)

    async def attempt(token: str) -> dict:
        raise MetaAuthError("code 190: Authentication Error")

    with pytest.raises(MetaAuthError):
        await graph_client._with_auth_retry(attempt, "dead-token", BUSINESS, "whatsapp")

    assert rec.reloads == 1
    assert len(rec.invalidations) == 1


@pytest.mark.asyncio
async def test_without_channel_context_error_propagates_untouched() -> None:
    async def attempt(token: str) -> dict:
        raise MetaAuthError("code 190: Authentication Error")

    with pytest.raises(MetaAuthError):
        await graph_client._with_auth_retry(attempt, "tok", None, None)


@pytest.mark.asyncio
async def test_success_path_calls_attempt_once() -> None:
    calls: list[str] = []

    async def attempt(token: str) -> dict:
        calls.append(token)
        return {"ok": True}

    assert await graph_client._with_auth_retry(attempt, "tok", BUSINESS, "whatsapp") == {"ok": True}
    assert calls == ["tok"]
