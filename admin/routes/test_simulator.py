"""JWT admin Test Simulator lab (/api/admin/test-simulator/*). Always skip_send."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from admin.auth import CurrentAdmin
from admin.helpers import require_business
from admin.test_simulator_lab import (
    MAX_BURST,
    SCENARIO_PRESETS,
    WEBHOOK_PRESETS,
    bridge_receive,
    bridge_start,
    e2e_history,
    fire_workflow,
    process_webhook_safe,
    run_burst,
    run_e2e,
    run_scenario,
    run_synthetic_message,
    voice_end,
    voice_start,
    voice_turn,
)

router = APIRouter(prefix="/api/admin/test-simulator", tags=["admin-test-simulator"])


class MessageBody(BaseModel):
    business_id: str
    text: str
    channel: str = "whatsapp"
    mode: str = Field(default="customer", pattern="^(customer|owner)$")
    session_id: str | None = None
    customer_name: str = "Test Customer"
    sender_id: str | None = None
    comment_id: str | None = None
    post_id: str | None = None
    burst: int | None = Field(default=None, ge=1, le=MAX_BURST)


class WebhookBody(BaseModel):
    business_id: str
    preset: str | None = None
    payload: dict[str, Any] | None = None
    validate_only: bool = False
    run_pipeline: bool = False


class VoiceStartBody(BaseModel):
    business_id: str
    caller: str = "+919876543210"
    is_owner: bool = False


class VoiceTurnBody(BaseModel):
    session_id: str
    text: str


class VoiceEndBody(BaseModel):
    session_id: str


class WorkflowFireBody(BaseModel):
    business_id: str
    workflow_id: str | None = None
    event_type: str | None = None
    text: str = "simulator workflow fire"


class BridgeStartBody(BaseModel):
    business_id: str
    phone: str = "+919876543210"
    prompt: str = "Please share the file here."


class BridgeReceiveBody(BaseModel):
    business_id: str
    phone: str = "+919876543210"
    text: str = "here is the document"


class ScenarioBody(BaseModel):
    business_id: str
    preset: str | None = None
    workflow_id: str | None = None
    user_count: int | None = Field(default=None, ge=1, le=25)
    delay_seconds: float = 0
    keyword: str | None = None
    duplicate_commenter: bool | None = None
    over_limit: bool | None = None
    non_matching: bool | None = None
    process: bool = True


class E2EBody(BaseModel):
    business_id: str
    process: bool = True


@router.get("/webhook/presets")
async def webhook_presets(admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    return {
        "items": [
            {"id": key, "name": val["name"], "payload": val["payload"]}
            for key, val in WEBHOOK_PRESETS.items()
        ]
    }


@router.get("/scenario/presets")
async def scenario_presets(admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    return {"items": list(SCENARIO_PRESETS.values())}


@router.get("/scenario/recommendation")
async def scenario_recommendation(
    business_id: str,
    workflow_id: str,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    from workflow_builder.store import get_workflow
    from workflow_builder.testing import auto_select_scenario

    workflow = await get_workflow(business_id, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {
        "workflow_id": workflow_id,
        "workflow_name": workflow.get("name"),
        "scenario": auto_select_scenario(workflow),
    }


@router.post("/message")
async def simulate_message(body: MessageBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    try:
        if body.burst:
            return await run_burst(
                business_id=body.business_id,
                text=body.text,
                channel=body.channel,
                count=body.burst,
                mode=body.mode,
            )
        return await run_synthetic_message(
            business_id=body.business_id,
            text=body.text,
            channel=body.channel,
            mode=body.mode,
            session_id=body.session_id,
            customer_name=body.customer_name,
            sender_id=body.sender_id,
            comment_id=body.comment_id,
            post_id=body.post_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/webhook")
async def simulate_webhook(body: WebhookBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    payload = body.payload
    if body.preset:
        preset = WEBHOOK_PRESETS.get(body.preset)
        if not preset:
            raise HTTPException(status_code=400, detail="Unknown webhook preset")
        payload = preset["payload"]
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload or preset is required")
    return await process_webhook_safe(
        business_id=body.business_id,
        payload=payload,
        validate_only=body.validate_only,
        run_pipeline=body.run_pipeline,
    )


@router.post("/voice/start")
async def sim_voice_start(body: VoiceStartBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    return await voice_start(
        business_id=body.business_id, caller=body.caller, is_owner=body.is_owner
    )


@router.post("/voice/turn")
async def sim_voice_turn(body: VoiceTurnBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    try:
        return await voice_turn(session_id=body.session_id, text=body.text)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/voice/end")
async def sim_voice_end(body: VoiceEndBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    return await voice_end(session_id=body.session_id)


@router.post("/workflow/fire")
async def sim_workflow_fire(body: WorkflowFireBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    try:
        return await fire_workflow(
            business_id=body.business_id,
            workflow_id=body.workflow_id,
            event_type=body.event_type,
            text=body.text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bridge/start")
async def sim_bridge_start(body: BridgeStartBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    try:
        return await bridge_start(
            business_id=body.business_id, phone=body.phone, prompt=body.prompt
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bridge/receive")
async def sim_bridge_receive(body: BridgeReceiveBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    return await bridge_receive(
        business_id=body.business_id, phone=body.phone, text=body.text
    )


@router.post("/scenario")
async def sim_scenario(body: ScenarioBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    edge: dict[str, bool] = {}
    if body.duplicate_commenter is not None:
        edge["duplicate_commenter"] = body.duplicate_commenter
    if body.over_limit is not None:
        edge["over_limit"] = body.over_limit
    if body.non_matching is not None:
        edge["non_matching"] = body.non_matching
    try:
        if body.workflow_id:
            from workflow_builder.owner_test import run_owner_facing_test

            return await run_owner_facing_test(
                business_id=body.business_id,
                workflow_id=body.workflow_id,
                run_agent_pipeline=body.process,
                include_technical=True,
            )
        if not body.preset:
            raise ValueError("preset or workflow_id is required")
        return await run_scenario(
            business_id=body.business_id,
            preset_id=body.preset,
            user_count=body.user_count,
            delay_seconds=body.delay_seconds,
            keyword=body.keyword,
            edge=edge or None,
            process=body.process,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/e2e")
async def sim_e2e(body: E2EBody, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(body.business_id)
    return await run_e2e(business_id=body.business_id, process=body.process)


@router.get("/e2e/history")
async def sim_e2e_history(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    return await e2e_history(business_id)
