"""Clear LangGraph checkpoints for one business's agent threads.

Use when stale conversation history makes the broker or a specialist repeat an
outdated failure (e.g. a tool that has since been added).

Usage:
    python scripts/reset_owner_threads.py --business-id <uuid>
    python scripts/reset_owner_threads.py --business-id <uuid> --channel instagram_dm
    python scripts/reset_owner_threads.py --business-id <uuid> --customer-id <uuid>
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
from agent.infrastructure import (
    clear_thread_checkpoints,
    close_checkpointer,
    init_checkpointer,
)
from agent.registry import BROKER_DELEGATE_AGENTS, CUSTOMER_SPECIALISTS
from agent.settings import get_agent_settings
from shared.db import close_pool, init_pool


def thread_ids_for(business_id: str, channel: str, customer_id: str | None) -> list[str]:
    """Base session thread plus broker and per-specialist sub-threads."""
    if customer_id:
        base = f"{business_id}:{customer_id}:{channel}"
        specialists = CUSTOMER_SPECIALISTS
    else:
        base = f"{business_id}:owner:{channel}"
        specialists = BROKER_DELEGATE_AGENTS

    threads = [base, f"{base}:broker"]
    threads.extend(f"{base}:spec:{name}" for name in specialists)
    return threads


async def _main(business_id: str, channel: str, customer_id: str | None) -> int:
    settings = get_agent_settings()
    await init_pool(settings.database_url, application_name="reflexo-thread-reset")
    await init_checkpointer()
    try:
        total = 0
        for thread_id in thread_ids_for(business_id, channel, customer_id):
            deleted = await clear_thread_checkpoints(thread_id)
            total += deleted
            print(f"{deleted:>5} checkpoint row(s)  {thread_id}")
        print(f"Total: {total} checkpoint row(s) deleted")
        return 0
    finally:
        await close_checkpointer()
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset agent threads for a business")
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--channel", default="whatsapp")
    parser.add_argument(
        "--customer-id",
        default=None,
        help="Reset a customer thread instead of the owner thread",
    )
    args = parser.parse_args()
    raise SystemExit(run_async(_main(args.business_id, args.channel, args.customer_id)))


if __name__ == "__main__":
    main()
