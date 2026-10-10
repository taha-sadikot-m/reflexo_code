"""Extract text from files and WhatsApp media for the voice cross-channel bridge."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROCESSING_TIMEOUT_SECONDS = 30
# Docling reads structure better but its PDF pipeline can run past the whole
# budget, so cap it low enough that the native fallback still gets a real slice.
DOCLING_TIMEOUT_SECONDS = 15
# PDFs with a text layer extract natively in milliseconds; only scans need
# Docling's layout pass, so try the cheap path first and escalate on empty text.
_NATIVE_FIRST_KINDS = {"pdf", "docx", "xlsx", "image"}
_NATIVE_TEXT_MIN_CHARS = 40
_docling_converter: Any | None = None
_docling_lock = Lock()

_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image",
}
_AUDIO_TYPES = {
    "audio",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/amr",
    "audio/aac",
    "audio/wav",
    "audio/webm",
}


def _empty_result(
    *,
    kind: str = "unknown",
    text: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    if error:
        meta["error"] = error
    return {"type": kind, "text": text, "tables": [], "metadata": meta}


def _mime_kind(mime_type: str | None, filename: str | None, message_type: str | None) -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    name = (filename or "").lower()
    hint = (message_type or "").lower()
    if hint in {"image", "document", "audio", "video", "text"} and hint != "document":
        if hint == "audio" or mime in _AUDIO_TYPES:
            return "audio"
        if hint == "image" or mime in _IMAGE_TYPES:
            return "image"
        if hint == "video":
            return "video"
        if hint == "text":
            return "text"
    if mime in _AUDIO_TYPES or name.endswith((".ogg", ".mp3", ".wav", ".m4a", ".amr", ".opus")):
        return "audio"
    if mime in _IMAGE_TYPES or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if "pdf" in mime or name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".docx", ".doc")) or "word" in mime:
        return "docx"
    if name.endswith((".xlsx", ".xls")) or "spreadsheet" in mime:
        return "xlsx"
    if name.endswith((".pptx", ".ppt")) or "presentation" in mime:
        return "pptx"
    if "html" in mime or name.endswith((".html", ".htm")):
        return "html"
    if mime.startswith("text/") or name.endswith(".txt"):
        return "text"
    return hint or "unknown"


def _ensure_meta_fernet() -> None:
    try:
        from mcp_servers.meta_mcp.auth.tokens import get_fernet

        get_fernet()
    except Exception:
        from mcp_servers.meta_mcp.auth.tokens import init_fernet
        from mcp_servers.meta_mcp.settings import get_settings

        init_fernet(get_settings().encryption_key)


def _docling_extract(data: bytes, filename: str, mime_type: str) -> tuple[str, list[Any]]:
    global _docling_converter
    from docling.document_converter import DocumentConverter

    suffix = Path(filename).suffix or ".bin"
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        with _docling_lock:
            if _docling_converter is None:
                _docling_converter = DocumentConverter()
            result = _docling_converter.convert(path)
        text = result.document.export_to_markdown()
        tables: list[Any] = []
        try:
            for table in getattr(result.document, "tables", []) or []:
                export = getattr(table, "export_to_dataframe", None)
                if callable(export):
                    frame = export()
                    tables.append(frame.to_dict(orient="records"))
        except Exception:
            tables = []
        return text or "", tables
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _extract_audio_deepgram(data: bytes, mime_type: str) -> str:
    api_key = os.getenv("DEEPGRAM_API_KEY", "")
    if not api_key:
        try:
            from voice.config import get_voice_settings

            api_key = get_voice_settings().deepgram_api_key
        except Exception:
            api_key = ""
    if not api_key:
        return ""
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": mime_type.split(";")[0].strip() or "audio/ogg",
    }
    async with httpx.AsyncClient(timeout=PROCESSING_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-2", "smart_format": "true"},
            headers=headers,
            content=data,
        )
        resp.raise_for_status()
        payload = resp.json()
    return (
        (((payload.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}])[0]
        .get("transcript")
        or ""
    )


async def _fallback_extract(data: bytes, kind: str, filename: str) -> tuple[str, list[Any]]:
    from mcp_servers.reflexo_core.documents.pipeline import (
        _extract_csv,
        _extract_docx,
        _extract_image_gemini,
        _extract_pdf,
    )

    if kind == "pdf":
        return await _extract_pdf(data), []
    if kind == "docx":
        return await _extract_docx(data), []
    if kind == "xlsx":
        try:
            import pandas as pd

            frame = pd.read_excel(io.BytesIO(data))
            return frame.head(50).to_string(index=False), frame.head(50).to_dict(orient="records")
        except Exception:
            return "", []
    if kind == "csv":
        text, rows = await _extract_csv(data)
        return text, rows
    if kind == "image":
        return await _extract_image_gemini(data, filename), []
    if kind in {"text", "html"}:
        return data.decode("utf-8", errors="replace")[:50000], []
    return data.decode("utf-8", errors="replace")[:20000], []


async def _run_docling(
    data: bytes, filename: str, mime_type: str
) -> tuple[str, list[Any]]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_docling_extract, data, filename, mime_type),
            timeout=DOCLING_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.info("Docling extract skipped: %s", exc)
        return "", []


async def _run_fallback(
    data: bytes, kind: str, filename: str
) -> tuple[str, list[Any], str | None]:
    try:
        text, tables = await _fallback_extract(data, kind, filename)
        return text, tables, None
    except Exception as exc:
        logger.warning("Fallback extract failed kind=%s: %s", kind, exc)
        return "", [], str(exc)[:500]


async def process_content(
    source: bytes,
    mime_type: str | None = None,
    filename: str | None = None,
    message_type: str | None = None,
) -> dict[str, Any]:
    """Extract text from in-memory bytes. Times out after 30s."""
    fname = filename or "document"
    kind = _mime_kind(mime_type, fname, message_type)
    metadata = {"filename": fname, "mime_type": mime_type or "", "kind": kind}

    async def _run() -> dict[str, Any]:
        if kind == "audio":
            text = await _extract_audio_deepgram(source, mime_type or "audio/ogg")
            return _empty_result(kind="audio", text=text, metadata=metadata)
        if kind == "text" and not source:
            return _empty_result(kind="text", text="", metadata=metadata)

        text = ""
        tables: list[Any] = []
        used = "fallback"
        error: str | None = None
        simple = kind in {"text", "html", "csv"}
        docling_eligible = not simple and kind not in {"audio", "video"}
        native_first = kind in _NATIVE_FIRST_KINDS

        if simple or native_first:
            text, tables, error = await _run_fallback(source, kind, fname)

        if docling_eligible and len((text or "").strip()) < _NATIVE_TEXT_MIN_CHARS:
            doc_text, doc_tables = await _run_docling(source, fname, mime_type or "")
            if (doc_text or "").strip():
                text, tables, used, error = doc_text, doc_tables, "docling", None

        if not (text or "").strip() and not simple and not native_first:
            text, tables, error = await _run_fallback(source, kind, fname)

        if not (text or "").strip() and error:
            return _empty_result(kind=kind, error=error, metadata=metadata)

        metadata["extractor"] = used
        return {"type": kind, "text": text or "", "tables": tables, "metadata": metadata}

    try:
        return await asyncio.wait_for(_run(), timeout=PROCESSING_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _empty_result(
            kind=kind,
            error="processing_timeout",
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("process_content failed")
        return _empty_result(kind=kind, error=str(exc)[:500], metadata=metadata)


async def process_whatsapp_media(
    business_id: str,
    *,
    media_id: str | None = None,
    media_url: str | None = None,
    message_type: str = "document",
    filename: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Download WhatsApp media (media_id or URL) and extract text."""
    started = time.perf_counter()
    data = b""
    detected_mime = mime_type or ""
    try:
        if media_id:
            _ensure_meta_fernet()
            from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
            from mcp_servers.meta_mcp.utils.media import download_media_from_whatsapp

            token_started = time.perf_counter()
            creds = await ensure_valid_token(business_id, "whatsapp")
            logger.info(
                "DOCUMENT_LATENCY business=%s stage=token duration_ms=%d",
                business_id,
                round((time.perf_counter() - token_started) * 1000),
            )
            download_started = time.perf_counter()
            data, detected_mime = await download_media_from_whatsapp(
                media_id, creds["access_token"]
            )
            logger.info(
                "DOCUMENT_LATENCY business=%s stage=download duration_ms=%d bytes=%d",
                business_id,
                round((time.perf_counter() - download_started) * 1000),
                len(data),
            )
        elif media_url:
            download_started = time.perf_counter()
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(media_url)
                resp.raise_for_status()
                data = resp.content
                detected_mime = detected_mime or resp.headers.get("content-type") or ""
            logger.info(
                "DOCUMENT_LATENCY business=%s stage=download duration_ms=%d bytes=%d",
                business_id,
                round((time.perf_counter() - download_started) * 1000),
                len(data),
            )
        else:
            return _empty_result(kind=message_type or "unknown", error="no_media")
    except Exception as exc:
        logger.warning("WhatsApp media download failed: %s", exc)
        return _empty_result(kind=message_type or "unknown", error=str(exc)[:500])

    extract_started = time.perf_counter()
    result = await process_content(
        data,
        mime_type=detected_mime or mime_type,
        filename=filename,
        message_type=message_type,
    )
    logger.info(
        "DOCUMENT_LATENCY business=%s stage=extract duration_ms=%d total_ms=%d kind=%s extractor=%s error=%s",
        business_id,
        round((time.perf_counter() - extract_started) * 1000),
        round((time.perf_counter() - started) * 1000),
        result.get("type"),
        (result.get("metadata") or {}).get("extractor"),
        bool((result.get("metadata") or {}).get("error")),
    )
    return result
