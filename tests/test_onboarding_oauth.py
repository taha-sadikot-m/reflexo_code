"""Unit tests for Meta OAuth state encoding and URL builders."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from onboarding.integrations import (
    decode_oauth_state,
    encode_oauth_state,
    generate_facebook_oauth_url,
    generate_instagram_oauth_url,
)


def test_oauth_state_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    payload = {"business_id": "biz-1", "integration": "instagram", "redirect": "onboarding"}
    state = encode_oauth_state(payload)
    assert decode_oauth_state(state) == payload


def test_oauth_state_tamper_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    state = encode_oauth_state({"business_id": "x", "integration": "facebook"})
    body, sig = state.rsplit(".", 1)
    with pytest.raises(ValueError):
        decode_oauth_state(f"{body}.deadbeef")


def test_instagram_oauth_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_APP_ID", "123456")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "https://example.com/api/oauth/callback")
    monkeypatch.setenv("META_API_VERSION", "v22.0")
    url = generate_instagram_oauth_url("00000000-0000-0000-0000-000000000099")
    assert "facebook.com/v22.0/dialog/oauth" in url
    assert "client_id=123456" in url
    assert "instagram_manage_messages" in url
    assert "state=" in url


def test_facebook_oauth_url_rerequest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_APP_ID", "123456")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "https://example.com/api/oauth/callback")
    monkeypatch.setenv("META_API_VERSION", "v22.0")
    url = generate_facebook_oauth_url(
        "00000000-0000-0000-0000-000000000099", mode="rerequest"
    )
    assert "auth_type=rerequest" in url
    assert "pages_manage_engagement" in url
    plain = generate_facebook_oauth_url("00000000-0000-0000-0000-000000000099")
    assert "auth_type=rerequest" not in plain


@pytest.mark.parametrize(
    "builder",
    [generate_instagram_oauth_url, generate_facebook_oauth_url],
)
def test_meta_fresh_oauth_forces_reauthentication_and_rerequest(
    monkeypatch: pytest.MonkeyPatch, builder
) -> None:
    monkeypatch.setenv("META_APP_ID", "123456")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.setenv(
        "META_OAUTH_REDIRECT_URI", "https://example.com/api/oauth/callback"
    )
    url = builder("00000000-0000-0000-0000-000000000099", mode="fresh")
    query = parse_qs(urlparse(url).query)
    assert query["auth_type"] == ["reauthenticate,rerequest"]
    assert query["return_scopes"] == ["true"]


def test_facebook_oauth_url_requires_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_APP_ID", raising=False)
    with pytest.raises(ValueError, match="META_APP_ID"):
        generate_facebook_oauth_url("biz")
