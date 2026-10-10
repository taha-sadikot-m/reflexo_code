"""Confidence-based non-Meta outbound policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SendAction = Literal["auto_send", "send_and_notify", "queued"]


@dataclass(frozen=True, slots=True)
class ConfidenceDecision:
    action: SendAction
    should_send: bool
    notify_owner: bool


def decide_confidence(
    score: float,
    config: dict[str, Any] | None = None,
) -> ConfidenceDecision:
    value = max(0.0, min(float(score), 1.0))
    settings = config or {}
    mode = str(settings.get("send_mode") or "threshold")
    if mode == "always_auto":
        return ConfidenceDecision("auto_send", True, bool(settings.get("notify_on_auto_send")))
    if mode == "always_approve":
        return ConfidenceDecision("queued", False, True)
    auto = float(settings.get("auto_send_threshold", 0.85))
    approval = float(settings.get("approval_threshold", 0.60))
    if not 0 <= approval <= auto <= 1:
        raise ValueError("confidence thresholds must satisfy 0 <= approval <= auto <= 1")
    if value >= auto:
        notify = bool(settings.get("notify_on_auto_send", False))
        return ConfidenceDecision("auto_send", True, notify)
    if value >= approval:
        return ConfidenceDecision("send_and_notify", True, True)
    return ConfidenceDecision("queued", False, True)
