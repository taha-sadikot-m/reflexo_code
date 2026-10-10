from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    redis_url: str = "redis://localhost:6379"
    encryption_key: str = ""

    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_app_id: str = ""
    meta_api_version: str = "v22.0"
    # When false, Graph API httpx skips TLS verify (local AV/proxy MITM only)
    meta_http_verify: bool = True

    # Shared secret for /internal/* routes; empty disables them entirely.
    voice_internal_token: str = ""

    log_level: str = "INFO"
    host: str = "0.0.0.0"
    # Separate from Core MCP PORT=8020
    port: int = Field(default=8021, validation_alias="META_MCP_PORT")


@lru_cache
def get_settings() -> Settings:
    return Settings()
