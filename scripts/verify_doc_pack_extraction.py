"""Run the HolonCode doc pack through the real extractor, offline.

Feeds each asset to shared.document_processor.process_content with the same
mime_type / message_type values Meta sends for WhatsApp media, so a slow or
unreadable file shows up here instead of during a live call.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

PACK_DIR = ROOT / "tests" / "assets" / "voice_doc_pack"

# (filename, mime_type, message_type) mirroring the Meta webhook payload.
CASES = [
    ("HolonCode_Services.pdf", "application/pdf", "document"),
    (
        "HolonCode_Onboarding_Note.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document",
    ),
    (
        "HolonCode_Price_List.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "document",
    ),
    ("HolonCode_Price_Card.png", "image/png", "image"),
]

# The voice tool clips extracted text at 1500 chars before the LLM sees it.
VOICE_TEXT_LIMIT = 1500
SLOW_SECONDS = 20.0


async def _main() -> int:
    from shared.document_processor import process_content

    missing = [name for name, _, _ in CASES if not (PACK_DIR / name).exists()]
    if missing:
        print("missing assets:", ", ".join(missing))
        print("run: python scripts/make_voice_doc_test_pack.py")
        return 1

    rows = []
    failures = 0
    for name, mime_type, message_type in CASES:
        data = (PACK_DIR / name).read_bytes()
        started = time.perf_counter()
        result = await process_content(
            data,
            mime_type=mime_type,
            filename=name,
            message_type=message_type,
        )
        elapsed = time.perf_counter() - started

        metadata = result.get("metadata") or {}
        text = str(result.get("text") or "")
        error = metadata.get("error")
        chars = len(text)
        status = "ok"
        if error or not text.strip():
            status = f"FAIL ({error or 'no_text'})"
            failures += 1
        elif elapsed > SLOW_SECONDS:
            status = "SLOW"
            failures += 1
        elif chars > VOICE_TEXT_LIMIT:
            status = "CLIPPED"

        rows.append(
            {
                "file": name,
                "kind": result.get("type"),
                "extractor": metadata.get("extractor", "-"),
                "seconds": f"{elapsed:.1f}",
                "chars": chars,
                "tables": len(result.get("tables") or []),
                "status": status,
            }
        )
        print(f"--- {name} ---")
        print(f"kind={result.get('type')} extractor={metadata.get('extractor', '-')} "
              f"seconds={elapsed:.1f} chars={chars} tables={len(result.get('tables') or [])}")
        preview = " ".join(text.split())[:200]
        print(f"preview: {preview}")
        print()

    widths = {key: max(len(key), *(len(str(row[key])) for row in rows)) for key in rows[0]}
    header = "  ".join(key.ljust(widths[key]) for key in rows[0])
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row[key]).ljust(widths[key]) for key in row))

    print()
    print(f"clip limit for the voice tool: {VOICE_TEXT_LIMIT} chars")
    if failures:
        print(f"{failures} asset(s) need attention")
    else:
        print("all assets extract cleanly within the voice timeout")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
