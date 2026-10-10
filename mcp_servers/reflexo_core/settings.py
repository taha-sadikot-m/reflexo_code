from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    redis_url: str = "redis://localhost:6379"
    google_api_key: str = ""
    encryption_key: str = ""
    log_level: str = "INFO"
    meta_mcp_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None
    port: int = 8020

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
