#!/usr/bin/env python3
"""Live check: an owner simulator turn returns a reply with real per-stage timings.

Usage:
    python scripts/verify_simulator_trace.py <business_id> [message]
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

BASE = f"http://127.0.0.1:{os.getenv('API_PORT', '8030')}"
ORIGIN = os.getenv("ADMIN_FRONTEND_ORIGIN", "http://localhost:3000")
DEV_SECRET = "dev-only-reflexo-admin-jwt-secret-do-not-use-in-prod"

DEFAULT_MESSAGE = (
    "i want to create the workflow then when any customer message the code "
    "#123456 then reply them with the 10% discount"
)


async def _token() -> str:
    import asyncpg
    import jwt

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        admin = await conn.fetchrow(
            """
            SELECT id, email, role FROM admin_users
            WHERE is_active AND role IN ('admin', 'super_admin')
            ORDER BY created_at LIMIT 1
            """
        )
    finally:
        await conn.close()
    if not admin:
        raise SystemExit("no active admin user")
    now = datetime.now(timezone.utc)
    secret = (os.getenv("ADMIN_JWT_SECRET") or "").strip() or DEV_SECRET
    return jwt.encode(
        {
            "sub": str(admin["id"]),
            "email": admin["email"],
            "role": admin["role"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )


async def _retry_queue_depth(business_id: str) -> int:
    from shared.redis_client import close_redis, get_redis, init_redis
    from shared.redis_keys import retry_queue_key

    await init_redis(os.environ["REDIS_URL"])
    try:
        return int(await get_redis().llen(retry_queue_key(business_id)) or 0)
    finally:
        await close_redis()


async def main() -> int:
    import httpx

    if len(sys.argv) < 2:
        raise SystemExit("business_id is required")
    business_id = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MESSAGE
    headers = {"Authorization": f"Bearer {await _token()}", "Origin": ORIGIN}

    async with httpx.AsyncClient(timeout=300.0, headers=headers) as client:
        res = await client.post(
            f"{BASE}/api/admin/test-simulator/message",
            json={
                "business_id": business_id,
                "text": message,
                "channel": "whatsapp",
                "mode": "owner",
            },
        )
    print("simulator message ->", res.status_code)
    if res.status_code != 200:
        print(res.text[:500])
        return 1
    body = res.json()
    print("  error:", body.get("error"))
    print("  specialist:", body.get("specialist"), "tools:", body.get("tools"))
    print("  response:", str(body.get("response"))[:600])

    steps = ((body.get("trace") or {}).get("steps")) or []
    print("\n  stage timings")
    timed = 0
    for step in steps:
        ms = step.get("ms")
        if isinstance(ms, (int, float)) and ms > 0:
            timed += 1
        print(
            f"    {str(step.get('name')):<11}"
            f"{'not timed' if ms is None else str(ms) + 'ms':>12}"
            f"  {step.get('status')}  {str(step.get('detail'))[:60]}"
        )
    print("  total_ms:", (body.get("trace") or {}).get("total_ms"))

    depth = await _retry_queue_depth(business_id)
    print("\n  retry queue depth:", depth)

    ok = bool(body.get("response")) and not body.get("error") and timed >= 3 and depth == 0
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
