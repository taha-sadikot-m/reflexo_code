#!/usr/bin/env python3
"""Live check: a test job fails fast with a hint when no worker polls the queue.

Usage:
    python scripts/verify_test_job_worker_down.py <business_id> <workflow_id>
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_simulator_and_test_job import BASE, ORIGIN, _token


async def main() -> int:
    import httpx

    business_id, workflow_id = sys.argv[1], sys.argv[2]
    headers = {"Authorization": f"Bearer {await _token()}", "Origin": ORIGIN}
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        started = time.monotonic()
        res = await client.post(
            f"{BASE}/api/admin/workflows/{business_id}/ai/{workflow_id}/test"
        )
        if res.status_code != 202:
            print("start failed:", res.status_code, res.text[:200])
            return 1
        job_id = res.json()["job_id"]
        for _ in range(30):
            await asyncio.sleep(1)
            job = (
                await client.get(
                    f"{BASE}/api/admin/workflows/{business_id}/ai/{workflow_id}/test/{job_id}"
                )
            ).json()
            if job["stage"] in {"completed", "failed"}:
                print(f"stage={job['stage']} after {time.monotonic() - started:.1f}s")
                print("  error:", job.get("error"))
                print("  hint:", job.get("hint"))
                return 0 if job["stage"] == "failed" else 1
    print("job did not settle")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
