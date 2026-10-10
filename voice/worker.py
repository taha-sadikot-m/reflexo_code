"""LiveKit Agents worker for WhatsApp voice rooms (`wa-call-*`)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from voice.config import get_voice_settings
from voice.session import start_voice_session
from voice.userdata import UserData

logger = logging.getLogger("voice.worker")


async def _ensure_infra() -> None:
    settings = get_voice_settings()
    from mcp_servers.meta_mcp.auth.tokens import init_fernet
    from shared.db import init_pool
    from shared.redis_client import init_redis

    if settings.encryption_key:
        init_fernet(settings.encryption_key)
    if settings.database_url:
        await init_pool(settings.database_url, application_name="voice-worker")
    if settings.redis_url:
        await init_redis(settings.redis_url)


def _userdata_from_room(room: object) -> UserData:
    return _userdata_from_json(getattr(room, "metadata", None) or "")


def _userdata_from_json(raw: str) -> UserData:
    data: dict = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("VOICE_WORKER metadata is not JSON")
    return UserData.from_dict(data)


async def _userdata_from_context(ctx, metadata_changed: asyncio.Event) -> UserData:
    """Load call context despite LiveKit's asynchronous room metadata sync."""
    room_name = str(getattr(ctx.room, "name", "") or "")
    derived_call_id = (
        room_name.removeprefix("wa-call-")
        if room_name.startswith("wa-call-")
        else ""
    )

    def candidates() -> list[str]:
        raw_values = [
            str(getattr(getattr(ctx, "job", None), "metadata", "") or ""),
            str(getattr(ctx.room, "metadata", "") or ""),
        ]
        participants = getattr(ctx.room, "remote_participants", {}) or {}
        for participant in participants.values():
            raw_values.append(str(getattr(participant, "metadata", "") or ""))
        return raw_values

    deadline = asyncio.get_running_loop().time() + 8.0
    while True:
        for raw in candidates():
            userdata = _userdata_from_json(raw)
            if not userdata.call_id and derived_call_id:
                userdata.call_id = derived_call_id
            if userdata.business_id and userdata.call_id:
                return userdata

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return UserData.from_dict({"call_id": derived_call_id})
        metadata_changed.clear()
        try:
            await asyncio.wait_for(metadata_changed.wait(), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            pass


async def entrypoint(ctx) -> None:
    from livekit.agents import AutoSubscribe

    await _ensure_infra()
    metadata_changed = asyncio.Event()

    @ctx.room.on("room_metadata_changed")
    def on_room_metadata_changed(_old: str, _new: str) -> None:
        metadata_changed.set()

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    userdata = await _userdata_from_context(ctx, metadata_changed)
    if not userdata.business_id or not userdata.call_id:
        logger.error(
            "VOICE_WORKER missing business_id/call_id after metadata fallbacks room=%s",
            getattr(ctx.room, "name", ""),
        )
        return
    from voice.telemetry import configure_voice_tracing, tag_voice_trace

    configure_voice_tracing()
    tag_voice_trace(call_id=userdata.call_id, business_id=userdata.business_id)
    await start_voice_session(
        ctx,
        userdata,
        prewarmed_vad=getattr(ctx, "proc", None).userdata.get("vad")
        if getattr(ctx, "proc", None)
        else None,
    )


async def request_fnc(req) -> None:
    name = getattr(getattr(req, "room", None), "name", "") or ""
    if str(name).startswith("wa-call-"):
        await req.accept()
        return
    await req.reject()


def _use_windows_selector_event_loop() -> None:
    """Make this LiveKit job process create a Selector loop.

    LiveKit calls ``asyncio.new_event_loop()`` after ``prewarm()`` returns.
    On Windows that defaults to ``ProactorEventLoop``, which psycopg async
    rejects, so the voice reply pipeline cannot open its Postgres pool.
    """
    if sys.platform != "win32":
        return
    import multiprocessing
    import selectors

    if multiprocessing.current_process().name != "job_proc":
        return

    def _new_event_loop() -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.new_event_loop = _new_event_loop


def prewarm(proc) -> None:
    """Load CPU-bound voice resources before a call is assigned."""
    _use_windows_selector_event_loop()
    from livekit.plugins import silero

    proc.userdata["vad"] = silero.VAD.load()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings = get_voice_settings()
    if not settings.is_configured():
        logger.error(
            "Voice worker missing LIVEKIT_URL / LIVEKIT_API_KEY / "
            "LIVEKIT_API_SECRET / DEEPGRAM_API_KEY"
        )
        sys.exit(1)

    from livekit.agents import JobExecutorType, WorkerOptions, cli

    from voice.telemetry import configure_voice_tracing

    configure_voice_tracing()
    kwargs = {
        "entrypoint_fnc": entrypoint,
        "request_fnc": request_fnc,
        "prewarm_fnc": prewarm,
        # asyncpg and redis clients are event-loop-bound. A process per job
        # prevents one call from reusing another job thread's closed loop.
        "job_executor_type": JobExecutorType.PROCESS,
        "num_idle_processes": min(
            3,
            settings.voice_max_calls_per_business,
            settings.voice_max_calls_global,
        ),
    }
    try:
        options = WorkerOptions(**kwargs)
    except TypeError:
        options = WorkerOptions(entrypoint_fnc=entrypoint)
    cli.run_app(options)


if __name__ == "__main__":
    main()
