"""Stage T2: complete owner Workflow Studio editing and token lifecycle."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workflow_builder.models import StepType, WorkflowDefinition, WorkflowStep

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKFLOW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _definition(delay_seconds: int = 86400) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Follow-up",
        description="Wait and send a reminder",
        trigger_type="whatsapp_message",
        instance_key="{{customer.id}}",
        steps=[
            WorkflowStep(
                name="wait",
                type=StepType.DELAY,
                delay_seconds=delay_seconds,
            ),
            WorkflowStep(
                name="send",
                type=StepType.CALL_TOOL,
                tool="send_whatsapp_template",
                params={
                    "to": "{{external_sender_id}}",
                    "template_name": "reminder",
                },
                depends_on=["wait"],
            ),
        ],
    )


def _row(definition: WorkflowDefinition) -> dict:
    return {
        "id": WORKFLOW,
        "business_id": BUSINESS,
        **definition.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_nl_edit_changes_delay_and_persists_inactive_definition() -> None:
    from workflow_builder.editor import WorkflowEditPatch, apply_workflow_edit

    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=WorkflowEditPatch(
            steps=_definition(delay_seconds=172800).steps
        )
    )
    saved = {**_row(_definition(172800)), "is_active": False, "is_tested": False}
    with (
        patch(
            "workflow_builder.editor.get_workflow",
            new=AsyncMock(return_value=_row(_definition())),
        ),
        patch(
            "workflow_builder.editor.available_tools_for_business",
            new=AsyncMock(
                return_value=[
                    {"name": "send_whatsapp_template", "description": "send"}
                ]
            ),
        ),
        patch(
            "workflow_builder.editor.ChatGoogleGenerativeAI",
            return_value=llm,
        ),
        patch(
            "workflow_builder.editor.update_workflow_definition",
            new=AsyncMock(return_value=saved),
        ) as update,
    ):
        result = await apply_workflow_edit(
            business_id=BUSINESS,
            workflow_id=WORKFLOW,
            instruction="Wait two days instead of one",
        )
    assert result["updated"] is True
    edited = update.await_args.args[2]
    assert edited.steps[0].delay_seconds == 172800


@pytest.mark.asyncio
async def test_invalid_edit_is_rejected_before_persist() -> None:
    from workflow_builder.editor import WorkflowEditPatch, apply_workflow_edit

    invalid = _definition()
    invalid.steps[1].tool = "invented_send_tool"
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=WorkflowEditPatch(steps=invalid.steps)
    )
    with (
        patch(
            "workflow_builder.editor.get_workflow",
            new=AsyncMock(return_value=_row(_definition())),
        ),
        patch(
            "workflow_builder.editor.available_tools_for_business",
            new=AsyncMock(
                return_value=[
                    {"name": "send_whatsapp_template", "description": "send"}
                ]
            ),
        ),
        patch(
            "workflow_builder.editor.ChatGoogleGenerativeAI",
            return_value=llm,
        ),
        patch(
            "workflow_builder.editor.update_workflow_definition",
            new=AsyncMock(),
        ) as update,
    ):
        result = await apply_workflow_edit(
            business_id=BUSINESS,
            workflow_id=WORKFLOW,
            instruction="Use an invented tool",
        )
    assert result["updated"] is False
    assert any("unknown tool" in error for error in result["errors"])
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_sparse_edit_preserves_unrelated_definition_fields() -> None:
    from workflow_builder.editor import WorkflowEditPatch, apply_workflow_edit

    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=WorkflowEditPatch(
            description="A clearer description",
            instance_key=None,
            response_template=None,
        )
    )
    current = _definition()
    with (
        patch(
            "workflow_builder.editor.get_workflow",
            new=AsyncMock(return_value=_row(current)),
        ),
        patch(
            "workflow_builder.editor.available_tools_for_business",
            new=AsyncMock(
                return_value=[
                    {"name": "send_whatsapp_template", "description": "send"}
                ]
            ),
        ),
        patch(
            "workflow_builder.editor.ChatGoogleGenerativeAI",
            return_value=llm,
        ),
        patch(
            "workflow_builder.editor.update_workflow_definition",
            new=AsyncMock(return_value=_row(current)),
        ) as update,
    ):
        result = await apply_workflow_edit(
            business_id=BUSINESS,
            workflow_id=WORKFLOW,
            instruction="Make the description clearer",
        )
    assert result["updated"] is True
    edited = update.await_args.args[2]
    assert edited.description == "A clearer description"
    assert edited.name == current.name
    assert edited.instance_key == current.instance_key
    assert edited.steps == current.steps


@pytest.mark.asyncio
async def test_studio_chat_updates_visual_history_and_owner_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.studio import apply_studio_chat_edit

    session = {
        "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "business_id": BUSINESS,
        "workflow_id": WORKFLOW,
        "owner_conversation_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    }
    updated_session = {
        **session,
        "status": "draft",
        "visual_data": {"nodes": [{"id": "trigger"}], "edges": []},
    }
    get_session = AsyncMock(side_effect=[session, updated_session])
    monkeypatch.setattr(
        "workflow_builder.studio.get_studio_session",
        get_session,
    )
    monkeypatch.setattr(
        "workflow_builder.editor.apply_workflow_edit",
        AsyncMock(
            return_value={
                "updated": True,
                "workflow": _row(_definition(172800)),
                "definition": _definition(172800).model_dump(mode="json"),
                "warnings": [],
            }
        ),
    )

    connection = MagicMock()
    connection.execute = AsyncMock()

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(
        "workflow_builder.studio.tenant_connection",
        connection_context,
    )
    log = AsyncMock()
    monkeypatch.setattr("agent.pipeline.log_messages", log)

    result = await apply_studio_chat_edit(
        "valid-token",
        "Wait two days instead",
    )
    assert result["updated"] is True
    assert result["session"]["status"] == "draft"
    sql = connection.execute.await_args.args[0]
    assert "test_results = NULL" in sql
    assert "edit_history = edit_history" in sql
    log.assert_awaited_once()
    assert (
        log.await_args.kwargs["conversation_id"]
        == "dddddddd-dddd-dddd-dddd-dddddddddddd"
    )


@pytest.mark.asyncio
async def test_regenerate_expires_old_link_and_issues_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.studio import regenerate_studio_link

    connection = MagicMock()
    connection.execute = AsyncMock()

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(
        "workflow_builder.studio.tenant_connection",
        connection_context,
    )
    create = AsyncMock(
        return_value={
            "token": "new-token",
            "expires_at": "2026-09-10T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        "workflow_builder.studio.create_studio_session",
        create,
    )
    result = await regenerate_studio_link(
        business_id=BUSINESS,
        workflow_id=WORKFLOW,
    )
    assert result["token"] == "new-token"
    sql = connection.execute.await_args.args[0]
    assert "SET status = 'expired'" in sql
    assert "status <> 'active'" not in sql
    create.assert_awaited_once_with(
        business_id=BUSINESS,
        workflow_id=WORKFLOW,
        created_by="ai",
        created_by_admin=None,
        owner_conversation_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "testing"])
async def test_chat_cannot_edit_active_or_testing_session(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.studio import apply_studio_chat_edit

    monkeypatch.setattr(
        "workflow_builder.studio.get_studio_session",
        AsyncMock(
            return_value={
                "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "business_id": BUSINESS,
                "workflow_id": WORKFLOW,
                "status": status,
                "is_active": status == "active",
            }
        ),
    )
    edit = AsyncMock()
    monkeypatch.setattr("workflow_builder.editor.apply_workflow_edit", edit)
    with pytest.raises(RuntimeError):
        await apply_studio_chat_edit("valid-token", "Change it")
    edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_session_status_is_rejected_by_lookup() -> None:
    from workflow_builder.studio import get_studio_session

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    with patch("admin.db.get_admin_pool", new=AsyncMock(return_value=pool)):
        assert await get_studio_session("old-token") is None
    query = pool.fetchrow.await_args.args[0]
    assert "s.status <> 'expired'" in query


@pytest.mark.asyncio
async def test_chat_api_rejects_invalid_edit() -> None:
    from fastapi import HTTPException

    from api.workflow_studio import StudioChatRequest, studio_chat

    with patch(
        "workflow_builder.studio.apply_studio_chat_edit",
        new=AsyncMock(
            return_value={
                "updated": False,
                "errors": ["unknown tool"],
                "warnings": [],
            }
        ),
    ):
        with pytest.raises(HTTPException) as error:
            await studio_chat(
                "valid-token",
                StudioChatRequest(message="Use an invalid tool"),
            )
    assert error.value.status_code == 422
