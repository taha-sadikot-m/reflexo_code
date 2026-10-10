"""The owner broker path must be able to reach the connect/OAuth tools.

Regression guard for the gap that let "connect my Instagram" be delegated to
config_agent, which has no OAuth tool and answered "already connected" from a
stored social handle. The pre-existing allowlist tests only covered
get_owner_tools() — the single-agent path — so nothing caught it.
"""

from __future__ import annotations

from agent.broker_prompts import build_broker_system_prompt
from agent.registry import (
    AGENT_REGISTRY,
    BROKER_DELEGATE_AGENTS,
    OWNER_SPECIALISTS,
    get_agent_definition,
    planned_tool_names,
)
from agent.specialist_prompts import PROMPT_BUILDERS, build_specialist_prompt
from agent.tools import OWNER_CONNECT_TOOLS

META_OAUTH_TOOLS = ("generate_instagram_oauth_url", "generate_facebook_oauth_url")


def test_integrations_agent_is_a_broker_delegate() -> None:
    assert "integrations_agent" in OWNER_SPECIALISTS
    assert "integrations_agent" in BROKER_DELEGATE_AGENTS


def test_integrations_agent_binds_every_connect_tool() -> None:
    bound = set(planned_tool_names("integrations_agent", "whatsapp"))
    assert set(OWNER_CONNECT_TOOLS).issubset(bound)
    for name in META_OAUTH_TOOLS:
        assert name in bound


def test_every_connect_tool_is_reachable_from_some_owner_specialist() -> None:
    reachable: set[str] = set()
    for name, defn in AGENT_REGISTRY.items():
        if defn.session_type == "owner":
            reachable.update(planned_tool_names(name, "whatsapp"))
    missing = sorted(set(OWNER_CONNECT_TOOLS) - reachable)
    assert not missing, f"unreachable in broker mode: {missing}"


def test_integrations_agent_prompt_is_registered_and_renders() -> None:
    defn = get_agent_definition("integrations_agent")
    assert defn.system_prompt_builder in PROMPT_BUILDERS
    prompt = build_specialist_prompt(
        defn.system_prompt_builder,
        business_config={"name": "HolonCode", "owner_name": "Taha"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "list_connected_integrations" in prompt
    for name in META_OAUTH_TOOLS:
        assert name in prompt


def test_integrations_prompt_forbids_answering_status_from_memory() -> None:
    prompt = build_specialist_prompt(
        "build_integrations_prompt",
        business_config={"name": "HolonCode"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "NOT a connection" in prompt
    assert "oauth_url EXACTLY" in prompt


def test_config_prompt_refuses_to_claim_connection_state() -> None:
    prompt = build_specialist_prompt(
        "build_config_mgmt_prompt",
        business_config={"name": "HolonCode"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "not a channel connection" in prompt
    assert "already connected" in prompt


def test_broker_prompt_routes_connect_requests_to_integrations_agent() -> None:
    prompt = build_broker_system_prompt({"name": "HolonCode", "owner_name": "Taha"})
    assert "integrations_agent" in prompt
    assert "connect, reconnect, link, authorize" in prompt
