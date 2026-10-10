"""RLS-aware CRUD for ai_workflows and ai_workflow_runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from shared.db import tenant_connection
from workflow_builder.models import WorkflowDefinition


class WorkflowTestRequired(ValueError):
    """Raised when activation is attempted without a passing scenario report."""


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, UUID):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key in {
            "steps",
            "trigger_config",
            "conditions",
            "metadata",
            "shadow_allowlist",
            "trigger_event",
            "steps_executed",
        } and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return out


async def insert_workflow(
    business_id: str,
    definition: WorkflowDefinition,
    *,
    nl_description: str = "",
    is_tested: bool = False,
    is_active: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ai_workflows (
                business_id, name, description, nl_description, trigger_type,
                trigger_config, conditions, steps, response_template, instance_key,
                max_duration_seconds, continue_as_new, requires_owner_confirmation,
                max_executions_per_day, is_tested, is_active, created_by, metadata
            ) VALUES (
                $1::uuid, $2, $3, $4, $5::workflow_trigger,
                $6::jsonb, $7::jsonb, $8::jsonb, $9, $10,
                $11, $12, $13, $14, $15, $16, 'ai', $17::jsonb
            )
            RETURNING *
            """,
            business_id,
            definition.name,
            definition.description,
            nl_description or definition.nl_description,
            definition.trigger_type,
            _json(definition.trigger_config),
            _json(definition.conditions),
            _json([s.model_dump(mode="json") for s in definition.steps]),
            definition.response_template,
            definition.instance_key,
            definition.max_duration_seconds,
            definition.continue_as_new,
            definition.requires_owner_confirmation,
            definition.max_executions_per_day,
            is_tested,
            is_active,
            _json(metadata or {}),
        )
    return _row(row)


async def list_workflows(business_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
    clause = "AND is_active = true" if active_only else ""
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM ai_workflows
            WHERE business_id = $1::uuid {clause}
            ORDER BY created_at DESC
            """,
            business_id,
        )
    return [_row(r) for r in rows]


async def get_workflow(business_id: str, workflow_id: str) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM ai_workflows
            WHERE business_id = $1::uuid AND id = $2::uuid
            """,
            business_id,
            workflow_id,
        )
    return _row(row) if row else None


async def update_workflow_definition(
    business_id: str,
    workflow_id: str,
    definition: WorkflowDefinition,
) -> dict[str, Any] | None:
    """Replace an inactive Temporal definition and invalidate prior test results."""
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET name = $3,
                description = $4,
                nl_description = $5,
                trigger_type = $6::workflow_trigger,
                trigger_config = $7::jsonb,
                conditions = $8::jsonb,
                steps = $9::jsonb,
                response_template = $10,
                instance_key = $11,
                max_duration_seconds = $12,
                continue_as_new = $13,
                requires_owner_confirmation = $14,
                max_executions_per_day = $15,
                is_tested = false,
                is_active = false,
                metadata = metadata - 'test_results' - 'last_tested_at',
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            business_id,
            workflow_id,
            definition.name,
            definition.description,
            definition.nl_description,
            definition.trigger_type,
            _json(definition.trigger_config),
            _json(definition.conditions),
            _json([step.model_dump(mode="json") for step in definition.steps]),
            definition.response_template,
            definition.instance_key,
            definition.max_duration_seconds,
            definition.continue_as_new,
            definition.requires_owner_confirmation,
            definition.max_executions_per_day,
        )
    return _row(row) if row else None


async def set_active(
    business_id: str,
    workflow_id: str,
    is_active: bool,
    *,
    shadow_mode: bool | None = None,
    shadow_allowlist: list[str] | None = None,
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        if is_active:
            eligible = await conn.fetchval(
                """
                SELECT is_tested
                       AND COALESCE(
                           (metadata->'test_results'->>'all_passed')::boolean,
                           false
                       )
                FROM ai_workflows
                WHERE business_id = $1::uuid AND id = $2::uuid
                """,
                business_id,
                workflow_id,
            )
            if not eligible:
                raise WorkflowTestRequired(
                    "Workflow must pass its stateful scenario test before activation"
                )
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET is_active = $3,
                shadow_mode = COALESCE($4, shadow_mode),
                shadow_allowlist = COALESCE($5::jsonb, shadow_allowlist),
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            business_id,
            workflow_id,
            is_active,
            shadow_mode,
            _json(shadow_allowlist) if shadow_allowlist is not None else None,
        )
    return _row(row) if row else None


async def save_test_results(
    business_id: str,
    workflow_id: str,
    results: dict[str, Any],
) -> dict[str, Any] | None:
    """Persist the latest stateful scenario report on an AI workflow."""
    all_passed = bool(results.get("all_passed"))
    payload = {
        "test_results": results,
        "last_tested_at": datetime.now(timezone.utc).isoformat(),
    }
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET metadata = metadata || $3::jsonb,
                is_tested = $4,
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            business_id,
            workflow_id,
            _json(payload),
            all_passed,
        )
    return _row(row) if row else None


async def save_test_progress(
    business_id: str,
    workflow_id: str,
    progress: dict[str, Any],
    *,
    expected_job_id: str | None = None,
) -> bool:
    """Store live test-job progress; a stale job never overwrites a newer one."""
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET metadata = metadata || jsonb_build_object('test_progress', $3::jsonb),
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
              AND (
                $4::text IS NULL
                OR metadata #>> '{test_progress,job_id}' IS NULL
                OR metadata #>> '{test_progress,job_id}' = $4::text
              )
            RETURNING id
            """,
            business_id,
            workflow_id,
            _json(progress),
            expected_job_id,
        )
    return row is not None


async def load_test_progress(
    business_id: str,
    workflow_id: str,
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        value = await conn.fetchval(
            """
            SELECT metadata -> 'test_progress'
            FROM ai_workflows
            WHERE business_id = $1::uuid AND id = $2::uuid
            """,
            business_id,
            workflow_id,
        )
    if not value:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value)


async def pattern_suggestion_exists(
    business_id: str,
    fingerprint: str,
) -> bool:
    async with tenant_connection(business_id) as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT 1
                FROM ai_workflows
                WHERE business_id = $1::uuid
                  AND metadata->>'pattern_fingerprint' = $2
                LIMIT 1
                """,
                business_id,
                fingerprint,
            )
        )


async def mark_pattern_suggestion(
    business_id: str,
    workflow_id: str,
    *,
    fingerprint: str,
    occurrence_count: int,
) -> None:
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE ai_workflows
            SET metadata = metadata || $3::jsonb,
                is_active = false,
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            """,
            business_id,
            workflow_id,
            _json(
                {
                    "pattern_suggestion": True,
                    "pattern_fingerprint": fingerprint,
                    "pattern_occurrence_count": occurrence_count,
                }
            ),
        )


async def mark_deployed(
    business_id: str,
    workflow_id: str,
    *,
    temporal_workflow_id: str,
    is_tested: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET is_active = true,
                is_tested = $4,
                temporal_workflow_id = $3,
                metadata = CASE
                    WHEN $5::jsonb IS NULL THEN metadata
                    ELSE metadata || $5::jsonb
                END,
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            business_id,
            workflow_id,
            temporal_workflow_id,
            is_tested,
            _json(metadata) if metadata is not None else None,
        )
    return _row(row) if row else None


async def mark_prepared(
    business_id: str,
    workflow_id: str,
    *,
    temporal_workflow_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Assign the stable Temporal base ID while keeping a workflow inactive."""
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflows
            SET is_active = false,
                is_tested = false,
                temporal_workflow_id = $3,
                metadata = metadata || $4::jsonb,
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            RETURNING *
            """,
            business_id,
            workflow_id,
            temporal_workflow_id,
            _json(metadata or {}),
        )
    return _row(row) if row else None


async def auto_pause(business_id: str, workflow_id: str, *, reason: str) -> None:
    meta = {
        "auto_paused_at": datetime.now(timezone.utc).isoformat(),
        "auto_pause_reason": reason,
    }
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE ai_workflows
            SET is_active = false,
                metadata = metadata || $3::jsonb,
                updated_at = now()
            WHERE business_id = $1::uuid AND id = $2::uuid
            """,
            business_id,
            workflow_id,
            _json(meta),
        )


async def matching_active(business_id: str, trigger_type: str) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM ai_workflows
            WHERE business_id = $1::uuid
              AND trigger_type = $2::workflow_trigger
              AND is_active = true
            """,
            business_id,
            trigger_type,
        )
    return [_row(r) for r in rows]


async def insert_run(
    business_id: str,
    workflow_id: str,
    *,
    trigger_event: dict[str, Any],
    conversation_id: str | None = None,
    customer_id: str | None = None,
    temporal_instance_id: str = "",
    status: str = "running",
    count_metrics: bool = True,
    is_shadow: bool = False,
) -> dict[str, Any]:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ai_workflow_runs (
                business_id, workflow_id, trigger_event, conversation_id, customer_id,
                temporal_workflow_instance_id, status, is_shadow
            ) VALUES (
                $1::uuid, $2::uuid, $3::jsonb, $4::uuid, $5::uuid, $6, $7, $8
            )
            RETURNING *
            """,
            business_id,
            workflow_id,
            _json(trigger_event),
            conversation_id,
            customer_id,
            temporal_instance_id,
            status,
            is_shadow,
        )
        if count_metrics:
            await conn.execute(
                """
                UPDATE ai_workflows
                SET total_runs = total_runs + 1, last_run_at = now(), updated_at = now()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                workflow_id,
                business_id,
            )
    return _row(row)


async def append_step(
    business_id: str,
    run_id: str,
    step: dict[str, Any],
) -> None:
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE ai_workflow_runs
            SET steps_executed = COALESCE(steps_executed, '[]'::jsonb) || $3::jsonb
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            run_id,
            business_id,
            _json([step]),
        )


async def finish_run(
    business_id: str,
    run_id: str,
    *,
    status: str,
    error_message: str | None = None,
    latency_ms: int | None = None,
    count_metrics: bool = True,
) -> None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE ai_workflow_runs
            SET status = $3, error_message = $4, total_latency_ms = $5
            WHERE id = $1::uuid AND business_id = $2::uuid
            RETURNING workflow_id
            """,
            run_id,
            business_id,
            status,
            error_message,
            latency_ms,
        )
        if row and count_metrics and status in {"success", "failed"}:
            col = "successful_runs" if status == "success" else "failed_runs"
            await conn.execute(
                f"""
                UPDATE ai_workflows
                SET {col} = {col} + 1, updated_at = now()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                str(row["workflow_id"]),
                business_id,
            )


async def runs_last_24h(business_id: str, workflow_id: str) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT status FROM ai_workflow_runs
            WHERE business_id = $1::uuid AND workflow_id = $2::uuid
              AND created_at > now() - interval '24 hours'
              AND is_shadow = false
            """,
            business_id,
            workflow_id,
        )
    return [_row(r) for r in rows]
