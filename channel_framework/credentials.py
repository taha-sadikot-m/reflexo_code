"""AES-256-GCM credential encryption and tenant-scoped storage."""

from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from shared.db import tenant_connection

_VERSION = b"\x01"
_NONCE_BYTES = 12
_DEFAULT_CHANNEL_CONFIG = {
    "send_mode": "threshold",
    "auto_send_threshold": 0.85,
    "approval_threshold": 0.60,
    "notify_on_auto_send": False,
}


def load_encryption_key(value: str | bytes | None = None) -> bytes:
    raw = value if value is not None else os.getenv("CHANNEL_ENCRYPTION_KEY", "")
    if isinstance(raw, bytes):
        key = raw
    else:
        text = raw.strip()
        try:
            key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        except Exception as exc:
            raise ValueError("CHANNEL_ENCRYPTION_KEY must be base64 encoded") from exc
        if len(key) != 32 and len(text.encode()) == 32:
            key = text.encode()
    if len(key) != 32:
        raise ValueError("CHANNEL_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def encrypt_secret(
    plaintext: str,
    *,
    key: str | bytes | None = None,
    associated_data: str = "",
) -> bytes:
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("credential plaintext is required")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(load_encryption_key(key)).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        associated_data.encode("utf-8") or None,
    )
    return _VERSION + nonce + ciphertext


def decrypt_secret(
    envelope: bytes,
    *,
    key: str | bytes | None = None,
    associated_data: str = "",
) -> str:
    value = bytes(envelope)
    if len(value) <= 1 + _NONCE_BYTES or value[:1] != _VERSION:
        raise ValueError("invalid encrypted credential envelope")
    plaintext = AESGCM(load_encryption_key(key)).decrypt(
        value[1 : 1 + _NONCE_BYTES],
        value[1 + _NONCE_BYTES :],
        associated_data.encode("utf-8") or None,
    )
    return plaintext.decode("utf-8")


def _aad(business_id: str, platform: str, kind: str) -> str:
    return f"reflexo:{business_id}:{platform.lower()}:{kind}"


async def store_tokens(
    *,
    business_id: str,
    platform: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    scopes: list[str] | None = None,
    auth_type: str = "oauth2",
    account_identifier: str | None = None,
    account_display_name: str | None = None,
    capabilities: list[str] | None = None,
    is_communication_channel: bool | None = None,
    channel_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = platform.strip().lower()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in))
        if expires_in is not None
        else None
    )
    encrypted_access = encrypt_secret(
        access_token,
        associated_data=_aad(business_id, normalized, "access"),
    )
    encrypted_refresh = (
        encrypt_secret(
            refresh_token,
            associated_data=_aad(business_id, normalized, "refresh"),
        )
        if refresh_token
        else None
    )
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO connected_accounts (
                business_id, platform, auth_type, access_token_encrypted,
                refresh_token_encrypted, token_expires_at, scopes,
                account_identifier, account_display_name, capabilities,
                is_communication_channel, channel_config
            ) VALUES (
                $1::uuid, $2, $3, $4, $5, $6, $7::text[],
                $8, $9, $10::text[], COALESCE($11::boolean, false), $12::jsonb
            )
            ON CONFLICT (business_id, platform) DO UPDATE SET
                auth_type = EXCLUDED.auth_type,
                access_token_encrypted = EXCLUDED.access_token_encrypted,
                refresh_token_encrypted = COALESCE(
                    EXCLUDED.refresh_token_encrypted,
                    connected_accounts.refresh_token_encrypted
                ),
                token_expires_at = EXCLUDED.token_expires_at,
                scopes = CASE WHEN $13 THEN EXCLUDED.scopes
                    ELSE connected_accounts.scopes END,
                account_identifier = COALESCE(
                    EXCLUDED.account_identifier,
                    connected_accounts.account_identifier
                ),
                account_display_name = COALESCE(
                    EXCLUDED.account_display_name,
                    connected_accounts.account_display_name
                ),
                capabilities = CASE WHEN $14 THEN EXCLUDED.capabilities
                    ELSE connected_accounts.capabilities END,
                is_communication_channel = CASE WHEN $15 THEN
                    EXCLUDED.is_communication_channel
                    ELSE connected_accounts.is_communication_channel END,
                channel_config = CASE WHEN $16 THEN EXCLUDED.channel_config
                    ELSE connected_accounts.channel_config END,
                is_active = true,
                connection_error = NULL,
                updated_at = now()
            RETURNING id, platform, token_expires_at, is_active
            """,
            business_id,
            normalized,
            auth_type,
            encrypted_access,
            encrypted_refresh,
            expires_at,
            scopes or [],
            account_identifier,
            account_display_name,
            capabilities or [],
            is_communication_channel,
            json.dumps(channel_config or _DEFAULT_CHANNEL_CONFIG),
            scopes is not None,
            capabilities is not None,
            is_communication_channel is not None,
            channel_config is not None,
        )
    return {
        "id": str(row["id"]),
        "platform": row["platform"],
        "token_expires_at": row["token_expires_at"],
        "is_active": bool(row["is_active"]),
    }


async def load_tokens(business_id: str, platform: str) -> dict[str, Any] | None:
    normalized = platform.strip().lower()
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM connected_accounts
            WHERE business_id = $1::uuid AND platform = $2 AND is_active = true
            """,
            business_id,
            normalized,
        )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "business_id": business_id,
        "platform": normalized,
        "access_token": decrypt_secret(
            row["access_token_encrypted"],
            associated_data=_aad(business_id, normalized, "access"),
        ),
        "refresh_token": (
            decrypt_secret(
                row["refresh_token_encrypted"],
                associated_data=_aad(business_id, normalized, "refresh"),
            )
            if row["refresh_token_encrypted"]
            else None
        ),
        "token_expires_at": row["token_expires_at"],
        "scopes": list(row["scopes"] or []),
        "account_identifier": row["account_identifier"],
        "capabilities": list(row["capabilities"] or []),
        "channel_config": dict(row["channel_config"] or {}),
    }


async def update_access_token(
    business_id: str,
    platform: str,
    access_token: str,
    *,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> bool:
    """Rotate OAuth token material without changing account policy or metadata."""
    normalized = platform.strip().lower()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in))
        if expires_in is not None
        else None
    )
    access = encrypt_secret(
        access_token,
        associated_data=_aad(business_id, normalized, "access"),
    )
    refresh = (
        encrypt_secret(
            refresh_token,
            associated_data=_aad(business_id, normalized, "refresh"),
        )
        if refresh_token
        else None
    )
    async with tenant_connection(business_id) as conn:
        result = await conn.execute(
            """
            UPDATE connected_accounts
            SET access_token_encrypted = $3,
                refresh_token_encrypted = COALESCE(
                    $4, refresh_token_encrypted
                ),
                token_expires_at = $5,
                connection_error = NULL,
                updated_at = now()
            WHERE business_id = $1::uuid AND platform = $2
            """,
            business_id,
            normalized,
            access,
            refresh,
            expires_at,
        )
    return result.endswith(" 1")


async def get_valid_tokens(
    business_id: str,
    platform: str,
) -> dict[str, Any] | None:
    """Load credentials and refresh expiring Gmail/Outlook OAuth tokens."""
    tokens = await load_tokens(business_id, platform)
    if not tokens:
        return None
    expires_at = tokens.get("token_expires_at")
    if (
        not expires_at
        or expires_at > datetime.now(timezone.utc) + timedelta(minutes=2)
    ):
        return tokens
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise RuntimeError(f"{platform} OAuth token expired without refresh token")
    normalized = platform.strip().lower()
    if normalized == "gmail":
        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    elif normalized == "outlook":
        tenant = os.getenv("MICROSOFT_TENANT_ID", "common")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = {
            "client_id": os.environ["MICROSOFT_CLIENT_ID"],
            "client_secret": os.environ["MICROSOFT_CLIENT_SECRET"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    else:
        return tokens
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, data=data)
        response.raise_for_status()
        refreshed = response.json()
    await update_access_token(
        business_id,
        normalized,
        str(refreshed["access_token"]),
        refresh_token=refreshed.get("refresh_token"),
        expires_in=int(refreshed.get("expires_in") or 3600),
    )
    return await load_tokens(business_id, normalized)


async def list_connected_accounts(business_id: str) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT platform, auth_type, account_identifier,
                   account_display_name, capabilities,
                   is_communication_channel, channel_config,
                   is_active, connection_error, token_expires_at,
                   created_at, updated_at
            FROM connected_accounts
            WHERE business_id = $1::uuid
            ORDER BY platform
            """,
            business_id,
        )
    return [dict(row) for row in rows]


async def delete_account(business_id: str, platform: str) -> bool:
    async with tenant_connection(business_id) as conn:
        result = await conn.execute(
            """
            DELETE FROM connected_accounts
            WHERE business_id = $1::uuid AND platform = $2
            """,
            business_id,
            platform.strip().lower(),
        )
    return result.endswith(" 1")
