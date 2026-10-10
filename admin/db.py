"""Admin service-role connection pool (bypasses RLS)."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from api.settings import get_api_settings

logger = logging.getLogger(__name__)

_admin_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    settings = get_api_settings()
    service = (settings.database_url_service_role or "").strip()
    fallback = (settings.database_url or "").strip()
    if service:
        return service
    env = (settings.environment or "development").lower()
    if env in ("production", "prod") and not service:
        raise RuntimeError("DATABASE_URL_SERVICE_ROLE is required in production")
    if fallback:
        logger.warning(
            "DATABASE_URL_SERVICE_ROLE unset; admin pool is using DATABASE_URL "
            "(local development only)"
        )
        return fallback
    raise RuntimeError("DATABASE_URL or DATABASE_URL_SERVICE_ROLE is required")


async def init_admin_pool() -> asyncpg.Pool:
    global _admin_pool
    if _admin_pool is not None:
        return _admin_pool
    _admin_pool = await asyncpg.create_pool(
        _dsn(),
        min_size=2,
        max_size=5,
        command_timeout=30,
        server_settings={"application_name": "reflexo-admin"},
    )
    return _admin_pool


async def get_admin_pool() -> asyncpg.Pool:
    if _admin_pool is None:
        return await init_admin_pool()
    return _admin_pool


async def close_admin_pool() -> None:
    global _admin_pool
    if _admin_pool is not None:
        await _admin_pool.close()
        _admin_pool = None


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "hex") and not isinstance(value, (str, bytes, int)):
        return str(value)
    return value
