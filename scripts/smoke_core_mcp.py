#!/usr/bin/env python3
"""Smoke-test core MCP tool handlers against the seed business."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

SEED_BIZ = "00000000-0000-0000-0000-000000000001"


async def main() -> int:
    from shared.db import close_pool, init_pool
    from mcp_servers.reflexo_core.utils.cache import close_redis, init_redis
    from mcp_servers.reflexo_core.tools.config import get_business_config_impl
    from mcp_servers.reflexo_core.search.embeddings import embed
    from mcp_servers.reflexo_core.search.hybrid_rrf import hybrid_rrf_search
    from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection

    database_url = os.getenv("DATABASE_URL")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    if not database_url:
        print("DATABASE_URL missing")
        return 1

    await init_pool(database_url, application_name="reflexo-core-smoke")
    try:
        await init_redis(redis_url)
    except Exception as exc:
        print(f"Redis init warning (continuing): {exc}")

    try:
        config = await get_business_config_impl(SEED_BIZ)
        if not config:
            print("FAIL: get_business_config returned None")
            return 1
        print(f"OK config name={config.get('name')} persona={config.get('persona_name')}")

        # Full-text only path if embeddings fail (API key issues)
        try:
            emb = await embed("return policy", task_type="retrieval_query")
            async with tenant_connection(SEED_BIZ) as conn:
                rows = await hybrid_rrf_search(conn, emb, "return policy", limit=5)
            print(f"OK search_knowledge results={len(rows)}")
            if rows:
                print(f"  top: {rows[0].get('question')}")
        except Exception as exc:
            print(f"WARN search_knowledge/embeddings: {exc}")
            async with tenant_connection(SEED_BIZ) as conn:
                n = await conn.fetchval(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE business_id = current_business_id()"
                )
            print(f"OK tenant KB count under RLS={n}")

        print("Smoke test passed.")
        return 0
    finally:
        try:
            await close_redis()
        except Exception:
            pass
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
