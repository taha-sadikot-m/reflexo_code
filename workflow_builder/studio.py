"""Workflow Studio token sessions for owner review, testing, and activation."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from shared.db import tenant_connection
from workflow_builder.activation import activate_tested_workflow
from workflow_builder.store import get_workflow

logger = logging.getLogger(__name__)


def generate_workflow_visual(workflow: dict[str, Any]) -> dict[str, Any]:
    nodes = [
        {
            "id": "trigger",
            "type": "input",
            "position": {"x": 0, "y": 80},
            "data": {
                "label": f"Trigger: {workflow.get('trigger_type') or 'event'}",
                "kind": "trigger",
                "config": workflow.get("trigger_config") or {},
            },
        }
    ]
    edges: list[dict[str, Any]] = []
    name_to_id: dict[str, str] = {}
    steps = workflow.get("steps") or []
    for index, step in enumerate(steps):
        node_id = f"step-{index}"
        name = str(step.get("name") or node_id)
        name_to_id[name] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "default",
                "position": {"x": (index + 1) * 220, "y": 80},
                "data": {
                    "label": name,
                    "kind": step.get("type"),
                    "tool": step.get("tool"),
                    "config": step,
                },
            }
        )
    for index, step in enumerate(steps):
        target = f"step-{index}"
        dependencies = step.get("depends_on") or []
        if dependencies:
            for dependency in dependencies:
                source = name_to_id.get(str(dependency))
                if source:
                    edges.append(
                        {
                            "id": f"{source}-{target}",
                            "source": source,
                            "target": target,
                        }
                    )
        elif index == 0:
            edges.append(
                {
                    "id": f"trigger-{target}",
                    "source": "trigger",
                    "target": target,
                }
            )
        else:
            edges.append(
                {
                    "id": f"step-{index - 1}-{target}",
                    "source": f"step-{index - 1}",
                    "target": target,
                }
            )
    return {"nodes": nodes, "edges": edges}


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _studio_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key in {
            "visual_data",
            "test_results",
            "edit_history",
            "steps",
            "trigger_config",
            "conditions",
            "metadata",
            "shadow_allowlist",
        }:
            out[key] = _decode(value)
        elif value is not None and key.endswith("_id"):
            out[key] = str(value)
    return out


async def create_studio_session(
    *,
    business_id: str,
    workflow_id: str,
    created_by: str = "ai",
    created_by_admin: str | None = None,
    owner_conversation_id: str | None = None,
) -> dict[str, Any]:
    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise ValueError("Workflow not found")
    token = secrets.token_urlsafe(32)
    visual = generate_workflow_visual(workflow)
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO workflow_studio_sessions (
                business_id, workflow_id, token, visual_data,
                created_by, created_by_admin, owner_conversation_id
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4::jsonb, $5, $6::uuid, $7::uuid
            )
            RETURNING *
            """,
            business_id,
            workflow_id,
            token,
            json.dumps(visual),
            created_by,
            created_by_admin,
            owner_conversation_id,
        )
    return _studio_row(row)


async def get_studio_session(token: str) -> dict[str, Any] | None:
    """Token lookup happens before a tenant can be known."""
    from admin.db import get_admin_pool

    row = await (await get_admin_pool()).fetchrow(
        """
        SELECT s.*, w.name AS workflow_name, w.description,
               w.nl_description, w.trigger_type, w.trigger_config,
               w.conditions, w.steps, w.instance_key, w.is_active, w.is_tested,
               w.requires_owner_confirmation, w.max_executions_per_day,
               w.shadow_mode, w.shadow_allowlist, w.metadata
        FROM workflow_studio_sessions s
        JOIN ai_workflows w ON w.id = s.workflow_id
        WHERE s.token = $1
          AND s.expires_at > now()
          AND s.status <> 'expired'
        """,
        token,
    )
    return _studio_row(row) if row else None


async def regenerate_studio_link(
    *,
    business_id: str,
    workflow_id: str,
    created_by: str = "ai",
    created_by_admin: str | None = None,
    owner_conversation_id: str | None = None,
) -> dict[str, Any]:
    """Expire existing links and create a fresh 48-hour session token."""
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE workflow_studio_sessions
            SET status = 'expired',
                expires_at = LEAST(expires_at, now()),
                updated_at = now()
            WHERE business_id = $1::uuid AND workflow_id = $2::uuid
            """,
            business_id,
            workflow_id,
        )
    return await create_studio_session(
        business_id=business_id,
        workflow_id=workflow_id,
        created_by=created_by,
        created_by_admin=created_by_admin,
        owner_conversation_id=owner_conversation_id,
    )


async def apply_studio_chat_edit(token: str, instruction: str) -> dict[str, Any]:
    """Apply a Studio edit and append it to the owner's WhatsApp conversation."""
    from agent.pipeline import log_messages
    from workflow_builder.editor import apply_workflow_edit

    session = await get_studio_session(token)
    if not session:
        raise ValueError("Studio link is invalid or expired")
    if session.get("status") == "active" or session.get("is_active"):
        raise RuntimeError(
            "Active workflows cannot be edited from an existing Studio link. "
            "Pause it and generate a new link first."
        )
    if session.get("status") == "testing":
        raise RuntimeError("Wait for the running workflow test before editing")
    business_id = str(session["business_id"])
    workflow_id = str(session["workflow_id"])
    result = await apply_workflow_edit(
        business_id=business_id,
        workflow_id=workflow_id,
        instruction=instruction,
    )
    if not result.get("updated"):
        return result

    workflow = result["workflow"]
    visual = generate_workflow_visual(workflow)
    history_entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "instruction": instruction,
        "workflow": result["definition"],
    }
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE workflow_studio_sessions
            SET visual_data = $2::jsonb,
                edit_history = edit_history || $3::jsonb,
                test_results = NULL,
                status = 'draft',
                updated_at = now()
            WHERE id = $1::uuid
            """,
            str(session["id"]),
            json.dumps(visual, default=str),
            json.dumps([history_entry], default=str),
        )

    # The Studio and WhatsApp are two views of the exact owner conversation
    # captured when the link was created. Never create an unrelated thread here.
    bridge_warning: str | None = None
    owner_conversation_id = session.get("owner_conversation_id")
    if owner_conversation_id:
        await log_messages(
            business_id=business_id,
            conversation_id=str(owner_conversation_id),
            channel="whatsapp",
            incoming_text=instruction,
            incoming_type="text",
            incoming_external_id=None,
            response_text=f"Workflow draft updated: {workflow.get('name')}",
            ai_metadata={
                "source": "workflow_studio",
                "workflow_id": workflow_id,
                "edit": True,
            },
            is_owner=True,
        )
    else:
        bridge_warning = (
            "This Studio session was created outside an owner WhatsApp "
            "conversation, so the edit was not added to WhatsApp history."
        )

    updated = await get_studio_session(token)
    return {
        "updated": True,
        "session": updated,
        "warnings": result.get("warnings") or [],
        "bridge_warning": bridge_warning,
    }


async def start_studio_test(token: str) -> dict[str, Any]:
    """Start a background scenario test for a Studio link and claim the session."""
    from workflow_builder.test_jobs import TestJobRunning, start_test_job

    session = await get_studio_session(token)
    if not session:
        raise ValueError("Studio link is invalid or expired")
    business_id = str(session["business_id"])
    session_id = str(session["id"])
    async with tenant_connection(business_id) as conn:
        claimed = await conn.fetchval(
            """
            UPDATE workflow_studio_sessions
            SET status = 'testing', updated_at = now()
            WHERE id = $1::uuid AND status <> 'testing'
            RETURNING id
            """,
            session_id,
        )
    if not claimed:
        raise RuntimeError("A workflow test is already running for this Studio link")
    try:
        return await start_test_job(business_id, str(session["workflow_id"]))
    except TestJobRunning as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception:
        await _set_studio_status(business_id, session_id, "draft")
        raise


async def studio_test_job(token: str, job_id: str) -> dict[str, Any]:
    """Report job progress and sync the Studio session once the job finishes."""
    from workflow_builder.test_jobs import get_test_job

    session = await get_studio_session(token)
    if not session:
        raise ValueError("Studio link is invalid or expired")
    business_id = str(session["business_id"])
    job = await get_test_job(business_id, str(session["workflow_id"]), job_id=job_id)
    if not job:
        raise ValueError("Test job not found")
    stage = str(job.get("stage"))
    if stage == "completed":
        results = job.get("results") or {}
        async with tenant_connection(business_id) as conn:
            await conn.execute(
                """
                UPDATE workflow_studio_sessions
                SET status = $2, test_results = $3::jsonb, updated_at = now()
                WHERE id = $1::uuid
                """,
                str(session["id"]),
                "tested" if results.get("all_passed") else "draft",
                json.dumps(results, default=str),
            )
    elif stage == "failed":
        await _set_studio_status(business_id, str(session["id"]), "draft")
    return job


async def _set_studio_status(business_id: str, session_id: str, status: str) -> None:
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE workflow_studio_sessions
            SET status = $2, updated_at = now()
            WHERE id = $1::uuid
            """,
            session_id,
            status,
        )


async def activate_from_studio(
    token: str,
    *,
    shadow_mode: bool = False,
    shadow_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    session = await get_studio_session(token)
    if not session:
        raise ValueError("Studio link is invalid or expired")
    business_id = str(session["business_id"])
    row = await activate_tested_workflow(
        business_id,
        str(session["workflow_id"]),
        shadow_mode=shadow_mode,
        shadow_allowlist=shadow_allowlist or [],
    )
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE workflow_studio_sessions
            SET status = 'active', activated_at = now(), updated_at = now()
            WHERE id = $1::uuid
            """,
            str(session["id"]),
        )
    return row
