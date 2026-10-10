"""REFLEXO Core MCP server — FastMCP streamable-http on port 8020."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

# Repo root on sys.path for `shared` and `mcp_servers`
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_servers.reflexo_core.prompts import register as register_prompts
from mcp_servers.reflexo_core.resources import register as register_resources
from mcp_servers.reflexo_core.search.embeddings import init_embeddings
from mcp_servers.reflexo_core.settings import get_settings
from mcp_servers.reflexo_core.tools import register_all
from mcp_servers.reflexo_core.utils.cache import close_redis, init_redis
from shared.db import close_pool, init_pool

logger = logging.getLogger("reflexo-core")


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    await init_pool(settings.database_url, application_name="reflexo-core")
    await init_redis(settings.redis_url)
    await init_embeddings()
    logger.info("reflexo-core ready (db + redis)")
    try:
        yield
    finally:
        await close_pool()
        await close_redis()
        logger.info("reflexo-core shut down")


mcp = FastMCP(
    "reflexo-core",
    lifespan=lifespan,
    instructions="""
    REFLEXO Core — the AI employee's brain.

    RULES:
    - business_id is ALWAYS the first parameter on every tool.
    - For general product/service catalogues, use list_entities. For a specific
      product/service use search_entities; for FAQ/policy knowledge use search_knowledge.
    - When you don't know something, call log_gap before telling the customer.
    - When the owner teaches you something, call add_knowledge to remember it.
    - Before creating a workflow rule, call list_workflow_rules to check for duplicates.
    - Never call escalate_to_owner more than once per conversation without a response.
    - For payments/SMS/email/calendar, if a tool returns action_needed connect_*,
      ask the owner to connect that integration first.
    - During onboarding, call update_onboarding_progress after each saved fact,
      and complete_onboarding only when required fields are done.
    """,
)

register_all(mcp)
register_resources(mcp)
register_prompts(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "server": "reflexo-core"})


def main() -> None:
    settings = get_settings()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
