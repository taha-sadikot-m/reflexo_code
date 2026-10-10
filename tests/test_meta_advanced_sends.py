"""Stage T5: advanced WhatsApp payloads and workflow safety."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_builder.models import StepType, WorkflowDefinition, WorkflowStep
from workflow_builder.validator import validate_workflow

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PHONE = "+919876543210"


class FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


@pytest.fixture
def advanced_tools(monkeypatch: pytest.MonkeyPatch):
    from mcp_servers.meta_mcp.tools import whatsapp

    mcp = FakeMcp()
    whatsapp.register(mcp)
    monkeypatch.setattr(whatsapp, "business_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(
        whatsapp,
        "ensure_valid_token",
        AsyncMock(
            return_value={
                "phone_number_id": "phone-id",
                "access_token": "token",
                "catalog_id": "catalog-id",
            }
        ),
    )
    rate = AsyncMock()
    graph = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
    monkeypatch.setattr(whatsapp, "check_rate_limit", rate)
    monkeypatch.setattr(whatsapp, "graph_post", graph)
    return mcp.tools, rate, graph


@pytest.mark.asyncio
async def test_location_payload(advanced_tools) -> None:
    tools, rate, graph = advanced_tools
    result = await tools["send_whatsapp_location"](
        BUSINESS,
        PHONE,
        19.1196,
        72.8478,
        "Chai Corner",
        "Andheri West",
    )
    payload = graph.await_args.args[2]
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "919876543210",
        "type": "location",
        "location": {
            "latitude": 19.1196,
            "longitude": 72.8478,
            "name": "Chai Corner",
            "address": "Andheri West",
        },
    }
    rate.assert_awaited_with(BUSINESS, "whatsapp_location")
    assert result["type"] == "location"


@pytest.mark.asyncio
async def test_flow_payload(advanced_tools) -> None:
    tools, rate, graph = advanced_tools
    await tools["send_whatsapp_flow"](
        BUSINESS,
        PHONE,
        "flow-1",
        "Book Now",
        "Choose a slot",
        "navigate",
        {"screen": "BOOKING_SCREEN"},
        "Appointment",
        "Chai Corner",
    )
    interactive = graph.await_args.args[2]["interactive"]
    assert interactive["type"] == "flow"
    assert interactive["action"]["parameters"] == {
        "flow_message_version": "3",
        "flow_id": "flow-1",
        "flow_cta": "Book Now",
        "flow_action": "navigate",
        "flow_action_payload": {"screen": "BOOKING_SCREEN"},
    }
    rate.assert_awaited_with(BUSINESS, "whatsapp_flow")


@pytest.mark.asyncio
async def test_catalog_payload(advanced_tools) -> None:
    tools, rate, graph = advanced_tools
    await tools["send_whatsapp_catalog"](
        BUSINESS,
        PHONE,
        "Browse",
        ["sku-1", "sku-2"],
        "Products",
        "Reply for help",
    )
    interactive = graph.await_args.args[2]["interactive"]
    assert interactive["type"] == "product_list"
    assert interactive["action"]["catalog_id"] == "catalog-id"
    assert interactive["action"]["sections"][0]["product_items"] == [
        {"product_retailer_id": "sku-1"},
        {"product_retailer_id": "sku-2"},
    ]
    rate.assert_awaited_with(BUSINESS, "whatsapp_catalog")


@pytest.mark.asyncio
async def test_product_catalog_requires_header(advanced_tools) -> None:
    tools, _rate, graph = advanced_tools
    result = await tools["send_whatsapp_catalog"](
        BUSINESS,
        PHONE,
        "Browse",
        ["sku-1"],
    )
    assert result["error"]["code"] == "INVALID_INPUT"
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_carousel_payload(advanced_tools) -> None:
    tools, rate, graph = advanced_tools
    cards = [
        {
            "header_image_url": f"https://example.com/{index}.jpg",
            "body_text": f"Item {index}",
            "buttons": [{"type": "quick_reply", "text": "Buy"}],
        }
        for index in range(2)
    ]
    await tools["send_whatsapp_carousel"](
        BUSINESS,
        PHONE,
        "product_showcase",
        "Our products",
        cards,
    )
    payload = graph.await_args.args[2]
    assert payload["type"] == "template"
    template = payload["template"]
    assert template["name"] == "product_showcase"
    carousel = template["components"][1]
    assert carousel["type"] == "carousel"
    assert len(carousel["cards"]) == 2
    button = carousel["cards"][0]["components"][2]["parameters"][0]
    assert button["type"] == "payload"
    rate.assert_awaited_with(BUSINESS, "whatsapp_carousel")


@pytest.mark.asyncio
async def test_carousel_rejects_missing_image_or_too_many_buttons(
    advanced_tools,
) -> None:
    tools, _rate, graph = advanced_tools
    missing_image = await tools["send_whatsapp_carousel"](
        BUSINESS,
        PHONE,
        "product_showcase",
        "Products",
        [
            {"body_text": "One", "buttons": []},
            {
                "header_image_url": "https://example.com/2.jpg",
                "body_text": "Two",
                "buttons": [],
            },
        ],
    )
    assert missing_image["error"]["code"] == "INVALID_INPUT"
    too_many = await tools["send_whatsapp_carousel"](
        BUSINESS,
        PHONE,
        "product_showcase",
        "Products",
        [
            {
                "header_image_url": f"https://example.com/{index}.jpg",
                "body_text": f"Item {index}",
                "buttons": [
                    {"type": "quick_reply", "text": str(button)}
                    for button in range(3)
                ],
            }
            for index in range(2)
        ],
    )
    assert too_many["error"]["code"] == "INVALID_INPUT"
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_catalog_accepts_new_tools_and_forces_template_after_delay() -> None:
    from workflow_builder.catalog import core_tool_catalog

    catalog = core_tool_catalog()
    names = {tool["name"] for tool in catalog}
    for name in (
        "send_whatsapp_carousel",
        "send_whatsapp_location",
        "send_whatsapp_catalog",
        "send_whatsapp_flow",
    ):
        assert name in names
    immediate = WorkflowDefinition(
        name="Location",
        trigger_type="whatsapp_message",
        steps=[
            WorkflowStep(
                name="send",
                type=StepType.CALL_TOOL,
                tool="send_whatsapp_location",
                params={"to": PHONE, "latitude": 1, "longitude": 2},
            )
        ],
    )
    assert (await validate_workflow(immediate, catalog)).is_valid

    delayed = WorkflowDefinition(
        name="Follow-up",
        trigger_type="whatsapp_message",
        steps=[
            WorkflowStep(name="wait", type=StepType.DELAY, delay_seconds=90000),
            WorkflowStep(
                name="send",
                type=StepType.CALL_TOOL,
                tool="send_whatsapp_message",
                params={"to": PHONE, "text": "Hello"},
                depends_on=["wait"],
            ),
        ],
    )
    validation = await validate_workflow(delayed, catalog)
    assert not validation.is_valid
    assert any("24-hour" in error for error in validation.errors)

    from mcp_servers.meta_mcp.utils.rate_limiter import RATE_LIMITS

    for endpoint in (
        "whatsapp_carousel",
        "whatsapp_location",
        "whatsapp_catalog",
        "whatsapp_flow",
    ):
        assert endpoint in RATE_LIMITS

    from agent.registry import get_agent_definition

    owner_tools = get_agent_definition("workflow_agent").meta_tools
    assert "list_business_templates" in owner_tools
    assert "sync_whatsapp_templates" in owner_tools


@pytest.mark.asyncio
async def test_template_sync_upserts_meta_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_servers.meta_mcp.tools import templates

    monkeypatch.setattr(
        templates,
        "ensure_valid_token",
        AsyncMock(
            return_value={
                "phone_number_id": "phone-id",
                "access_token": "token",
            }
        ),
    )
    monkeypatch.setattr(
        templates,
        "graph_get",
        AsyncMock(
            side_effect=[
                {"whatsapp_business_account": {"id": "waba-id"}},
                {
                    "data": [
                        {
                            "id": "template-id",
                            "name": "order_update",
                            "status": "APPROVED",
                            "category": "UTILITY",
                            "language": "en",
                            "components": [
                                {"type": "BODY", "text": "Order {{1}} shipped"}
                            ],
                        }
                    ]
                },
            ]
        ),
    )
    connection = MagicMock()
    connection.execute = AsyncMock()

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(templates, "tenant_connection", connection_context)
    result = await templates.sync_whatsapp_templates_impl(BUSINESS)
    assert result == {
        "synced": 1,
        "approved": 1,
        "pending": 0,
        "rejected": 0,
    }
    assert "ON CONFLICT" in connection.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_celery_runtime_initializes_db_and_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onboarding.reminders import _ensure_runtime

    init_pool = AsyncMock()
    init_redis = AsyncMock()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr("shared.db.init_pool", init_pool)
    monkeypatch.setattr("shared.redis_client.init_redis", init_redis)
    await _ensure_runtime()
    init_pool.assert_awaited_once()
    init_redis.assert_awaited_once_with("redis://test")
