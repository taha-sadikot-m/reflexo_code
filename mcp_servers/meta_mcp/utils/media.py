"""WhatsApp media upload/download helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from mcp_servers.meta_mcp.settings import get_settings
from mcp_servers.meta_mcp.utils.errors import MediaUploadFailedError
from mcp_servers.meta_mcp.utils.graph_client import graph_get, graph_post_multipart


async def upload_media_to_whatsapp(
    phone_number_id: str,
    access_token: str,
    file_path: str,
    mime_type: str,
) -> str:
    """Upload a local file to WhatsApp media API; return media_id."""
    path = Path(file_path)
    if not path.is_file():
        raise MediaUploadFailedError(f"File not found: {file_path}")

    with path.open("rb") as f:
        file_bytes = f.read()

    try:
        result = await graph_post_multipart(
            f"{phone_number_id}/media",
            access_token,
            files={"file": (path.name, file_bytes, mime_type)},
            data={"messaging_product": "whatsapp", "type": mime_type},
        )
    except Exception as exc:
        raise MediaUploadFailedError(str(exc)) from exc

    media_id = result.get("id")
    if not media_id:
        raise MediaUploadFailedError(f"No media id in response: {result}")
    return str(media_id)


async def download_media_from_whatsapp(
    media_id: str,
    access_token: str,
) -> tuple[bytes, str]:
    """Download media from WhatsApp (two-step URL + binary)."""
    async with httpx.AsyncClient(
        timeout=60.0,
        verify=bool(get_settings().meta_http_verify),
    ) as client:
        meta = await graph_get(media_id, access_token, client=client)
        media_url = meta.get("url")
        mime_type = meta.get("mime_type", "application/octet-stream")
        if not media_url:
            raise MediaUploadFailedError(f"No download URL for media_id={media_id}")
        file_response = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if file_response.status_code >= 400:
            raise MediaUploadFailedError(
                f"Media download failed HTTP {file_response.status_code}"
            )
        return file_response.content, mime_type


async def download_media_url(media_url: str, access_token: str) -> bytes:
    """Download bytes from an already-resolved Meta media URL."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code >= 400:
            raise MediaUploadFailedError(f"Download failed HTTP {response.status_code}")
        return response.content
