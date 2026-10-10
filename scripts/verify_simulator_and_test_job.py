#!/usr/bin/env python3
"""Live check: simulator pipeline runs, and a stateful test job reports progress.

Usage:
    python scripts/verify_simulator_and_test_job.py <business_id> [workflow_id]
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


async def main() -> int:
    import httpx

    if len(sys.argv) < 2:
        raise SystemExit("business_id is required")
    business_id = sys.argv[1]
    workflow_id = sys.argv[2] if len(sys.argv) > 2 else None
    headers = {"Authorization": f"Bearer {await _token()}", "Origin": ORIGIN}

    async with httpx.AsyncClient(timeout=180.0, headers=headers) as client:
        message = await client.post(
            f"{BASE}/api/admin/test-simulator/message",
            json={
                "business_id": business_id,
                "text": "Create an automation that replies to every comment",
                "channel": "instagram_comment",
                "mode": "owner",
            },
        )
        print("simulator message ->", message.status_code)
        body = message.json() if message.status_code == 200 else {}
        print("  error:", body.get("error"))
        print("  specialist:", body.get("specialist"), "tools:", body.get("tools"))
        print("  response:", str(body.get("response"))[:400])
        if message.status_code != 200 or body.get("error"):
            return 1

        if not workflow_id:
            return 0

        started = await client.post(
            f"{BASE}/api/admin/workflows/{business_id}/ai/{workflow_id}/test"
        )
        print("\nstart test ->", started.status_code, started.text[:200])
        if started.status_code != 202:
            return 1
        job_id = started.json()["job_id"]

        seen: list[str] = []
        for _ in range(180):
            await asyncio.sleep(1)
            poll = await client.get(
                f"{BASE}/api/admin/workflows/{business_id}/ai/{workflow_id}/test/{job_id}"
            )
            if poll.status_code != 200:
                print("poll failed:", poll.status_code, poll.text[:200])
                return 1
            job = poll.json()
            marker = f"{job['stage']}:{job.get('events_processed')}/{job.get('events_total')}"
            if marker not in seen:
                seen.append(marker)
                print("  ", marker)
            if job["stage"] in {"completed", "failed"}:
                print("\nfinal stage:", job["stage"])
                print("  error:", job.get("error"), job.get("hint"))
                results = job.get("results") or {}
                print("  all_passed:", results.get("all_passed"))
                for line in results.get("summary") or []:
                    print("   -", line)
                print("  distinct progress updates:", len(seen))
                return 0 if job["stage"] == "completed" else 1
    print("timed out waiting for job")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
