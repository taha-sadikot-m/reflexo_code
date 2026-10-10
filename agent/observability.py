"""Optional Langfuse tracing and token metadata helpers (MVP Stage 3 / §13.2)."""

from __future__ import annotations

import logging
from typing import Any

from agent.settings import get_agent_settings

logger = logging.getLogger(__name__)

# Gemini 2.5 Flash list prices (USD / 1M tokens) for estimated_cost_usd metadata.
_INPUT_USD_PER_M = 0.15
_OUTPUT_USD_PER_M = 0.60


def get_langfuse_handler(
    trace_name: str = "agent_turn",
    business_id: str = "",
    session_id: str = "",
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Langfuse LangChain CallbackHandler, or None when keys/package missing."""
    settings = get_agent_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    meta = {
        "business_id": business_id,
        **(metadata or {}),
    }
    tags = ["reflexo"]
    if business_id:
        tags.append(f"business:{business_id}")
    specialist = meta.get("specialist")
    if specialist:
        tags.append(f"specialist:{specialist}")
    route_method = meta.get("route_method")
    if route_method:
        tags.append(f"route:{route_method}")

    try:
        try:
            from langfuse.langchain import CallbackHandler  # type: ignore
        except ImportError:
            from langfuse.callback import CallbackHandler  # type: ignore
    except Exception as exc:
        logger.warning("Langfuse callbacks unavailable: %s", exc)
        return None

    kwargs: dict[str, Any] = {
        "public_key": settings.langfuse_public_key,
        "secret_key": settings.langfuse_secret_key,
        "host": settings.langfuse_host or None,
        "trace_name": trace_name,
        "session_id": session_id or None,
        "user_id": user_id or business_id or None,
        "metadata": meta,
        "tags": tags,
    }
    try:
        return CallbackHandler(**kwargs)
    except TypeError:
        try:
            handler = CallbackHandler(
                public_key=kwargs["public_key"],
                secret_key=kwargs["secret_key"],
                host=kwargs["host"],
            )
        except Exception as exc:
            logger.warning("Langfuse CallbackHandler failed: %s", exc)
            return None
        for attr, value in (
            ("session_id", kwargs["session_id"]),
            ("user_id", kwargs["user_id"]),
            ("metadata", meta),
            ("tags", tags),
            ("trace_name", trace_name),
        ):
            try:
                setattr(handler, attr, value)
            except Exception:
                pass
        return handler
    except Exception as exc:
        logger.warning("Langfuse CallbackHandler failed: %s", exc)
        return None


def build_langfuse_callbacks(
    *,
    trace_name: str = "agent_turn",
    business_id: str = "",
    session_id: str = "",
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[Any]:
    """
    Return Langfuse LangChain callbacks when keys are configured.

    No-op (empty list) when unset or when the langfuse package is missing.
    """
    handler = get_langfuse_handler(
        trace_name=trace_name,
        business_id=business_id,
        session_id=session_id,
        user_id=user_id,
        metadata=metadata,
    )
    return [handler] if handler is not None else []


def extract_token_usage(messages: list[Any]) -> dict[str, int] | None:
    """Sum prompt/completion/total tokens from AIMessage usage_metadata if present."""
    prompt = 0
    completion = 0
    total = 0
    found = False

    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if not usage and isinstance(msg, dict):
            usage = msg.get("usage_metadata")
        if not usage:
            # Some providers put usage on response_metadata
            meta = getattr(msg, "response_metadata", None) or {}
            if isinstance(meta, dict):
                usage = meta.get("usage_metadata") or meta.get("token_usage") or meta.get("usage")
        if not usage:
            continue
        if isinstance(usage, dict):
            pi = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            po = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            pt = usage.get("total_tokens") or (int(pi) + int(po))
        else:
            pi = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
            po = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0) or 0
            pt = getattr(usage, "total_tokens", None) or (int(pi) + int(po))
        prompt += int(pi or 0)
        completion += int(po or 0)
        total += int(pt or 0)
        found = True

    if not found:
        return None
    return {"prompt": prompt, "completion": completion, "total": total}


def merge_token_usage(*parts: dict[str, int] | None) -> dict[str, int] | None:
    merged = {"prompt": 0, "completion": 0, "total": 0}
    found = False
    for part in parts:
        if not part:
            continue
        found = True
        merged["prompt"] += int(part.get("prompt") or 0)
        merged["completion"] += int(part.get("completion") or 0)
        merged["total"] += int(part.get("total") or 0)
    return merged if found else None


def estimate_cost_usd(usage: dict[str, int] | None) -> float | None:
    if not usage:
        return None
    inp = int(usage.get("prompt") or 0)
    out = int(usage.get("completion") or 0)
    return (inp * _INPUT_USD_PER_M + out * _OUTPUT_USD_PER_M) / 1_000_000
