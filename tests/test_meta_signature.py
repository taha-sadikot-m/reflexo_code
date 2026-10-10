"""Unit tests for Meta webhook HMAC signature verification."""

from __future__ import annotations

import hashlib
import hmac

from mcp_servers.meta_mcp.auth.signature import verify_signature


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_signature_valid():
    secret = "test_app_secret"
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(body, _sign(secret, body), secret) is True


def test_verify_signature_invalid():
    secret = "test_app_secret"
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(body, "sha256=deadbeef", secret) is False


def test_verify_signature_missing_prefix():
    secret = "test_app_secret"
    body = b"{}"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, digest, secret) is False


def test_verify_signature_empty_secret():
    body = b"{}"
    assert verify_signature(body, "sha256=abc", "") is False
