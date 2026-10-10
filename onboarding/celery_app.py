"""Celery app for onboarding reminders (Redis broker)."""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
# Celery expects redis:// — Upstash rediss:// works with SSL
BROKER_URL = os.getenv("CELERY_BROKER_URL") or REDIS_URL
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND") or BROKER_URL

celery_app = Celery("reflexo", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "check-stale-onboarding-hourly": {
            "task": "check_stale_onboarding",
            "schedule": crontab(minute=0),  # every hour
        },
        "analyze-workflow-patterns-every-6-hours": {
            "task": "analyze_workflow_patterns",
            "schedule": crontab(minute=15, hour="*/6"),
        },
        "sync-whatsapp-templates-daily": {
            "task": "sync_whatsapp_templates_daily",
            "schedule": crontab(minute=30, hour=2),
        },
        "renew-channel-subscriptions-hourly": {
            "task": "renew_channel_subscriptions",
            "schedule": crontab(minute=5),
        },
    },
    imports=(
        "onboarding.reminders",
        "agent.memory_tasks",
        "workflow_builder.pattern_analyzer",
        "onboarding.template_sync",
        "channel_framework.tasks",
    ),
)

# Alias used by `celery -A onboarding.celery_app worker`
app = celery_app
