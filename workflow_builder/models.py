"""Pydantic schema for AI-built Temporal workflows."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StepType(str, Enum):
    CALL_TOOL = "call_tool"
    AGGREGATE_EVENTS = "aggregate_events"
    DELAY = "delay"
    WAIT_FOR_EVENT = "wait_for_event"
    CONDITION = "condition"
    PARALLEL = "parallel"
    SEND_NOTIFICATION = "send_notification"


class WorkflowStep(BaseModel):
    name: str = Field(description="Short step name, e.g. 'count_comments'")
    type: StepType
    tool: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    event_filter: dict[str, Any] = Field(default_factory=dict)
    threshold: int | None = None
    window_seconds: int | None = None
    delay_seconds: int | None = None
    delay_until: str | None = None
    wait_event_type: str | None = None
    wait_timeout_seconds: int | None = None
    on_timeout_step: str | None = None
    expression: str | None = None
    on_true_step: str | None = None
    on_false_step: str | None = None
    children: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    retry_count: int = 2
    timeout_seconds: int = 30


class WorkflowDefinition(BaseModel):
    name: str
    description: str = ""
    trigger_type: str
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    instance_key: str | None = None
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[WorkflowStep]
    response_template: str = ""
    max_duration_seconds: int = 86400 * 7
    continue_as_new: bool = False
    requires_owner_confirmation: bool = False
    max_executions_per_day: int = 1000
    nl_description: str = ""


VALID_TRIGGERS = {
    "whatsapp_message",
    "instagram_dm",
    "instagram_comment",
    "facebook_message",
    "facebook_comment",
    "email_received",
    "slack_message",
    "voice_call",
    "payment_received",
    "payment_failed",
    "order_created",
    "schedule",
    "manual",
}

CHANNEL_TO_TRIGGER = {
    "whatsapp": "whatsapp_message",
    "instagram_dm": "instagram_dm",
    "instagram_comment": "instagram_comment",
    "facebook_messenger": "facebook_message",
    "facebook_message": "facebook_message",
    "facebook_comment": "facebook_comment",
    "email": "email_received",
    "gmail": "email_received",
    "outlook": "email_received",
    "slack": "slack_message",
    "voice": "voice_call",
    "sms": "whatsapp_message",
}

TEMPLATE_PREFIXES = (
    "message.",
    "customer.",
    "channel",
    "business.",
    "result.",
    "aggregate_count",
    "event.",
    "now",
    "today",
    "order.",
)
