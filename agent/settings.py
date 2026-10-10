"""Agent worker settings from environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _ensure_mcp_path(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/mcp"):
        return u
    return f"{u}/mcp"


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str
    redis_url: str = "redis://localhost:6379"
    google_api_key: str = ""
    log_level: str = "INFO"
    agent_log_file: str = "logs/agent-worker.log"
    agent_log_max_bytes: int = 10_000_000
    agent_log_backup_count: int = 5

    reflexo_core_mcp_url: str = "http://127.0.0.1:8020/mcp"
    meta_mcp_url: str = "http://127.0.0.1:8021"
    meta_mcp_mcp_url: str | None = None
    channel_framework_mcp_url: str = "http://127.0.0.1:8022/mcp"

    agent_model: str = "gemini-2.5-flash"
    agent_temperature: float = 0.3
    agent_max_tokens: int = 1024
    # Stage 1: replace customer/owner single-agent. Set false to roll back.
    multi_agent_enabled: bool = True
    # Stage 2: input/output rails. Set false to skip injection/PII/price checks.
    guardrails_enabled: bool = True
    guardrails_llm: bool = False
    guardrails_config_dir: str = "guardrails"

    database_pool_size: int = 10

    # Observability (optional — §13.2)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL", "langfuse_host"),
    )
    # Stage 3 daily LLM token cap per business (Redis). T3.5 tests override this.
    token_daily_budget: int = 500_000
    # Stage 4: Redis semantic response cache (customer text only).
    semantic_cache_enabled: bool = True
    # Cosine floor for a cache hit. Spec default 0.92 (MVP §1.9); lower it to
    # trade precision for paraphrase recall.
    semantic_cache_threshold: float = 0.92
    # Stage 6: semantic customer memory. Retrieval fails open when Qdrant is
    # unavailable so channel delivery never depends on the memory sidecar.
    mem0_enabled: bool = True
    qdrant_host: str = "127.0.0.1"
    qdrant_port: int = 6333
    qdrant_api_key: str = ""
    mem0_collection: str = "reflexo_memories"
    mem0_search_limit: int = 5

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "reflexo-workflows"

    @property
    def core_mcp_url(self) -> str:
        return _ensure_mcp_path(self.reflexo_core_mcp_url)

    @property
    def meta_mcp_tools_url(self) -> str:
        if self.meta_mcp_mcp_url:
            return _ensure_mcp_path(self.meta_mcp_mcp_url)
        return _ensure_mcp_path(self.meta_mcp_url)

    @property
    def channel_mcp_tools_url(self) -> str:
        return _ensure_mcp_path(self.channel_framework_mcp_url)


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
