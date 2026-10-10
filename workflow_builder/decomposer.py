"""NL → WorkflowDefinition via Gemini structured output."""

from __future__ import annotations

import logging
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from workflow_builder.catalog import available_tools_for_business
from workflow_builder.models import WorkflowDefinition

logger = logging.getLogger(__name__)

DECOMPOSER_PROMPT = """You are a workflow architect for a business AI assistant platform.

Given a natural language description of a workflow, decompose it into a structured
definition. You have primitives for STATEFUL, LONG-RUNNING business processes.

STEP PRIMITIVES:
1. CALL_TOOL — Execute an MCP tool (search, send message, create record, etc.)
2. AGGREGATE_EVENTS — Wait until N matching events arrive within a time window.
3. DELAY — Durable sleep. Survives server restarts.
4. WAIT_FOR_EVENT — Pause until a specific external event arrives (or timeout).
5. CONDITION — Branch based on expression evaluating step results or event data.
6. PARALLEL — Execute multiple steps concurrently.
7. SEND_NOTIFICATION — Notify the owner or customer via their preferred channel.

INSTANCE KEY: If instance_key is "{{customer.id}}", events from the same customer
feed the SAME running instance via signals (how "3 comments from one user" works).

AVAILABLE TOOLS:
{available_tools}

BUSINESS CONTEXT:
{business_context}

AVAILABLE TRIGGER TYPES:
whatsapp_message, instagram_dm, instagram_comment, facebook_message,
facebook_comment, email_received, voice_call, payment_received,
payment_failed, order_created, schedule, manual

TEMPLATE VARIABLES: {{{{message.text}}}}, {{{{customer.name}}}}, {{{{customer.id}}}},
{{{{channel}}}}, {{{{business.name}}}}, {{{{result.step_name.field}}}},
{{{{aggregate_count}}}}, {{{{event.field}}}}, {{{{now}}}}, {{{{today}}}}

RULES:
1. Each CALL_TOOL step must use exactly one tool from the available tools list
2. Step dependencies form a DAG (no cycles)
3. Use AGGREGATE_EVENTS for repeated actions over time
4. Use DELAY for follow-ups and reminders
5. Use instance_key when tracking per-customer or per-entity state
6. Prefer search_entities / send_whatsapp_message over inventing tools
7. There is no generate_coupon_code tool — use send_whatsapp_message with a coupon message

EXAMPLES:

"If someone comments on 3 of my posts, send them a coupon":
- trigger: instagram_comment, instance_key: "{{{{customer.id}}}}"
- step wait_for_comments: aggregate_events (threshold=3, window_seconds=604800)
- step send_coupon: call_tool send_whatsapp_message, depends_on wait_for_comments

"When someone asks about prices on Instagram, search products and reply with prices":
- trigger: instagram_dm
- step search: call_tool search_entities
- step reply: call_tool send_instagram_dm, depends_on search
"""


async def decompose_workflow(
    nl_description: str,
    *,
    business_id: str = "",
    available_tools: list[dict[str, Any]] | None = None,
) -> WorkflowDefinition:
    tools = available_tools if available_tools is not None else await available_tools_for_business(business_id)
    listing = "\n".join(f"- {t['name']}" for t in tools) or "(none)"
    business_context = "(no business selected)"
    if business_id:
        try:
            from mcp_servers.reflexo_core.tools.config import get_business_config_impl

            config = await get_business_config_impl(business_id) or {}
            safe_context = {
                key: config.get(key)
                for key in (
                    "name",
                    "description",
                    "vertical",
                    "business_hours",
                    "business_context",
                    "persona_language",
                    "custom_instructions",
                )
                if config.get(key) not in (None, "", {}, [])
            }
            business_context = json.dumps(
                safe_context,
                ensure_ascii=False,
                default=str,
            )[:8000]
        except Exception:
            logger.warning(
                "WF_DECOMPOSE business context unavailable business=%s",
                business_id,
                exc_info=True,
            )
    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    llm = ChatGoogleGenerativeAI(
        model=settings.agent_model or "gemini-2.5-flash",
        google_api_key=settings.google_api_key,
        temperature=0.1,
    )
    structured = llm.with_structured_output(WorkflowDefinition)
    result = await structured.ainvoke(
        [
            SystemMessage(
                content=DECOMPOSER_PROMPT.format(
                    available_tools=listing,
                    business_context=business_context,
                )
            ),
            HumanMessage(content=f"Decompose this workflow:\n\n{nl_description}"),
        ]
    )
    if not isinstance(result, WorkflowDefinition):
        result = WorkflowDefinition.model_validate(result)
    result.nl_description = nl_description
    logger.info("WF_DECOMPOSE name=%s steps=%s", result.name, len(result.steps))
    return result
