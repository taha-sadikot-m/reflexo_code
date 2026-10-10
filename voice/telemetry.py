"""Optional OpenTelemetry export from LiveKit Agents to Langfuse."""

from __future__ import annotations

import base64
import logging
from typing import Any

from voice.config import get_voice_settings

logger = logging.getLogger(__name__)

_provider: Any = None


def configure_voice_tracing() -> None:
    global _provider
    settings = get_voice_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning(
            "VOICE_OTEL skipped — LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"
        )
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from livekit.agents.telemetry import set_tracer_provider
    except Exception as exc:
        logger.warning("Voice Langfuse tracing unavailable: %s", exc)
        return

    host = (settings.langfuse_host or "https://cloud.langfuse.com").rstrip("/")
    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {token}"},
    )
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: "reflexo-voice"}),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    set_tracer_provider(provider, metadata={"service": "reflexo-voice"})
    _provider = provider
    logger.info("VOICE_OTEL Langfuse exporter configured host=%s", host)


def tag_voice_trace(*, call_id: str, business_id: str) -> None:
    """Attach call identity to subsequent spans in this worker process."""
    if _provider is None:
        return
    try:
        from livekit.agents.telemetry.traces import _MetadataSpanProcessor
    except Exception:
        return
    _provider.add_span_processor(
        _MetadataSpanProcessor(
            {
                "call_id": call_id,
                "business_id": business_id,
                "service": "reflexo-voice",
            }
        )
    )
    logger.info("VOICE_OTEL tagged call_id=%s", call_id)
