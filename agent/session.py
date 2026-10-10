"""Session lifecycle — main entry for inbound messages."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import psycopg
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphInterrupt, GraphRecursionError

from agent.confidence import (
    LOW_CONFIDENCE_CUSTOMER_TEXT,
    calculate_confidence,
    extract_search_results_from_tool_payloads,
    maybe_hedge_response,
    should_force_low_confidence_escalation,
)
from agent.config import (
    AGENT_CONFIG,
    is_guardrails_enabled,
    is_multi_agent_enabled,
    is_semantic_cache_enabled,
)
from agent.error_handling import (
    RECURSION_FALLBACK_MESSAGE,
    notify_owner_of_outage,
    queue_for_retry,
)
from agent.factory import create_agent_for_session
from agent.guardrails import INJECTION_BLOCK_REPLY, check_input_rails, check_output_rails
from agent.history_repair import (
    find_orphaned_tool_calls,
    is_invalid_chat_history_error,
    synthetic_tool_messages_for_orphans,
)
from agent.infrastructure import (
    clear_thread_checkpoints,
    ensure_store,
    get_checkpointer,
    reset_checkpointer,
)
from agent.stage_timings import StageTimings
from agent.memory import (
    get_customer_memories,
    load_cross_channel_context,
    load_customer_memory,
    load_owner_cross_channel_context,
)
from agent.pipeline import (
    SendFailedError,
    check_bot_detection,
    check_message_limit,
    claim_inbound_message,
    get_or_create_conversation,
    handle_limit_reached,
    llm_already_sent_outbound,
    load_business_config,
    load_customer_profile,
    log_inbound_message,
    log_messages,
    mark_message_answered,
    message_already_answered,
    post_process,
    release_inbound_message,
    resolve_identity,
    send_response,
)
from agent.semantic_cache import (
    cache_eligible,
    embed_for_cache,
    get_cached_response,
    set_cached_response,
)
from agent.settings import get_agent_settings
from agent.token_budget import (
    BUDGET_EXHAUSTED_MESSAGE,
    check_budget,
    record_usage,
)

logger = logging.getLogger(__name__)


def resolve_session_type(*, is_owner: bool, onboarding_completed: bool) -> str:
    """Map identity + business flags to agent session_type."""
    if is_owner and not onboarding_completed:
        return "onboarding"
    if is_owner:
        return "owner"
    return "customer"


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif hasattr(block, "get") and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts)
    return str(content)


def _is_ai_message(msg: Any) -> bool:
    msg_type = getattr(msg, "type", None)
    if msg_type in ("ai", "assistant"):
        return True
    return msg.__class__.__name__ in ("AIMessage", "AIMessageChunk")


def _extract_last_ai_text(messages: list[Any]) -> str:
    """Prefer the last AI/assistant message with non-empty text (skip ToolMessages)."""
    for msg in reversed(messages):
        if not _is_ai_message(msg):
            continue
        text = _message_content_to_text(getattr(msg, "content", ""))
        if text.strip():
            return text
    return ""


def _tool_result_succeeded(entry: dict[str, Any]) -> bool:
    if entry.get("error") or entry.get("error_type"):
        return False
    return True


def _synthesize_tool_confirmation(tool_results: list[dict[str, Any]]) -> str:
    """Build a short user-facing confirmation when the model returned no final text."""
    for entry in reversed(tool_results):
        if not _tool_result_succeeded(entry):
            continue
        name = str(entry.get("tool_name") or "")
        if name == "create_calendar_event" and (entry.get("event_id") or entry.get("id")):
            summary = entry.get("summary") or "your event"
            start = entry.get("start") or ""
            text = f'Done — I created the calendar event "{summary}".'
            if start:
                text = f"{text} It starts at {start}."
            return text
        if name == "send_email" and entry.get("sent"):
            return "Done — I sent the email."
        if name == "get_calendar_events" and "events" in entry:
            n = len(entry.get("events") or [])
            return f"I found {n} calendar event(s)."
        if name == "check_availability" and "available_slots" in entry:
            n = len(entry.get("available_slots") or [])
            return f"I found {n} available slot(s)."
        if name and name != "unknown":
            if any(
                k in entry
                for k in ("event_id", "sent", "id", "events", "available_slots", "link")
            ):
                return f"Done — I completed {name.replace('_', ' ')}."
    return ""


def _resolve_response_text(
    messages: list[Any],
    tool_results: list[dict[str, Any]],
    tool_calls_made: list[str],
) -> str:
    text = _extract_last_ai_text(messages)
    if text.strip():
        return text
    synthesized = _synthesize_tool_confirmation(tool_results)
    if synthesized.strip():
        logger.info(
            "Synthesized reply from tool results tools=%s",
            tool_calls_made,
        )
        return synthesized
    logger.warning(
        "Empty agent reply after invoke; tools=%s",
        tool_calls_made,
    )
    return "Sorry, I could not generate a reply just now. Please try again."


def _slice_current_turn(messages: list[Any]) -> list[Any]:
    """Return messages from the last human/user message to the end (this turn)."""
    if not messages:
        return []
    last_human = 0
    for i, msg in enumerate(messages):
        msg_type = getattr(msg, "type", None)
        name = msg.__class__.__name__
        if msg_type in ("human", "user") or name == "HumanMessage":
            last_human = i
    return messages[last_human:]


def _collect_tool_metadata(messages: list[Any]) -> tuple[list[str], list[dict[str, Any]]]:
    tool_calls_made: list[str] = []
    tool_results: list[dict[str, Any]] = []

    for msg in messages:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    tool_calls_made.append(name)

        if getattr(msg, "type", None) == "tool" or msg.__class__.__name__ == "ToolMessage":
            name = getattr(msg, "name", None) or "unknown"
            raw = _message_content_to_text(getattr(msg, "content", ""))
            entry: dict[str, Any] = {"tool_name": name}
            try:
                import json

                parsed = json.loads(raw) if raw.startswith("{") or raw.startswith("[") else None
                if isinstance(parsed, dict):
                    entry.update(parsed)
                    if "error" in parsed:
                        entry["error"] = parsed["error"]
                else:
                    entry["raw"] = raw
            except Exception:
                entry["raw"] = raw
            tool_results.append(entry)

    return tool_calls_made, tool_results


def _reply_preview(text: str, limit: int = 80) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


async def _repair_orphaned_checkpoint(agent: Any, config: dict, thread_id: str) -> int:
    """
    Append synthetic ToolMessages for orphaned tool_calls in checkpoint state.

    Returns number of orphans repaired (0 if none / unable).
    """
    try:
        state = await agent.aget_state(config)
    except Exception as exc:
        logger.warning("aget_state failed during history repair thread=%s: %s", thread_id, exc)
        return 0

    values = getattr(state, "values", None) or {}
    messages = list(values.get("messages") or [])
    orphans = find_orphaned_tool_calls(messages)
    if not orphans:
        return 0

    extras = synthetic_tool_messages_for_orphans(orphans)
    try:
        await agent.aupdate_state(config, {"messages": extras})
    except Exception as exc:
        logger.warning(
            "aupdate_state failed during history repair thread=%s: %s",
            thread_id,
            exc,
        )
        return 0

    logger.warning(
        "Repaired %s orphaned tool_call(s) on thread=%s names=%s",
        len(orphans),
        thread_id,
        [n for _, n in orphans],
    )
    return len(orphans)


async def _invoke_agent_with_llm_retries(
    agent: Any,
    message_text: str,
    config: dict,
    *,
    thread_id: str,
    resume: Any | None = None,
) -> Any:
    """
    Honor llm_retry_max / llm_retry_delay_s before treating as outage.

    On INVALID_CHAT_HISTORY: repair orphaned tool_calls once, then clear thread
    checkpoints as last resort — do not burn all LLM retries on corrupt history.
    """
    max_retries = int(AGENT_CONFIG.get("llm_retry_max", 2))
    delay = float(AGENT_CONFIG.get("llm_retry_delay_s", 2.0))
    last_exc: Exception | None = None
    history_repaired = False
    history_cleared = False

    attempt = 0
    while attempt <= max_retries:
        try:
            if resume is not None:
                from langgraph.types import Command

                return await agent.ainvoke(Command(resume=resume), config=config)
            return await agent.ainvoke(
                {"messages": [HumanMessage(content=message_text)]},
                config=config,
            )
        except GraphInterrupt:
            raise
        except psycopg.OperationalError:
            raise
        except GraphRecursionError:
            raise
        except Exception as exc:
            last_exc = exc
            if is_invalid_chat_history_error(exc):
                if not history_repaired:
                    history_repaired = True
                    n = await _repair_orphaned_checkpoint(agent, config, thread_id)
                    if n > 0:
                        # Retry immediately without counting as an LLM flake
                        continue
                if not history_cleared:
                    history_cleared = True
                    await clear_thread_checkpoints(thread_id)
                    logger.warning(
                        "Cleared checkpoints after INVALID_CHAT_HISTORY thread=%s; retrying",
                        thread_id,
                    )
                    continue
                # Repair + clear already tried — fall through to raise
                raise

            logger.warning("LLM/agent invoke failed (attempt %s): %s", attempt + 1, exc)
            if attempt < max_retries:
                await asyncio.sleep(delay)
                attempt += 1
                continue
            raise

    assert last_exc is not None
    raise last_exc


async def handle_incoming_message(
    channel: str,
    external_sender_id: str,
    message_text: str,
    receiving_account_id: str,
    message_type: str = "text",
    media_url: str | None = None,
    media_id: str | None = None,
    media_filename: str | None = None,
    media_mime_type: str | None = None,
    external_message_id: str | None = None,
    *,
    comment_id: str | None = None,
    post_id: str | None = None,
    sender_display_name: str | None = None,
    skip_send: bool = False,
    is_retry: bool = False,
    hitl_reply_id: str | None = None,
    simulator: dict[str, Any] | None = None,
    progress_session_id: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Main entry point for inbound channel messages.

    Returns status dict with response_text, thread_id, latency_ms.
    """
    start_time = time.monotonic()
    stages = StageTimings()
    logger.info(
        "PIPELINE_START channel=%s message_type=%s external_id_present=%s comment=%s",
        channel,
        message_type,
        bool(external_message_id),
        bool(comment_id),
    )
    # media_url: inject into agent context (vision/TTS owned by other pipelines)
    agent_message_text = message_text
    if media_url:
        agent_message_text = (
            f"{message_text}\n\n"
            f"[Customer shared media] url={media_url}"
        ).strip()
    if post_id or (comment_id and channel in ("instagram_comment", "facebook_comment")):
        agent_message_text = (
            f"{agent_message_text}\n\n"
            f"[POST CONTEXT] post_id={post_id or ''} comment_id={comment_id or ''}"
        ).strip()

    # ── Step 1: Identity ──────────────────────────────────────────────────
    if simulator:
        from identity.models import IdentityResult

        business_id = str(simulator.get("business_id") or "")
        is_owner = str(simulator.get("mode") or "customer") == "owner"
        customer_id = None if is_owner else simulator.get("customer_id")
        identity = IdentityResult(
            business_id=business_id,
            customer_id=str(customer_id) if customer_id else None,
            is_owner=is_owner,
            is_new_customer=False,
            customer_name=str(simulator.get("customer_name") or "Simulator"),
            is_bot_suspected=False,
        )
    else:
        identity = await resolve_identity(
            channel=channel,
            external_sender_id=external_sender_id,
            receiving_account_id=receiving_account_id,
            sender_display_name=sender_display_name,
        )
        business_id = identity.business_id
        customer_id = identity.customer_id
        is_owner = identity.is_owner

    stages.mark(
        "identity",
        detail="simulator identity" if simulator else "resolved",
    )
    logger.info(
        "IDENTITY_READY business=%s customer_present=%s owner=%s new_customer=%s",
        identity.business_id,
        bool(identity.customer_id),
        identity.is_owner,
        identity.is_new_customer,
    )

    # Owner YES/NO replies resume the oldest Temporal run waiting for approval.
    # Simulator traffic must never approve a real pending workflow.
    if is_owner and not simulator:
        try:
            from workflow_builder.confirmation import signal_pending_confirmation

            confirmation = await signal_pending_confirmation(
                business_id,
                message_text,
            )
        except Exception:
            logger.warning(
                "WF_CONFIRM signal failed business=%s",
                business_id,
                exc_info=True,
            )
            confirmation = None
        if confirmation:
            approved = bool(confirmation["approved"])
            response = (
                "Approved. The workflow will continue."
                if approved
                else "Cancelled. The workflow will not run its actions."
            )
            outbound_sent = False
            if not skip_send:
                await send_response(
                    channel=channel,
                    recipient_id=external_sender_id,
                    text=response,
                    business_id=business_id,
                    comment_id=comment_id,
                )
                outbound_sent = True
                await mark_message_answered(channel, external_message_id)
            stages.mark(
                "workflow_confirmation",
                detail="approved" if approved else "declined",
            )
            return {
                "status": "workflow_confirmation",
                "response_text": response,
                "thread_id": "",
                "latency_ms": _elapsed_ms(start_time),
                "business_id": business_id,
                "outbound_sent": outbound_sent,
                "outbound": "pipeline_sent" if outbound_sent else "skip_send",
                "workflow_matched": True,
                "route_method": "temporal_confirmation",
                "stage_timings": stages.to_list(),
            }

    from agent.progress import publish_progress

    progress_sid = progress_session_id or external_sender_id
    await publish_progress(identity.business_id, progress_sid, "typing")

    if channel == "whatsapp" and (customer_id or is_owner):
        try:
            from voice.cross_channel import (
                check_cross_channel_route,
                handle_inbound_whatsapp_if_listening,
            )

            if await check_cross_channel_route(
                business_id=business_id,
                is_owner=is_owner,
                customer_id=customer_id,
                sender_external_id=external_sender_id,
            ):
                conversation = await get_or_create_conversation(
                    business_id, customer_id, channel
                )
                bridged = await handle_inbound_whatsapp_if_listening(
                    customer_id=customer_id,
                    is_owner=is_owner,
                    sender_external_id=external_sender_id,
                    business_id=business_id,
                    conversation_id=conversation["conversation_id"],
                    message_text=message_text,
                    message_type=message_type,
                    media_id=media_id,
                    media_url=media_url,
                    filename=media_filename,
                    mime_type=media_mime_type,
                    external_message_id=external_message_id,
                )
                if bridged:
                    bridged["latency_ms"] = _elapsed_ms(start_time)
                    logger.info(
                        "CROSS_CHANNEL_HANDLED owner=%s customer_present=%s call_id=%s",
                        is_owner,
                        bool(customer_id),
                        bridged.get("voice_session_id"),
                    )
                    return bridged
        except Exception:
            logger.exception("Cross-channel intercept failed; continuing normal pipeline")

    # ── Step 1.5: Token budget (before routing / LLM) ───────────────────────
    if not await check_budget(business_id):
        outbound_sent = False
        if not skip_send:
            try:
                await send_response(
                    channel=channel,
                    recipient_id=external_sender_id,
                    text=BUDGET_EXHAUSTED_MESSAGE,
                    business_id=business_id,
                    comment_id=comment_id,
                )
                outbound_sent = True
            except Exception as send_exc:
                logger.error("Budget-exhausted send failed: %s", send_exc)
        return {
            "status": "budget_exhausted",
            "response_text": BUDGET_EXHAUSTED_MESSAGE,
            "thread_id": "",
            "latency_ms": _elapsed_ms(start_time),
            "business_id": business_id,
            "outbound_sent": outbound_sent,
        }

    # ── Step 2: Message limit (customers only) ────────────────────────────
    if not is_owner:
        if not await check_message_limit(business_id):
            await handle_limit_reached(
                business_id,
                channel=channel,
                recipient_id=external_sender_id,
                comment_id=comment_id,
                skip_send=skip_send,
            )
            return {
                "status": "limit_reached",
                "response_text": None,
                "thread_id": "",
                "latency_ms": _elapsed_ms(start_time),
                "business_id": business_id,
            }

    # ── Step 3: Bot detection ─────────────────────────────────────────────
    if not is_owner:
        is_bot = await check_bot_detection(
            business_id,
            external_sender_id,
            channel,
            already_suspected=identity.is_bot_suspected,
        )
        if is_bot:
            return {
                "status": "bot_detected",
                "response_text": None,
                "thread_id": "",
                "latency_ms": _elapsed_ms(start_time),
                "business_id": business_id,
            }

    # ── Step 4: Context ───────────────────────────────────────────────────
    business_config = await load_business_config(business_id)
    if runtime_context:
        business_config = {
            **business_config,
            "_runtime_context": dict(runtime_context),
        }
    conversation = await get_or_create_conversation(
        business_id, customer_id, channel
    )
    conversation_id = conversation["conversation_id"]
    customer_profile = None
    if not is_owner and customer_id:
        customer_profile = await load_customer_profile(business_id, customer_id)
    is_personal_contact = bool(
        business_config.get("smart_number_mode")
        and customer_profile
        and customer_profile.get("contact_type") == "personal"
    )
    if is_personal_contact:
        await log_inbound_message(
            business_id,
            conversation_id,
            channel,
            message_text,
            message_type,
            external_message_id,
            is_owner=False,
        )
        stages.mark(
            "smart_number",
            status="skip",
            detail="personal contact muted",
        )
        await publish_progress(
            business_id,
            progress_sid,
            "done",
            status="personal_contact",
            outbound="none",
            response_text=None,
        )
        return {
            "status": "personal_contact",
            "response_text": None,
            "thread_id": "",
            "latency_ms": _elapsed_ms(start_time),
            "business_id": business_id,
            "conversation_id": conversation_id,
            "outbound": "none",
            "outbound_sent": False,
            "workflow_matched": False,
            "tools_called": [],
            "skip_send": True,
            "specialist": None,
            "route_method": "smart_number",
            "stage_timings": stages.to_list(),
        }

    # Temporal is the only automation engine. Workflow routing happens after
    # identity, budget, quota and bot checks; a matching customer event consumes
    # the turn so the ReAct path cannot produce a duplicate reply.
    from agent.pipeline import emit_ai_workflow_event

    workflow_matched = False
    if not (simulator or {}).get("skip_workflows"):
        workflow_matched = await emit_ai_workflow_event(
            channel=channel,
            business_id=business_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            message_text=message_text,
            is_owner=is_owner,
            extra={
                "comment_id": comment_id,
                "post_id": post_id,
                "external_sender_id": external_sender_id,
                "contact_type": (
                    customer_profile.get("contact_type", "unknown")
                    if customer_profile
                    else "unknown"
                ),
                "smart_number_mode": bool(
                    business_config.get("smart_number_mode")
                ),
                "skip_send": skip_send,
                "simulate_outbound": skip_send,
                "simulator": simulator,
            },
            wait=True,
        )
    stages.mark(
        "workflow",
        status="ok" if workflow_matched else "skip",
        detail="matched; agent skipped" if workflow_matched else "no Temporal match",
    )
    if workflow_matched and not is_owner:
        await log_inbound_message(
            business_id,
            conversation_id,
            channel,
            message_text,
            message_type,
            external_message_id,
            is_owner=False,
        )
        stage_timings = stages.to_list()
        try:
            await publish_progress(
                business_id,
                progress_sid,
                "done",
                status="workflow_consumed",
                outbound="temporal_workflow",
                workflow_matched=True,
                response_text=None,
            )
        except Exception:
            pass
        return {
            "status": "workflow_consumed",
            "response_text": None,
            "thread_id": "",
            "latency_ms": _elapsed_ms(start_time),
            "business_id": business_id,
            "conversation_id": conversation_id,
            "ai_metadata": {
                "workflow_matched": True,
                "stage_timings": stage_timings,
            },
            "outbound": "temporal_workflow",
            "outbound_sent": False,
            "workflow_matched": True,
            "tools_called": [],
            "skip_send": skip_send,
            "specialist": None,
            "route_method": "temporal",
            "stage_timings": stage_timings,
        }

    recent_messages = conversation.get("recent_messages") or []
    cross_channel_messages: list[dict[str, Any]] = []
    if is_owner:
        # Owner/onboarding: same cross-channel injection as customers (other channels).
        cross_channel_messages = await load_owner_cross_channel_context(
            business_id, channel
        )
    elif customer_id:
        # Voice hybrid (§10.4): exclude current channel so WhatsApp text
        # arrives in the voice session context (and vice versa).
        cross_channel_messages = await load_cross_channel_context(
            business_id, customer_id, channel
        )

    session_type = resolve_session_type(
        is_owner=is_owner,
        onboarding_completed=bool(business_config.get("onboarding_completed", False)),
    )
    is_voice = channel in ("whatsapp_voice", "voice")

    guardrails_triggers: list[str] = []
    input_blocked = False
    canned_reply: str | None = None
    if is_guardrails_enabled():
        input_blocked, canned_reply, guardrails_triggers = check_input_rails(
            agent_message_text, session_type
        )

    cache_hit = False
    query_embedding: list[float] | None = None
    use_cache = cache_eligible(
        session_type=session_type,
        message_type=message_type,
        media_url=media_url,
        input_blocked=input_blocked,
        enabled=is_semantic_cache_enabled(),
    )
    # A retry replays identical text, so the cache would answer it at similarity
    # 1.0 and re-send a reply the customer already has. Reads are skipped; the
    # write below still happens once the retry produces a real answer.
    if use_cache and not is_retry:
        try:
            query_embedding = await embed_for_cache(message_text)
            cached = await get_cached_response(
                business_id,
                message_text,
                query_embedding=query_embedding,
            )
            if cached:
                cache_hit = True
                canned_reply = cached
        except Exception as exc:
            logger.warning("Semantic cache lookup failed: %s", exc)

    store = await ensure_store()
    customer_memory = None
    if not is_owner and customer_id:
        customer_memory = await load_customer_memory(
            store, business_id, customer_id, "profile"
        )

    mem0_memories: list[str] = []
    memory_retrieval_ms = 0
    if (
        session_type == "customer"
        and customer_id
        and not input_blocked
        and not cache_hit
    ):
        memory_started = time.monotonic()
        mem0_memories = await get_customer_memories(
            customer_id,
            message_text,
            business_id,
        )
        memory_retrieval_ms = _elapsed_ms(memory_started)
        logger.info(
            "Customer memory retrieval business=%s customer=%s count=%s latency_ms=%s",
            business_id,
            customer_id,
            len(mem0_memories),
            memory_retrieval_ms,
        )

    onboarding_progress = None
    if session_type == "onboarding":
        from onboarding.progress import load_onboarding_progress

        onboarding_progress = await load_onboarding_progress(business_id)

    stages.mark(
        "context",
        detail=f"session={session_type} cache_hit={cache_hit}",
    )

    # ── Step 5: Agent ─────────────────────────────────────────────────────
    checkpointer = await get_checkpointer()
    thread_key = customer_id or "owner"
    thread_id = f"{business_id}:{thread_key}:{channel}"

    agent_kwargs = dict(
        session_type=session_type,
        business_config=business_config,
        customer_profile=customer_profile,
        channel=channel,
        is_voice=is_voice,
        recent_messages=recent_messages,
        cross_channel_messages=cross_channel_messages,
        checkpointer=checkpointer,
        store=store,
        conversation_id=conversation_id,
        customer_memory=customer_memory,
        mem0_memories=mem0_memories,
        onboarding_progress=onboarding_progress,
    )

    from agent.observability import (
        build_langfuse_callbacks,
        estimate_cost_usd,
        extract_token_usage,
        merge_token_usage,
    )

    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": int(AGENT_CONFIG["max_tool_calls"]) * 2 + 1,
    }
    lf_callbacks = build_langfuse_callbacks(
        trace_name="owner_message" if is_owner else "customer_message",
        business_id=business_id,
        session_id=thread_id,
        user_id=customer_id or "owner",
        metadata={
            "channel": channel,
            "session_type": session_type,
        },
    )
    if lf_callbacks:
        config["callbacks"] = lf_callbacks

    response_text = ""
    tool_calls_made: list[str] = []
    tool_results: list[dict[str, Any]] = []
    checkpointer_retried = False
    token_usage: dict[str, int] | None = None
    routing_meta: dict[str, Any] = {}
    use_multi = is_multi_agent_enabled() and session_type in ("customer", "owner")
    hitl_prompt_sent = False
    hitl_required = False
    hitl_approved: bool | None = None
    progress_sid = progress_session_id or external_sender_id

    try:
        while True:
            try:
                if input_blocked:
                    response_text = canned_reply or INJECTION_BLOCK_REPLY
                    routing_meta = {
                        "specialist": None,
                        "route_method": "guardrail",
                        "route_confidence": 1.0,
                    }
                    break
                if cache_hit:
                    response_text = canned_reply or ""
                    routing_meta = {
                        "specialist": None,
                        "route_method": "cache",
                        "route_confidence": 1.0,
                    }
                    break
                if use_multi and session_type == "customer":
                    from agent.router import route_customer_message
                    from agent.specialist_runner import run_specialist

                    route = await route_customer_message(
                        agent_message_text,
                        channel,
                        message_type,
                        invoke_config=config,
                    )
                    spec_callbacks = build_langfuse_callbacks(
                        trace_name=route["agent"],
                        business_id=business_id,
                        session_id=thread_id,
                        user_id=customer_id,
                        metadata={
                            "channel": channel,
                            "specialist": route["agent"],
                            "route_method": route.get("method"),
                        },
                    )
                    spec_config = dict(config)
                    if spec_callbacks:
                        spec_config["callbacks"] = spec_callbacks
                    spec_out = await run_specialist(
                        route["agent"],
                        agent_message_text,
                        business_config=business_config,
                        customer_profile=customer_profile,
                        channel=channel,
                        is_voice=is_voice,
                        recent_messages=recent_messages,
                        cross_channel_messages=cross_channel_messages,
                        checkpointer=checkpointer,
                        store=store,
                        thread_id=thread_id,
                        conversation_id=conversation_id,
                        customer_memory=customer_memory,
                        mem0_memories=mem0_memories,
                        invoke_config=spec_config,
                    )
                    response_text = spec_out.get("response_text") or ""
                    tool_calls_made = spec_out.get("tool_calls_made") or []
                    tool_results = spec_out.get("tool_results") or []
                    token_usage = merge_token_usage(
                        route.get("token_usage"), spec_out.get("token_usage")
                    )
                    routing_meta = {
                        "specialist": spec_out.get("specialist") or route["agent"],
                        "route_method": route.get("method"),
                        "route_confidence": route.get("confidence"),
                        "handoff_chain": spec_out.get("handoff_chain") or [],
                        "router_latency_ms": route.get("latency_ms"),
                        "initial_specialist": spec_out.get("initial_specialist"),
                    }
                    logger.info(
                        "ROUTE_COMPLETE channel=%s specialist=%s method=%s tools=%s count=%s",
                        channel,
                        routing_meta["specialist"],
                        routing_meta["route_method"],
                        tool_calls_made,
                        len(tool_calls_made),
                    )
                elif use_multi and session_type == "owner":
                    from agent.broker import run_broker_path
                    from agent.confirmation import (
                        HITL_REMINDER_SUFFIX,
                        clear_pending_hitl,
                        confirmation_body,
                        get_pending_hitl,
                        parse_hitl_reply,
                        send_confirmation_prompt,
                        set_pending_hitl,
                    )
                    from agent.progress import publish_progress
                    from agent.specialist_runner import resume_specialist

                    pending = await get_pending_hitl(
                        business_id, channel, external_sender_id
                    )
                    if pending:
                        decision = parse_hitl_reply(
                            agent_message_text, reply_id=hitl_reply_id
                        )
                        if decision is None:
                            preview = str(pending.get("preview") or "that change")
                            response_text = (
                                f"I still need confirmation for: {preview}. "
                                f"{HITL_REMINDER_SUFFIX}"
                            )
                            hitl_required = True
                            routing_meta = {
                                "specialist": pending.get("specialist") or "broker",
                                "route_method": "hitl_pending",
                                "route_confidence": 1.0,
                                "handoff_chain": [],
                            }
                        else:
                            spec_out = await resume_specialist(
                                str(pending.get("specialist") or "knowledge_agent"),
                                approved=decision,
                                business_config=business_config,
                                channel=channel,
                                is_voice=is_voice,
                                recent_messages=recent_messages,
                                cross_channel_messages=cross_channel_messages,
                                checkpointer=checkpointer,
                                store=store,
                                thread_id=str(
                                    pending.get("thread_id")
                                    or f"{thread_id}:spec:{pending.get('specialist')}"
                                ),
                                conversation_id=conversation_id,
                                invoke_config=config,
                            )
                            await clear_pending_hitl(
                                business_id, channel, external_sender_id
                            )
                            hitl_approved = decision
                            if spec_out.get("status") == "confirmation_required":
                                hitl = spec_out.get("hitl") or {}
                                await set_pending_hitl(
                                    business_id,
                                    channel,
                                    external_sender_id,
                                    hitl,
                                )
                                preview = str(hitl.get("preview") or "")
                                await send_confirmation_prompt(
                                    channel=channel,
                                    recipient_id=external_sender_id,
                                    business_id=business_id,
                                    preview=preview,
                                    message=hitl.get("message"),
                                    skip_send=skip_send,
                                )
                                await publish_progress(
                                    business_id,
                                    progress_sid,
                                    "confirmation",
                                    preview=preview,
                                )
                                response_text = confirmation_body(
                                    preview, hitl.get("message")
                                )
                                hitl_prompt_sent = not skip_send
                                hitl_required = True
                                routing_meta = {
                                    "specialist": hitl.get("specialist") or "broker",
                                    "route_method": "hitl",
                                    "route_confidence": 1.0,
                                    "handoff_chain": [],
                                }
                            else:
                                response_text = spec_out.get("response_text") or (
                                    "Cancelled." if not decision else ""
                                )
                                if not decision and not (response_text or "").strip():
                                    response_text = "Cancelled — I did not make that change."
                                tool_calls_made = spec_out.get("tool_calls_made") or []
                                tool_results = spec_out.get("tool_results") or []
                                token_usage = spec_out.get("token_usage")
                                routing_meta = {
                                    "specialist": spec_out.get("specialist")
                                    or pending.get("specialist"),
                                    "route_method": "hitl_resume",
                                    "route_confidence": 1.0,
                                    "handoff_chain": [],
                                }
                    else:
                        broker_out = await run_broker_path(
                            agent_message_text,
                            business_config=business_config,
                            channel=channel,
                            is_voice=is_voice,
                            recent_messages=recent_messages,
                            cross_channel_messages=cross_channel_messages,
                            checkpointer=checkpointer,
                            store=store,
                            thread_id=thread_id,
                            conversation_id=conversation_id,
                            invoke_config=config,
                            recipient_id=external_sender_id,
                            skip_send=skip_send,
                            progress_session_id=progress_sid,
                        )
                        if broker_out.get("status") == "confirmation_required":
                            hitl = broker_out.get("hitl") or {}
                            await set_pending_hitl(
                                business_id, channel, external_sender_id, hitl
                            )
                            preview = str(hitl.get("preview") or "")
                            await send_confirmation_prompt(
                                channel=channel,
                                recipient_id=external_sender_id,
                                business_id=business_id,
                                preview=preview,
                                message=hitl.get("message"),
                                skip_send=skip_send,
                            )
                            await publish_progress(
                                business_id,
                                progress_sid,
                                "confirmation",
                                preview=preview,
                            )
                            response_text = confirmation_body(
                                preview, hitl.get("message")
                            )
                            hitl_prompt_sent = not skip_send
                            hitl_required = True
                            tool_calls_made = broker_out.get("tool_calls_made") or []
                            tool_results = broker_out.get("tool_results") or []
                            token_usage = broker_out.get("token_usage")
                            routing_meta = {
                                "specialist": hitl.get("specialist") or "broker",
                                "route_method": "hitl",
                                "route_confidence": 1.0,
                                "handoff_chain": [],
                                "delegates": broker_out.get("delegates") or [],
                            }
                        else:
                            response_text = broker_out.get("response_text") or ""
                            tool_calls_made = broker_out.get("tool_calls_made") or []
                            tool_results = broker_out.get("tool_results") or []
                            token_usage = broker_out.get("token_usage")
                            routing_meta = {
                                "specialist": "broker",
                                "route_method": "broker",
                                "route_confidence": 1.0,
                                "handoff_chain": [],
                                "delegates": broker_out.get("delegates") or [],
                            }
                            logger.info(
                                "Owner broker path delegates=%s tools=%s",
                                routing_meta["delegates"],
                                tool_calls_made,
                            )
                else:
                    agent = await create_agent_for_session(**agent_kwargs)
                    result = await _invoke_agent_with_llm_retries(
                        agent, agent_message_text, config, thread_id=thread_id
                    )
                    messages = result.get("messages") or []
                    turn_messages = _slice_current_turn(messages)
                    tool_calls_made, tool_results = _collect_tool_metadata(turn_messages)
                    response_text = _resolve_response_text(
                        turn_messages, tool_results, tool_calls_made
                    )
                    token_usage = extract_token_usage(turn_messages)
                    routing_meta = {"specialist": session_type, "route_method": "legacy"}
                break
            except psycopg.OperationalError as exc:
                if checkpointer_retried:
                    raise
                checkpointer_retried = True
                logger.warning(
                    "Checkpointer connection lost (thread=%s), resetting pool: %s",
                    thread_id,
                    exc,
                )
                checkpointer = await reset_checkpointer()
                agent_kwargs["checkpointer"] = checkpointer
    except GraphRecursionError:
        logger.error("Recursion limit hit thread=%s", thread_id)
        response_text = RECURSION_FALLBACK_MESSAGE
        tool_calls_made = ["escalate_to_owner"]
        try:
            from agent.tools import call_mcp_tool

            await call_mcp_tool(
                "core",
                "escalate_to_owner",
                {
                    "business_id": business_id,
                    "reason": "Agent hit recursion limit — possible tool loop",
                    "customer_id": customer_id,
                    "conversation_id": conversation_id,
                    "priority": "high",
                    "channel": channel,
                },
            )
        except Exception as exc:
            logger.warning("escalate_to_owner after recursion failed: %s", exc)
    except Exception as exc:
        logger.exception("Agent invoke failed: %s", exc)
        # A bare TimeoutError stringifies to "", which would surface as a blank
        # reason, so fall back to the exception type.
        reason = str(exc).strip() or type(exc).__name__
        stages.mark("agent", status="error", detail=reason[:200])
        # Lab traffic must never replay through the live path or page the owner.
        if not (skip_send or simulator):
            await queue_for_retry(
                business_id=business_id,
                customer_id=customer_id,
                channel=channel,
                message_text=message_text,
                error=reason,
                external_sender_id=external_sender_id,
                receiving_account_id=receiving_account_id,
                message_id=external_message_id,
                comment_id=comment_id,
                skip_send=skip_send,
                simulator=simulator,
            )
            await notify_owner_of_outage(business_id, reason)
        # Say nothing to the customer: the retry will deliver the real answer,
        # and only an exhausted retry chain earns a holding message.
        return {
            "status": "error",
            "error": reason,
            "response_text": None,
            "thread_id": thread_id,
            "latency_ms": _elapsed_ms(start_time),
            "business_id": business_id,
            "outbound_sent": False,
            "stage_timings": stages.to_list(),
        }

    stages.mark(
        "agent",
        detail=str((routing_meta or {}).get("specialist") or ""),
    )

    # Empty-text path already resolved in _resolve_response_text (incl. Sorry fallback)

    search_results = extract_search_results_from_tool_payloads(tool_calls_made, tool_results)
    confidence = calculate_confidence(
        tool_calls_made=tool_calls_made,
        tool_results=tool_results,
        search_results=search_results,
        gap_logged="log_gap" in tool_calls_made,
        escalation_triggered="escalate_to_owner" in tool_calls_made,
    )

    # §5.2 post-hoc behavior — customer sessions only (owners/onboarding skip)
    if not is_owner and not input_blocked and not cache_hit:
        if confidence.tier == "MEDIUM":
            response_text = maybe_hedge_response(response_text, confidence)
        elif should_force_low_confidence_escalation(
            confidence, tool_calls_made, tool_results, search_results
        ):
            from agent.tools import call_mcp_tool

            response_text = LOW_CONFIDENCE_CUSTOMER_TEXT
            try:
                if "log_gap" not in tool_calls_made:
                    gap_result = await call_mcp_tool(
                        "core",
                        "log_gap",
                        {
                            "business_id": business_id,
                            "question": message_text,
                            "context": "Auto log_gap on LOW confidence",
                            "conversation_id": conversation_id,
                            "customer_id": customer_id,
                        },
                    )
                    tool_calls_made.append("log_gap")
                    if isinstance(gap_result, dict):
                        tool_results.append({"tool_name": "log_gap", **gap_result})
                await call_mcp_tool(
                    "core",
                    "escalate_to_owner",
                    {
                        "business_id": business_id,
                        "reason": f"Customer asked: {message_text[:300]} — low confidence",
                        "customer_id": customer_id,
                        "conversation_id": conversation_id,
                        "priority": "normal",
                        "channel": channel,
                    },
                )
                tool_calls_made.append("escalate_to_owner")
                confidence = calculate_confidence(
                    tool_calls_made=tool_calls_made,
                    tool_results=tool_results,
                    search_results=search_results,
                    gap_logged=True,
                    escalation_triggered=True,
                )
            except Exception as exc:
                logger.warning("LOW-confidence force escalate failed: %s", exc)

    if is_guardrails_enabled() and not input_blocked:
        response_text, out_triggers = await check_output_rails(
            response_text,
            business_id,
            session_type,
            business_config,
        )
        guardrails_triggers.extend(out_triggers)

    if (
        use_cache
        and not cache_hit
        and not input_blocked
        and confidence.tier != "LOW"
        and (response_text or "").strip()
        and response_text != INJECTION_BLOCK_REPLY
        and response_text != LOW_CONFIDENCE_CUSTOMER_TEXT
    ):
        try:
            await set_cached_response(
                business_id,
                message_text,
                response_text,
                query_embedding=query_embedding,
            )
        except Exception as exc:
            logger.warning("Semantic cache write failed: %s", exc)

    latency_ms = _elapsed_ms(start_time)
    settings = get_agent_settings()
    ai_metadata: dict[str, Any] = {
        "model": settings.agent_model or AGENT_CONFIG["model"],
        "tools_called": tool_calls_made,
        "latency_ms": latency_ms,
        "thread_id": thread_id,
        "confidence": {
            "score": confidence.score,
            "tier": confidence.tier,
            "reasons": confidence.reasons,
        },
        "escalated": "escalate_to_owner" in tool_calls_made,
        "is_owner": is_owner,
        "hedged": confidence.hedge_response,
        "guardrails_triggered": list(guardrails_triggers),
        "memories_injected": len(mem0_memories),
        "memory_retrieval_ms": memory_retrieval_ms,
        "hitl_required": hitl_required,
        "hitl_approved": hitl_approved,
    }
    ai_metadata.update(routing_meta)
    if token_usage:
        ai_metadata["tokens"] = token_usage
        cost = estimate_cost_usd(token_usage)
        if cost is not None:
            ai_metadata["estimated_cost_usd"] = round(cost, 8)
        try:
            await record_usage(business_id, int(token_usage.get("total") or 0))
        except Exception as exc:
            logger.warning("record_usage failed: %s", exc)
    if media_url:
        ai_metadata["media_url"] = media_url

    # ── Step 6: Send ──────────────────────────────────────────────────────
    send_status = "cache_hit" if cache_hit else "responded"
    outbound = "skip_send" if skip_send else "pipeline_sent"
    if hitl_prompt_sent:
        send_status = "hitl_pending"
        outbound = "hitl_prompt"
        await mark_message_answered(channel, external_message_id)
    elif not skip_send:
        if not (response_text or "").strip():
            outbound = "skipped_empty"
            logger.warning("Outbound skipped — empty response_text business=%s", business_id)
        elif await message_already_answered(channel, external_message_id):
            outbound = "already_answered"
            send_status = "already_answered"
            logger.info(
                "Outbound skipped — message_id=%s already answered channel=%s",
                external_message_id,
                channel,
            )
        elif llm_already_sent_outbound(tool_results):
            outbound = "llm_sent_skip"
            logger.info(
                "Skipping pipeline send — LLM already sent outbound this turn tools=%s",
                [
                    e.get("tool_name")
                    for e in tool_results
                    if str(e.get("tool_name") or "").startswith(("send_", "reply_"))
                ],
            )
            await mark_message_answered(channel, external_message_id)
        else:
            try:
                await send_response(
                    channel=channel,
                    recipient_id=external_sender_id,
                    text=response_text,
                    business_id=business_id,
                    comment_id=comment_id,
                )
                outbound = "pipeline_sent"
                await mark_message_answered(channel, external_message_id)
            except SendFailedError as exc:
                logger.error("Outbound send failed: %s", exc)
                send_status = "send_failed"
                outbound = "send_failed"
                ai_metadata["send_error"] = str(exc)
                await queue_for_retry(
                    business_id=business_id,
                    customer_id=customer_id,
                    channel=channel,
                    message_text=message_text,
                    error=f"send_failed: {exc}",
                    external_sender_id=external_sender_id,
                    receiving_account_id=receiving_account_id,
                    message_id=external_message_id,
                    comment_id=comment_id,
                )

    stages.mark(
        "outbound",
        status="error" if outbound == "send_failed" else "ok",
        detail=str(outbound),
    )
    ai_metadata["outbound"] = outbound
    stage_timings = stages.to_list()
    ai_metadata["stage_timings"] = stage_timings
    logger.info(
        "OUTBOUND_COMPLETE channel=%s outbound=%s chars=%s tools=%s",
        channel,
        outbound,
        len(response_text or ""),
        tool_calls_made,
    )

    # ── Step 7: Log ───────────────────────────────────────────────────────
    await log_messages(
        business_id=business_id,
        conversation_id=conversation_id,
        channel=channel,
        incoming_text=message_text,
        incoming_type=message_type,
        incoming_external_id=external_message_id,
        response_text=response_text,
        ai_metadata=ai_metadata,
        is_owner=is_owner,
    )

    # ── Step 8: Post-process ──────────────────────────────────────────────
    await post_process(
        business_id=business_id,
        customer_id=customer_id,
        ai_metadata=ai_metadata,
        tool_calls_made=tool_calls_made,
        conversation_id=conversation_id,
        external_sender_id=external_sender_id,
        channel=channel,
        message_text=message_text,
        response_text=response_text,
        tool_results=tool_results,
        store=store,
    )

    # MULTI_AGENT_SPEC Step 12: once this customer turn has been delivered and
    # logged, publish fact extraction to Celery. The broker call itself runs in
    # a thread and failures are non-fatal to the channel response.
    if (
        not is_owner
        and customer_id
        and not input_blocked
        and outbound in ("pipeline_sent", "llm_sent_skip", "skip_send")
        and (response_text or "").strip()
    ):
        from agent.memory_tasks import enqueue_memory_extraction

        await enqueue_memory_extraction(
            [
                {"role": "user", "content": message_text},
                {"role": "assistant", "content": response_text},
            ],
            customer_id,
            business_id,
        )

    # Logging, post-process and memory extraction run after the reply is decided
    # but still inside the turn, so they belong in the trace instead of showing
    # up as unattributed wall time.
    stages.mark("post", detail="log, post_process, memory")
    stage_timings = stages.to_list()

    try:
        from agent.progress import publish_progress as _pub

        await _pub(
            business_id,
            progress_sid,
            "done",
            status=send_status,
            outbound=outbound,
            hitl_required=hitl_required,
            response_text=response_text,
            specialist=(ai_metadata or {}).get("specialist"),
            route_method=(ai_metadata or {}).get("route_method"),
            route_confidence=(ai_metadata or {}).get("route_confidence")
            or (ai_metadata or {}).get("confidence"),
            tools=tool_calls_made,
            workflow_matched=bool(workflow_matched),
        )
    except Exception:
        pass

    return {
        "status": send_status,
        "response_text": response_text,
        "thread_id": thread_id,
        "latency_ms": latency_ms,
        "business_id": business_id,
        "conversation_id": conversation_id,
        "ai_metadata": ai_metadata,
        "outbound": outbound,
        "workflow_matched": bool(workflow_matched),
        "tools_called": tool_calls_made,
        "skip_send": skip_send,
        "specialist": (ai_metadata or {}).get("specialist"),
        "route_method": (ai_metadata or {}).get("route_method"),
        "stage_timings": stage_timings,
    }


async def handle_redis_event(event: dict[str, Any], *, skip_send: bool = False) -> dict[str, Any] | None:
    """Map a Meta Redis pub/sub payload onto handle_incoming_message."""
    from agent.config import IGNORED_EVENT_TYPES, MESSAGE_EVENT_TYPES
    from agent.pipeline import extract_message_text, receiving_account_from_event

    event_type = event.get("event_type") or ""
    if event_type in IGNORED_EVENT_TYPES:
        return None
    if event_type and event_type not in MESSAGE_EVENT_TYPES and event_type != "retry":
        # Allow events that still look like messages (content + sender),
        # including transcribed voice payloads on whatsapp_voice.
        if not (event.get("sender_id") and event.get("channel")):
            return None

    channel = event.get("channel")
    sender_id = event.get("sender_id")
    if not channel or not sender_id:
        logger.warning("Dropping event missing channel/sender: %s", event_type)
        return None

    skip_send = skip_send or bool(event.get("skip_send"))
    simulator = event.get("simulator") if isinstance(event.get("simulator"), dict) else None
    progress_session_id = str(event.get("session_id") or sender_id)

    from agent.channel_ack import acknowledge_inbound, event_should_ack
    from agent.progress import publish_progress

    biz_hint = str(event.get("business_id") or (simulator or {}).get("business_id") or "")
    if event_should_ack(event) and biz_hint:
        async def _ack_and_progress() -> None:
            try:
                await acknowledge_inbound(
                    business_id=biz_hint,
                    channel=str(channel),
                    message_id=str(event.get("message_id") or "") or None,
                    sender_id=str(sender_id),
                )
                await publish_progress(biz_hint, progress_session_id, "typing")
            except Exception:
                logger.debug(
                    "Inbound acknowledgement failed in background",
                    exc_info=True,
                )

        asyncio.create_task(_ack_and_progress())

    is_retry = event_type == "retry"
    message_id = event.get("message_id") or event.get("comment_id")
    # The claim gate exists to drop Meta's own re-deliveries. A retry is our own
    # replay of a turn we already claimed, so it bypasses the gate and relies on
    # the answered marker to stay silent if the customer already got a reply.
    if not is_retry and not await claim_inbound_message(str(channel), message_id):
        logger.info(
            "Skipping duplicate inbound message_id=%s channel=%s",
            message_id,
            channel,
        )
        return {"status": "duplicate", "response_text": None, "thread_id": "", "latency_ms": 0}

    receiving = receiving_account_from_event(event)
    if not receiving and simulator:
        receiving = str(simulator.get("business_id") or event.get("business_id") or "simulator")
    if not receiving:
        logger.warning("Dropping event missing receiving account: %s", event_type)
        await release_inbound_message(str(channel), message_id)
        return None

    MEDIA_MESSAGE_TYPES = {"image", "document", "audio", "video", "sticker"}
    content = event.get("content") or {}
    hitl_reply_id = None
    media_id = None
    media_filename = None
    media_mime_type = None
    media_url = None
    if isinstance(content, dict):
        interactive = content.get("interactive") or {}
        reply = interactive.get("reply") if isinstance(interactive, dict) else {}
        if isinstance(reply, dict):
            hitl_reply_id = reply.get("id")
        media_id = content.get("media_id") or event.get("media_id")
        media_filename = content.get("filename")
        media_mime_type = content.get("media_type") or content.get("mime_type")
        media_url = content.get("media_url") or content.get("url") or content.get("image_url")
    media_url = media_url or event.get("media_url")
    message_type = str(event.get("message_type") or "text")
    has_media = bool(media_id) or bool(media_url) or message_type in MEDIA_MESSAGE_TYPES
    message_text = extract_message_text(event.get("content"))
    if has_media and isinstance(content, dict) and message_text.strip().startswith("{"):
        caption = content.get("text") or content.get("caption") or ""
        message_text = str(caption or "")
    if not message_text.strip():
        if hitl_reply_id:
            message_text = str(hitl_reply_id)
        elif has_media:
            message_text = ""
        else:
            logger.info("Ignoring non-text / empty content event_type=%s", event_type)
            await release_inbound_message(str(channel), message_id)
            return None

    # Active voice calls have already established and verified the actor. Route
    # their WhatsApp input directly by business + sender, before normal identity,
    # conversation, progress, and broker work.
    if channel == "whatsapp" and biz_hint:
        try:
            from voice.cross_channel import (
                check_cross_channel_sender_route,
                handle_inbound_whatsapp_if_listening,
            )

            active_route = await check_cross_channel_sender_route(
                business_id=biz_hint,
                sender_external_id=str(sender_id),
            )
            if active_route:
                is_owner = active_route.get("actor_type") == "owner"
                active_customer_id = active_route.get("customer_id")
                bridged = await handle_inbound_whatsapp_if_listening(
                    customer_id=str(active_customer_id) if active_customer_id else None,
                    is_owner=is_owner,
                    sender_external_id=str(sender_id),
                    business_id=biz_hint,
                    conversation_id="",
                    message_text=message_text,
                    message_type=message_type,
                    media_id=str(media_id) if media_id else None,
                    media_url=str(media_url) if media_url else None,
                    filename=str(media_filename) if media_filename else None,
                    mime_type=str(media_mime_type) if media_mime_type else None,
                    external_message_id=str(message_id) if message_id else None,
                )
                if bridged:
                    logger.info(
                        "CROSS_CHANNEL_FAST_PATH owner=%s call_id=%s",
                        is_owner,
                        bridged.get("voice_session_id"),
                    )
                    return bridged
        except Exception:
            logger.exception(
                "Cross-channel sender fast path failed; continuing normal pipeline"
            )

    try:
        result = await handle_incoming_message(
            channel=channel,
            external_sender_id=str(sender_id),
            message_text=message_text,
            receiving_account_id=str(receiving),
            message_type=message_type,
            media_url=str(media_url) if media_url else None,
            media_id=str(media_id) if media_id else None,
            media_filename=str(media_filename) if media_filename else None,
            media_mime_type=str(media_mime_type) if media_mime_type else None,
            external_message_id=message_id,
            comment_id=event.get("comment_id"),
            post_id=event.get("post_id"),
            sender_display_name=event.get("sender_username") or event.get("sender_name"),
            skip_send=skip_send,
            is_retry=is_retry,
            hitl_reply_id=str(hitl_reply_id) if hitl_reply_id else None,
            simulator=simulator,
            progress_session_id=progress_session_id,
        )
    except Exception:
        await release_inbound_message(str(channel), message_id)
        raise

    if (
        isinstance(result, dict)
        and result.get("status") == "error"
        and not result.get("outbound_sent")
    ):
        await release_inbound_message(str(channel), message_id)

    return result
