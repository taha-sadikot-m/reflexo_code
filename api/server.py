"""Run: python -m api.server"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from api.settings import get_api_settings


def main() -> None:
    settings = get_api_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    reload = os.getenv("API_RELOAD", "").lower() in ("1", "true", "yes")
    if sys.platform == "win32" and not reload:
        # The simulator runs the agent pipeline in this process, and psycopg
        # async (LangGraph store/checkpointer) refuses uvicorn's default
        # ProactorEventLoop on Windows.
        from agent.asyncio_compat import run_async

        server = uvicorn.Server(
            uvicorn.Config("api:app", host=settings.host, port=settings.port)
        )
        run_async(server.serve())
        return
    uvicorn.run(
        "api:app",
        host=settings.host,
        port=settings.port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
