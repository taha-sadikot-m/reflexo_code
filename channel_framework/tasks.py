"""Celery tasks for Channel Framework maintenance."""

from onboarding.celery_app import celery_app


@celery_app.task(name="renew_channel_subscriptions")
def renew_channel_subscriptions_task() -> dict[str, int]:
    from agent.asyncio_compat import run_async
    from onboarding.reminders import _ensure_runtime
    from channel_framework.renewal import renew_expiring_subscriptions

    async def run() -> dict[str, int]:
        await _ensure_runtime()
        return await renew_expiring_subscriptions()

    return run_async(run())
