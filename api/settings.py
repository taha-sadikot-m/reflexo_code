"""Signup API settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = ""
    redis_url: str = "redis://localhost:6379"
    encryption_key: str = ""

    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_api_version: str = "v22.0"
    meta_oauth_redirect_uri: str = Field(
        default="http://127.0.0.1:8030/api/oauth/callback",
        validation_alias="META_OAUTH_REDIRECT_URI",
    )
    meta_oauth_success_url: str = Field(
        default="https://reflexo.ai/onboarding/success",
        validation_alias="META_OAUTH_SUCCESS_URL",
    )
    meta_oauth_error_url: str = Field(
        default="https://reflexo.ai/onboarding/error",
        validation_alias="META_OAUTH_ERROR_URL",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8030, validation_alias="API_PORT")
    log_level: str = "INFO"
    simulator_api_key: str = ""

    database_url_service_role: str = Field(
        default="",
        validation_alias="DATABASE_URL_SERVICE_ROLE",
    )
    admin_jwt_secret: str = Field(default="", validation_alias="ADMIN_JWT_SECRET")
    admin_jwt_expiry_hours: int = Field(default=8, validation_alias="ADMIN_JWT_EXPIRY_HOURS")
    admin_frontend_origin: str = Field(
        default="http://localhost:3000",
        validation_alias="ADMIN_FRONTEND_ORIGIN",
    )
    widget_allowed_origins: str = Field(
        default="*",
        validation_alias="WIDGET_ALLOWED_ORIGINS",
    )
    widget_rate_limit_per_min: int = Field(default=30, validation_alias="WIDGET_RATE_LIMIT_PER_MIN")
    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    mcp_registry_url: str = Field(
        default="https://registry.modelcontextprotocol.io",
        validation_alias="MCP_REGISTRY_URL",
    )
    mcp_registry_cache_seconds: int = Field(
        default=3600, validation_alias="MCP_REGISTRY_CACHE_SECONDS"
    )
    mcp_oauth_base_url: str = Field(
        default="http://127.0.0.1:8030", validation_alias="MCP_OAUTH_BASE_URL"
    )
    temporal_address: str = Field(default="localhost:7233", validation_alias="TEMPORAL_ADDRESS")
    temporal_namespace: str = Field(default="default", validation_alias="TEMPORAL_NAMESPACE")
    temporal_task_queue: str = Field(
        default="reflexo-workflows", validation_alias="TEMPORAL_TASK_QUEUE"
    )


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()
