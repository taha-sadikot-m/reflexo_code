"""One-off / cron entry for LangGraph checkpoint TTL cleanup.

Usage:
    python scripts/cleanup_checkpoints.py
    python scripts/cleanup_checkpoints.py --days 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agent.asyncio_compat import run_async
from agent.infrastructure import cleanup_old_checkpoints, close_checkpointer, init_checkpointer
from agent.settings import get_agent_settings
from shared.db import close_pool, init_pool


async def _main(days: int) -> int:
    settings = get_agent_settings()
    await init_pool(settings.database_url, application_name="reflexo-checkpoint-cleanup")
    await init_checkpointer()
    try:
        deleted = await cleanup_old_checkpoints(days=days)
        print(f"Deleted {deleted} checkpoint row(s) older than {days} day(s)")
        return 0
    finally:
        await close_checkpointer()
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean old LangGraph checkpoints")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    raise SystemExit(run_async(_main(args.days)))


if __name__ == "__main__":
    main()
