#!/usr/bin/env python3
"""Smoke checks for Meta MCP (no live Meta API calls required)."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.meta_mcp.auth.signature import verify_signature
from mcp_servers.meta_mcp.webhooks.whatsapp import extract_whatsapp_content


def test_signature() -> None:
    secret = "smoke_secret"
    body = b'{"object":"page"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret), "signature verify failed"
    assert not verify_signature(body, "sha256=bad", secret), "bad signature accepted"
    print("OK signature")


def test_extract() -> None:
    content = extract_whatsapp_content(
        {"type": "text", "text": {"body": "smoke"}}
    )
    assert content["text"] == "smoke"
    print("OK extract_whatsapp_content")


def test_settings_defaults() -> None:
    from mcp_servers.meta_mcp.settings import Settings

    # Don't load real .env requirements — just check port default via model fields
    assert Settings.model_fields["port"].default == 8021
    assert Settings.model_fields["meta_api_version"].default == "v22.0"
    print("OK settings defaults")


def test_import_server() -> None:
    # Import should succeed without starting the server
    from mcp_servers.meta_mcp import server as meta_server

    assert meta_server.mcp.name == "meta-mcp"
    routes = {r.path for r in meta_server.mcp._get_additional_http_routes()}
    assert "/webhook" in routes
    assert "/health" in routes
    print("OK server import + custom routes")


def main() -> None:
    test_signature()
    test_extract()
    test_settings_defaults()
    test_import_server()
    print("smoke_meta_mcp: all checks passed")


if __name__ == "__main__":
    main()
