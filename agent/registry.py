"""Specialist agent registry (MULTI_AGENT_SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt_builder: str
    core_tools: list[str]
    meta_tools: list[str]
    channel_tools: list[str]
    internal_tools: list[str] = field(default_factory=list)
    model: str = "gemini-2.5-flash"
    temperature: float = 0.3
    max_tool_calls: int = 5
    session_type: str = "customer"


AGENT_REGISTRY: dict[str, AgentDefinition] = {}

CUSTOMER_SPECIALISTS = (
    "inquiry_agent",
    "sales_agent",
    "support_agent",
    "social_agent",
)
OWNER_SPECIALISTS = (
    "knowledge_agent",
    "config_agent",
    "analytics_agent",
    "workflow_agent",
    "integrations_agent",
)
CUSTOM_TOOLS_AGENT = "custom_tools_agent"
BROKER_DELEGATE_AGENTS = OWNER_SPECIALISTS + ("inquiry_agent", "sales_agent")


def _register(agent: AgentDefinition) -> None:
    AGENT_REGISTRY[agent.name] = agent


def get_agent_definition(name: str) -> AgentDefinition:
    agent = AGENT_REGISTRY.get(name)
    if agent is None:
        raise KeyError(f"Unknown specialist: {name}")
    return agent


# ── Customer specialists ─────────────────────────────────────────────────────

_register(
    AgentDefinition(
        name="inquiry_agent",
        description=(
            "Answers questions about the business, products, services, "
            "hours, policies, and general information. Handles FAQ lookups."
        ),
        system_prompt_builder="build_inquiry_prompt",
        core_tools=[
            "search_knowledge",
            "search_entities",
            "list_entities",
            "get_entity",
            "get_business_hours",
            "get_dynamic_data",
            "get_customer",
        ],
        meta_tools=[],
        channel_tools=[],
        internal_tools=["handoff_to_agent"],
        temperature=0.3,
        max_tool_calls=5,
    )
)

_register(
    AgentDefinition(
        name="sales_agent",
        description=(
            "Handles purchase intent, pricing inquiries, payment link "
            "generation, and order-related conversations."
        ),
        system_prompt_builder="build_sales_prompt",
        core_tools=[
            "search_entities",
            "list_entities",
            "get_entity",
            "get_dynamic_data",
            "get_customer",
            "update_customer",
        ],
        meta_tools=[],
        channel_tools=["create_payment_link", "check_payment_status"],
        internal_tools=["handoff_to_agent"],
        temperature=0.3,
        max_tool_calls=5,
    )
)

_register(
    AgentDefinition(
        name="support_agent",
        description=(
            "Handles complaints, issues, dissatisfaction, and requests "
            "to speak with the business owner. Escalates when needed."
        ),
        system_prompt_builder="build_support_prompt",
        core_tools=[
            "search_knowledge",
            "get_customer",
            "log_gap",
            "escalate_to_owner",
            "update_customer",
        ],
        meta_tools=[],
        channel_tools=[],
        internal_tools=["handoff_to_agent"],
        temperature=0.2,
        max_tool_calls=3,
    )
)

_register(
    AgentDefinition(
        name="social_agent",
        description=(
            "Replies to Facebook and Instagram post comments. "
            "Keeps responses brief, public-appropriate, and on-brand."
        ),
        system_prompt_builder="build_social_prompt",
        core_tools=[
            "search_knowledge",
            "search_entities",
            "list_entities",
            "get_entity",
            "log_gap",
        ],
        meta_tools=[
            "reply_facebook_comment",
            "get_facebook_post_info",
            "get_facebook_comments",
            "reply_instagram_comment",
            "get_instagram_post_info",
            "get_instagram_comments",
        ],
        channel_tools=[],
        internal_tools=[],
        temperature=0.3,
        max_tool_calls=4,
    )
)

# ── Owner specialists ────────────────────────────────────────────────────────

_register(
    AgentDefinition(
        name="knowledge_agent",
        description=(
            "Manages the business knowledge base and product/service catalog. "
            "Add, update, delete FAQ entries, products, services. "
            "Process uploaded documents. Resolve knowledge gaps."
        ),
        system_prompt_builder="build_knowledge_mgmt_prompt",
        core_tools=[
            "add_knowledge",
            "update_knowledge",
            "delete_knowledge",
            "search_knowledge",
            "upsert_entity",
            "delete_entity",
            "list_entities",
            "search_entities",
            "process_document",
            "resolve_gap",
        ],
        meta_tools=[],
        channel_tools=[],
        session_type="owner",
        temperature=0.1,
        max_tool_calls=8,
    )
)

_register(
    AgentDefinition(
        name="config_agent",
        description=(
            "Manages business settings — hours, dynamic data (today's specials), "
            "operational config, and free-storage facts (contact email, GST, extra details). "
            "Classifies Smart Number contacts as personal or business."
        ),
        system_prompt_builder="build_config_mgmt_prompt",
        core_tools=[
            "update_business_info",
            "get_business_config",
            "set_dynamic_data",
            "get_business_hours",
            "upsert_business_context",
            "classify_contact",
            "get_contact_type",
            "list_personal_contacts",
        ],
        meta_tools=[],
        channel_tools=[],
        session_type="owner",
        temperature=0.1,
        max_tool_calls=4,
    )
)

_register(
    AgentDefinition(
        name="analytics_agent",
        description=(
            "Generates reports — daily summaries, recent conversations, "
            "customer search, customer details and insights."
        ),
        system_prompt_builder="build_analytics_prompt",
        core_tools=[
            "get_daily_summary",
            "get_recent_conversations",
            "search_customers",
            "get_customer",
        ],
        meta_tools=[],
        channel_tools=[],
        session_type="owner",
        temperature=0.2,
        max_tool_calls=3,
    )
)

_register(
    AgentDefinition(
        name="workflow_agent",
        description=(
            "Creates and manages automation rules — follow-ups, reminders, "
            "greeting workflows, scheduled messages."
        ),
        system_prompt_builder="build_workflow_mgmt_prompt",
        core_tools=[
            "create_workflow_rule",
            "list_workflow_rules",
            "update_workflow_rule",
            "delete_workflow_rule",
            "get_workflow_performance",
            "build_workflow",
            "test_workflow",
            "deploy_workflow",
            "run_workflow_scenario_test",
            "activate_workflow",
            "generate_workflow_studio_link",
            "list_active_workflows",
            "pause_workflow",
            "resume_workflow",
            "get_workflow_health",
        ],
        meta_tools=[
            "list_business_templates",
            "sync_whatsapp_templates",
        ],
        channel_tools=[],
        session_type="owner",
        temperature=0.1,
        max_tool_calls=4,
    )
)

_register(
    AgentDefinition(
        name="integrations_agent",
        description=(
            "Connects and reconnects external accounts — Instagram, Facebook, "
            "Gmail/Calendar, Razorpay, SMS. Generates login/OAuth URLs and reports "
            "which integrations are currently connected."
        ),
        system_prompt_builder="build_integrations_prompt",
        # Must stay in sync with OWNER_CONNECT_TOOLS in agent/tools.py
        # (pinned by tests/test_owner_connect_routing.py).
        core_tools=[
            "connect_razorpay",
            "connect_fast2sms",
            "get_oauth_connect_url_tool",
            "list_connected_integrations",
            "get_integration_connect_instructions",
            "generate_instagram_oauth_url",
            "generate_facebook_oauth_url",
        ],
        meta_tools=[],
        channel_tools=[],
        session_type="owner",
        temperature=0.1,
        max_tool_calls=4,
    )
)


_register(
    AgentDefinition(
        name="custom_tools_agent",
        description=(
            "Executes tools from the business owner's custom MCP connections "
            "(CRM, inventory, accounting, etc.). Tools are discovered from "
            "configured MCP servers."
        ),
        system_prompt_builder="build_custom_tools_prompt",
        core_tools=["search_knowledge", "get_customer"],
        meta_tools=[],
        channel_tools=[],
        internal_tools=["handoff_to_agent"],
        session_type="owner",
        temperature=0.2,
        max_tool_calls=8,
    )
)


def planned_tool_names(agent_name: str, channel: str) -> list[str]:
    """Names the specialist should bind (MCP may omit missing tools)."""
    from agent.channel_tools import get_channel_tools

    agent = get_agent_definition(agent_name)
    names = list(agent.core_tools) + list(agent.channel_tools) + list(agent.meta_tools)
    if agent.session_type == "customer" and agent.name != "social_agent":
        names.extend(get_channel_tools(channel))
    names.extend(agent.internal_tools)
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out
