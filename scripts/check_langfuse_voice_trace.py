#!/usr/bin/env python3
"""List recent reflexo-voice traces from Langfuse (T10.6 check).

Usage:
  python scripts/check_langfuse_voice_trace.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    public = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret = (os.getenv("LANGFUSE_SECRET_KEY") or "").strip()
    host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    ).rstrip("/")
    if not public or not secret:
        print("ERROR LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
        return 1
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    url = f"{host}/api/public/traces?limit=50"
    req = Request(url, headers={"Authorization": f"Basic {token}"})
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    rows = payload.get("data") or payload.get("traces") or []
    if not rows:
        print("NO_TRACES")
        return 1
    found = 0
    for row in rows:
        name = str(row.get("name") or "")
        metadata = row.get("metadata") or {}
        service = str(metadata.get("service") or "")
        call_id = str(metadata.get("call_id") or "")
        obs = row.get("observations") or []
        kinds = sorted(
            {
                str(item.get("type") or item.get("name") or "")
                for item in obs
                if isinstance(item, dict)
            }
        )
        print(
            f"trace id={row.get('id')} name={name} service={service} "
            f"call_id={call_id} observations={','.join(kinds) or 'n/a'}"
        )
        if (
            service == "reflexo-voice"
            or "voice" in name.lower()
            or "agent_session" in name.lower()
            or "job" in name.lower()
            or call_id
        ):
            found += 1
    if found == 0:
        print("WARN no reflexo-voice tagged traces in this page; exporter is configured, place a call then re-run")
        return 1
    print(f"OK voice_related={found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
