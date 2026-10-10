"""FastAPI application for signup, admin panel, and widget (port 8030)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Settings read .env via pydantic-settings, which does not export into
# os.environ — load it here too so os.getenv consumers (Meta OAuth,
# capabilities, token refresh) see the same values.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from shared.logging_filters import install_httpx_credential_filter

install_httpx_credential_filter()

from api.oauth_callback import router as oauth_router
from api.settings import get_api_settings
from api.signup import router as signup_router

logger = logging.getLogger(__name__)


def _cors_origins(settings) -> list[str]:
    origins = [settings.admin_frontend_origin.strip()] if settings.admin_frontend_origin else []
    raw = (settings.widget_allowed_origins or "").strip()
    if raw and raw != "*":
        origins.extend(p.strip() for p in raw.split(",") if p.strip())
    origins.extend(
        [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8030",
            "http://127.0.0.1:8030",
        ]
    )
    # Preserve order, drop empties
    seen: set[str] = set()
    out: list[str] = []
    for o in origins:
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


class JsonErrorMiddleware(BaseHTTPMiddleware):
    """Convert unhandled errors into JSON while still inside the CORS layer.

    Starlette's ServerErrorMiddleware wraps CORSMiddleware, so a 500 raised
    there reaches the browser without Access-Control-Allow-Origin and surfaces
    as a CORS failure instead of the real error.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logger.exception("Unhandled error: %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_api_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the signup API")

    from admin.db import close_admin_pool, init_admin_pool
    from shared.db import close_pool, init_pool
    from shared.redis_client import close_redis, init_redis

    await init_pool(settings.database_url, application_name="reflexo-api")
    try:
        await init_admin_pool()
    except Exception as exc:
        logger.warning("Admin pool init skipped: %s", exc)
    try:
        await init_redis(settings.redis_url)
    except Exception as exc:
        logger.warning("Redis init skipped: %s", exc)

    # The simulator runs the agent pipeline in this process, which needs the
    # LangGraph store the agent worker normally opens for itself.
    try:
        from agent.infrastructure import init_store

        await init_store()
    except Exception as exc:
        logger.warning("Agent store init skipped: %s", exc)

    if settings.encryption_key:
        from mcp_servers.meta_mcp.auth.tokens import init_fernet

        init_fernet(settings.encryption_key)

    logger.info("API ready on port %s", settings.port)
    yield

    try:
        from agent.infrastructure import close_store

        await close_store()
    except Exception:
        pass
    await close_admin_pool()
    await close_pool()
    try:
        await close_redis()
    except Exception:
        pass


def create_app() -> FastAPI:
    settings = get_api_settings()
    app = FastAPI(
        title="Reflexo API",
        version="0.8.0",
        lifespan=lifespan,
    )
    # Added first, so it stays inside CORSMiddleware and its 500s get CORS headers.
    app.add_middleware(JsonErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(settings),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.include_router(signup_router)
    app.include_router(oauth_router)
    from api.workflow_studio import (
        router as workflow_studio_router,
        ws_router as workflow_studio_ws_router,
    )

    app.include_router(workflow_studio_router)
    app.include_router(workflow_studio_ws_router)
    from channel_framework.webhook_receiver import router as channel_webhook_router
    from channel_framework.connection_hub import router as connection_hub_router

    app.include_router(channel_webhook_router)
    app.include_router(connection_hub_router)

    from admin.routes import ADMIN_ROUTERS, widget_router
    from admin.routes.public_setup import router as public_setup_router

    for router in ADMIN_ROUTERS:
        app.include_router(router)
    app.include_router(widget_router)
    app.include_router(public_setup_router)

    widget_dir = Path(__file__).resolve().parent.parent / "widget"
    if widget_dir.is_dir():
        app.mount("/widget", StaticFiles(directory=str(widget_dir), html=True), name="widget")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
