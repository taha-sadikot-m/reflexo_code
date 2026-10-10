"""Voice pipeline settings from environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = ""
    redis_url: str = "redis://localhost:6379"
    google_api_key: str = ""
    log_level: str = "INFO"

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    deepgram_api_key: str = ""

    voice_max_calls_per_business: int = 5
    voice_max_calls_global: int = 20

    voice_llm_model: str = Field(
        default="gemini-2.5-flash-lite",
        validation_alias="VOICE_LLM_MODEL",
    )
    voice_llm_fallback_model: str = "gemini-2.5-flash"
    voice_llm_temperature: float = 0.2
    voice_llm_max_output_tokens: int = 512
    voice_llm_thinking_budget: int = 0
    # Google Gemini rejects manually supplied deadlines below 10 seconds.
    voice_llm_attempt_timeout: float = 10.0
    voice_llm_max_retry_per_model: int = 0
    voice_endpointing_min_delay: float = 0.6
    voice_endpointing_max_delay: float = 2.0
    voice_preemptive_tts: bool = False
    voice_whatsapp_input_timeout: int = Field(
        default=300,
        validation_alias="VOICE_WHATSAPP_INPUT_TIMEOUT",
    )
    encryption_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )

    stt_model: str = Field(
        default="nova-2",
        validation_alias=AliasChoices("STT_MODEL", "DEEPGRAM_STT_MODEL"),
    )
    stt_language: str = "multi"
    tts_model: str = Field(
        default="aura-2-asteria-en",
        validation_alias=AliasChoices("TTS_MODEL", "DEEPGRAM_TTS_MODEL"),
    )

    @property
    def agent_model(self) -> str:
        """Compatibility name used by the voice session."""
        return self.voice_llm_model

    def is_configured(self) -> bool:
        return bool(
            self.livekit_url
            and self.livekit_api_key
            and self.livekit_api_secret
            and self.deepgram_api_key
        )


@lru_cache
def get_voice_settings() -> VoiceSettings:
    return VoiceSettings()
