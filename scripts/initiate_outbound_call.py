#!/usr/bin/env python3
"""Place an outbound WhatsApp voice call for live testing.

The SDP offer and its peer connection must live in the process that receives
the `calls` webhook, so this script asks the running meta-mcp server to place
the call instead of doing it itself.

Prerequisites: meta-mcp running (port 8021), VOICE_INTERNAL_TOKEN set in .env,
a voice worker running, and WhatsApp calling enabled on the number.

Usage:
  python scripts/initiate_outbound_call.py --to +91... --reason "your appointment"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DEFAULT_BIZ = "00000000-0000-0000-0000-000000000001"


def main() -> int:
    parser = argparse.ArgumentParser(description="Initiate an outbound WhatsApp voice call")
    parser.add_argument("--to", default="", help="E.164 recipient, or META_TEST_RECIPIENT")
    parser.add_argument("--reason", default="a follow-up from the business")
    parser.add_argument("--customer-name", default="there")
    parser.add_argument(
        "--business-id",
        default=(os.getenv("WHATSAPP_SEED_BUSINESS_ID") or DEFAULT_BIZ).strip(),
    )
    parser.add_argument(
        "--server",
        default=f"http://localhost:{os.getenv('META_MCP_PORT', '8021')}",
        help="Base URL of the running meta-mcp server",
    )
    args = parser.parse_args()

    token = (os.getenv("VOICE_INTERNAL_TOKEN") or "").strip()
    if not token:
        print("ERROR: VOICE_INTERNAL_TOKEN is missing in .env (also needed by meta-mcp)")
        return 1
    to_phone = (args.to or os.getenv("META_TEST_RECIPIENT") or "").strip()
    if not to_phone:
        print("ERROR: pass --to or set META_TEST_RECIPIENT")
        return 1

    url = f"{args.server.rstrip('/')}/internal/voice/outbound-call"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "business_id": args.business_id,
                "to": to_phone,
                "reason": args.reason,
                "customer_name": args.customer_name,
            },
            timeout=90.0,
        )
    except httpx.HTTPError as exc:
        print(f"ERROR could not reach meta-mcp at {url}: {exc}")
        return 1

    try:
        body = response.json()
    except json.JSONDecodeError:
        print(f"ERROR HTTP {response.status_code} body={response.text[:400]}")
        return 1

    if response.status_code != 200:
        print(f"ERROR HTTP {response.status_code}: {body.get('error') or body}")
        return 1
    print(f"OK call_id={body.get('call_id')} ringing={to_phone}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
