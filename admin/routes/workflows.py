"""Workflow rules and executions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict

router = APIRouter(prefix="/api/admin/workflows", tags=["admin-workflows"])


class RulePatch(BaseModel):
    is_active: bool | None = None
    name: str | None = None
    description: str | None = None
    requires_owner_confirmation: bool | None = None


class AiWorkflowPatch(BaseModel):
    is_active: bool | None = None
    name: str | None = None
    description: str | None = None


class AiWorkflowActivate(BaseModel):
    shadow_mode: bool = False
    shadow_allowlist: list[str] = Field(default_factory=list, max_length=100)


@router.get("/{business_id}/ai")
async def list_ai_workflows(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from workflow_builder.store import list_workflows

    items = await list_workflows(business_id, active_only=False)
    return {"items": items}


@router.patch("/{business_id}/ai/{workflow_id}")
async def patch_ai_workflow(
    business_id: str, workflow_id: str, body: AiWorkflowPatch, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    from workflow_builder.store import get_workflow

    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "is_active" in data:
        from workflow_builder.store import WorkflowTestRequired

        try:
            if bool(data["is_active"]):
                from workflow_builder.activation import resume_workflow_execution

                row = await resume_workflow_execution(
                    business_id,
                    workflow_id,
                )
            else:
                from workflow_builder.activation import pause_workflow_execution

                row = await pause_workflow_execution(business_id, workflow_id)
        except WorkflowTestRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        await audit(admin.id, "ai_workflow.update", business_id=business_id, entity_id=workflow_id)
        return row
    if data.get("name") or data.get("description"):
        pool = await get_admin_pool()
        row = await pool.fetchrow(
            """
            UPDATE ai_workflows
            SET name = COALESCE($3, name),
                description = COALESCE($4, description),
                updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            RETURNING *
            """,
            workflow_id,
            business_id,
            data.get("name"),
            data.get("description"),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        await audit(admin.id, "ai_workflow.update", business_id=business_id, entity_id=workflow_id)
        return row_dict(row)
    existing = await get_workflow(business_id, workflow_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return existing


@router.post("/{business_id}/ai/{workflow_id}/test", status_code=202)
async def test_ai_workflow(
    business_id: str,
    workflow_id: str,
    admin: AdminPlus,
) -> dict[str, Any]:
    """Start the stateful scenario test; poll the job endpoint for progress."""
    await require_business(business_id)
    from workflow_builder.store import get_workflow
    from workflow_builder.test_jobs import TestJobRunning, start_test_job

    if not await get_workflow(business_id, workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        job = await start_test_job(business_id, workflow_id)
    except TestJobRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit(
        admin.id,
        "ai_workflow.test",
        business_id=business_id,
        entity_id=workflow_id,
        detail={"job_id": job.get("job_id")},
    )
    return job


@router.get("/{business_id}/ai/{workflow_id}/test/{job_id}")
async def get_ai_workflow_test_job(
    business_id: str,
    workflow_id: str,
    job_id: str,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from workflow_builder.test_jobs import get_test_job

    job = await get_test_job(business_id, workflow_id, job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Test job not found")
    return job


@router.post("/{business_id}/ai/{workflow_id}/activate")
async def activate_ai_workflow(
    business_id: str,
    workflow_id: str,
    admin: AdminPlus,
    body: AiWorkflowActivate | None = None,
) -> dict[str, Any]:
    await require_business(business_id)
    from workflow_builder.activation import activate_tested_workflow
    from workflow_builder.store import WorkflowTestRequired

    try:
        request = body or AiWorkflowActivate()
        row = await activate_tested_workflow(
            business_id,
            workflow_id,
            shadow_mode=request.shadow_mode,
            shadow_allowlist=request.shadow_allowlist,
        )
    except WorkflowTestRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await audit(
        admin.id,
        "ai_workflow.activate",
        business_id=business_id,
        entity_id=workflow_id,
        detail={
            "shadow_mode": bool(row.get("shadow_mode")),
            "shadow_allowlist_count": len(row.get("shadow_allowlist") or []),
        },
    )
    return row


@router.post("/{business_id}/ai/{workflow_id}/studio")
async def create_ai_workflow_studio(
    business_id: str,
    workflow_id: str,
    admin: AdminPlus,
) -> dict[str, Any]:
    await require_business(business_id)
    from api.settings import get_api_settings
    from workflow_builder.studio import regenerate_studio_link

    try:
        session = await regenerate_studio_link(
            business_id=business_id,
            workflow_id=workflow_id,
            created_by="admin",
            created_by_admin=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    base = get_api_settings().admin_frontend_origin.rstrip("/")
    await audit(
        admin.id,
        "ai_workflow.studio.create",
        business_id=business_id,
        entity_id=workflow_id,
    )
    return {
        "session_id": session["id"],
        "expires_at": session["expires_at"],
        "url": f"{base}/wf/{session['token']}",
    }


@router.get("/{business_id}/rules")
async def list_rules(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM workflow_rules
        WHERE business_id = $1::uuid
        ORDER BY created_at DESC
        """,
        business_id,
    )
    return {"items": [row_dict(r) for r in rows]}


@router.patch("/{business_id}/rules/{rule_id}")
async def patch_rule(
    business_id: str, rule_id: str, body: RulePatch, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets = []
    params: list[Any] = []
    for k, v in data.items():
        params.append(v)
        sets.append(f"{k} = ${len(params)}")
    params.extend([rule_id, business_id])
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE workflow_rules
        SET {', '.join(sets)}, updated_at = now()
        WHERE id = ${len(params) - 1}::uuid AND business_id = ${len(params)}::uuid
        RETURNING *
        """,
        *params,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    await audit(admin.id, "workflow.update", business_id=business_id, entity_id=rule_id)
    return row_dict(row)


@router.get("/{business_id}/rules/{rule_id}/executions")
async def executions(
    business_id: str,
    rule_id: str,
    admin: CurrentAdmin,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM workflow_executions
        WHERE business_id = $1::uuid AND rule_id = $2::uuid
        ORDER BY created_at DESC
        LIMIT $3
        """,
        business_id,
        rule_id,
        limit,
    )
    return {"items": [row_dict(r) for r in rows]}
