"""Validation for outbound MCP URLs and registry-declared inputs."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse


class McpSecurityError(ValueError):
    pass


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "development").lower() in {"prod", "production"}


async def validate_remote_url(url: str) -> str:
    """Reject URLs that could reach internal infrastructure in production."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpSecurityError("A valid HTTP MCP endpoint is required")
    if parsed.username or parsed.password:
        raise McpSecurityError("Credentials are not allowed in MCP URLs")
    if _is_production() and parsed.scheme != "https":
        raise McpSecurityError("Production MCP endpoints must use HTTPS")

    host = parsed.hostname.lower().rstrip(".")
    if not _is_production() and (
        host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".test")
    ):
        return url.rstrip("/")
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, host, parsed.port, type=socket.SOCK_STREAM
            ),
            timeout=3.0,
        )
    except (OSError, TimeoutError) as exc:
        raise McpSecurityError("MCP hostname could not be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise McpSecurityError("Private or local MCP endpoints are not allowed")
    return url.rstrip("/")


_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_remote(
    remote: dict[str, Any],
    *,
    variables: dict[str, str],
    headers: dict[str, str],
) -> tuple[str, dict[str, str]]:
    """Resolve only fields declared by a verified Registry remote."""
    declared_vars = remote.get("variables") or {}
    unknown_vars = set(variables) - set(declared_vars)
    if unknown_vars:
        raise McpSecurityError(f"Unknown URL variable: {sorted(unknown_vars)[0]}")
    values: dict[str, str] = {}
    for name, spec in declared_vars.items():
        value = str(variables.get(name) or spec.get("default") or "").strip()
        if spec.get("required") and not value:
            raise McpSecurityError(f"Missing required URL variable: {name}")
        choices = [str(x) for x in (spec.get("choices") or [])]
        if value and choices and value not in choices:
            raise McpSecurityError(f"Invalid value for URL variable: {name}")
        if any(ch in value for ch in "\r\n"):
            raise McpSecurityError(f"Invalid value for URL variable: {name}")
        values[name] = value

    template = str(remote.get("url") or "")
    needed = set(_TEMPLATE_RE.findall(template))
    if not needed.issubset(values):
        raise McpSecurityError("The Registry URL has undeclared variables")
    resolved_url = _TEMPLATE_RE.sub(lambda match: values[match.group(1)], template)

    declared_headers = {
        str(spec.get("name")): spec for spec in (remote.get("headers") or [])
    }
    unknown_headers = set(headers) - set(declared_headers)
    if unknown_headers:
        raise McpSecurityError(f"Unknown MCP header: {sorted(unknown_headers)[0]}")
    resolved_headers: dict[str, str] = {}
    for name, spec in declared_headers.items():
        value = str(headers.get(name) or spec.get("default") or "").strip()
        if spec.get("required") and not value:
            raise McpSecurityError(f"Missing required MCP header: {name}")
        choices = [str(x) for x in (spec.get("choices") or [])]
        if value and choices and value not in choices:
            raise McpSecurityError(f"Invalid value for MCP header: {name}")
        if "\r" in value or "\n" in value:
            raise McpSecurityError(f"Invalid value for MCP header: {name}")
        if value:
            resolved_headers[name] = value
    return resolved_url, resolved_headers
