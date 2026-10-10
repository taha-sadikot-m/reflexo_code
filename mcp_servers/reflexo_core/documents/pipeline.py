"""Document ingestion pipeline (media_url path)."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection, vec_literal

logger = logging.getLogger(__name__)


def _detect_file_type(filename: str | None, content_type: str | None, url: str) -> str:
    name = (filename or Path(urlparse(url).path).name or "").lower()
    if name.endswith(".pdf") or (content_type and "pdf" in content_type):
        return "pdf"
    if name.endswith((".xlsx", ".xls")) or (content_type and "spreadsheet" in (content_type or "")):
        return "xlsx"
    if name.endswith(".csv") or (content_type and "csv" in (content_type or "")):
        return "csv"
    if name.endswith((".docx", ".doc")):
        return "docx"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if name.endswith(".txt") or (content_type and content_type.startswith("text/")):
        return "txt"
    return "unknown"


def _chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i : i + max_chars])
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)
    return chunks


async def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


async def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        return ""
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text)


async def _extract_csv(data: bytes) -> tuple[str, list[dict]]:
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    preview = "\n".join(str(r) for r in rows[:50])
    return preview, rows


async def _extract_image_gemini(data: bytes, filename: str) -> str:
    import os

    from google import genai
    from google.genai import types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return ""
    client = genai.Client(api_key=api_key)
    mime = "image/jpeg"
    if filename.lower().endswith(".png"):
        mime = "image/png"
    resp = client.models.generate_content(
        model=os.getenv("DOCUMENT_IMAGE_MODEL", "gemini-2.5-flash-lite"),
        contents=[
            "Extract all readable text and product/price information from this business document image.",
            types.Part.from_bytes(data=data, mime_type=mime),
        ],
    )
    return getattr(resp, "text", "") or ""


async def run_document_pipeline(
    business_id: str,
    *,
    media_url: str,
    filename: str | None = None,
    document_type_hint: str | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(media_url)
        resp.raise_for_status()
        data = resp.content
        content_type = resp.headers.get("content-type")

    fname = filename or Path(urlparse(media_url).path).name or "document"
    file_type = _detect_file_type(fname, content_type, media_url)

    async with tenant_connection(business_id) as conn:
        doc = await conn.fetchrow(
            """
            INSERT INTO documents (business_id, filename, file_url, file_type, file_size_bytes, status)
            VALUES ($1::uuid, $2, $3, $4, $5, 'processing')
            RETURNING id
            """,
            business_id,
            fname,
            media_url,
            file_type,
            len(data),
        )
        document_id = doc["id"]

    extracted = ""
    entities_created = 0
    entity_samples: list[dict] = []
    pages = 1

    try:
        if file_type == "pdf":
            extracted = await _extract_pdf(data)
        elif file_type == "docx":
            extracted = await _extract_docx(data)
        elif file_type == "csv":
            extracted, rows = await _extract_csv(data)
            for row in rows[:100]:
                name = row.get("name") or row.get("Name") or row.get("product") or row.get("Product")
                if not name:
                    continue
                price_raw = row.get("price") or row.get("Price") or row.get("amount")
                entity_data: dict[str, Any] = dict(row)
                if price_raw is not None:
                    try:
                        entity_data["price"] = float(str(price_raw).replace(",", "").replace("₹", ""))
                        entity_data["currency"] = "INR"
                    except ValueError:
                        pass
                emb = await embed(str(name))
                async with tenant_connection(business_id) as conn:
                    exists = await conn.fetchval(
                        """
                        SELECT 1 FROM structured_entities
                        WHERE business_id = $1::uuid AND entity_type = 'product'
                          AND lower(name) = lower($2) AND is_active = true
                        """,
                        business_id,
                        str(name),
                    )
                    if not exists:
                        await conn.execute(
                            """
                            INSERT INTO structured_entities
                                (business_id, entity_type, name, description, data, embedding)
                            VALUES ($1::uuid, 'product', $2, $3, $4::jsonb, $5::vector)
                            """,
                            business_id,
                            str(name),
                            None,
                            json.dumps(entity_data),
                            vec_literal(emb),
                        )
                        entities_created += 1
                        if len(entity_samples) < 5:
                            entity_samples.append({"name": str(name), "price": entity_data.get("price")})
        elif file_type == "image":
            extracted = await _extract_image_gemini(data, fname)
        elif file_type == "txt":
            extracted = data.decode("utf-8", errors="replace")
        else:
            extracted = data.decode("utf-8", errors="replace")[:50000]

        chunks = _chunk_text(extracted)
        chunks_created = 0
        for i, chunk in enumerate(chunks):
            emb = await embed(chunk)
            q = f"Document chunk {i + 1} from {fname}"
            if document_type_hint:
                q = f"{document_type_hint}: {q}"
            async with tenant_connection(business_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO knowledge_entries
                        (business_id, question, answer, category, embedding, source)
                    VALUES ($1::uuid, $2, $3, 'general', $4::vector, 'document')
                    """,
                    business_id,
                    q,
                    chunk,
                    vec_literal(emb),
                )
            chunks_created += 1

        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                UPDATE documents
                SET status = 'completed', extracted_text = $1, chunks_created = $2, updated_at = now()
                WHERE id = $3::uuid
                """,
                extracted[:200000],
                chunks_created,
                document_id,
            )

        return {
            "document_id": str(document_id),
            "filename": fname,
            "file_type": file_type,
            "pages": pages,
            "status": "completed",
            "extraction_summary": {
                "text_chunks_created": chunks_created,
                "entities_created": entities_created,
                "entity_types": {"product": entities_created} if entities_created else {},
                "total_characters_extracted": len(extracted),
                "sample_entities": entity_samples,
            },
            "message": (
                f"Processed {file_type}. Created {entities_created} entities "
                f"and {chunks_created} knowledge chunks."
            ),
        }
    except Exception as exc:
        logger.exception("Document pipeline failed")
        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                UPDATE documents
                SET status = 'failed', processing_error = $1, updated_at = now()
                WHERE id = $2::uuid
                """,
                str(exc)[:2000],
                document_id,
            )
        raise
