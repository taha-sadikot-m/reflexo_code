"""Public, token-authenticated owner Workflow Studio API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/wf", tags=["workflow-studio"])
ws_router = APIRouter(tags=["workflow-studio"])


class StudioChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class StudioActivateRequest(BaseModel):
    shadow_mode: bool = False
    shadow_allowlist: list[str] = Field(default_factory=list, max_length=100)


@router.get("/{token}")
async def studio_state(token: str) -> dict[str, Any]:
    from workflow_builder.studio import get_studio_session

    session = await get_studio_session(token)
    if not session:
        raise HTTPException(status_code=404, detail="Studio link is invalid or expired")
    session.pop("token", None)
    return session


@router.post("/{token}/test", status_code=202)
async def studio_test(token: str) -> dict[str, Any]:
    """Start the scenario test; the owner UI polls the job for progress."""
    from workflow_builder.studio import start_studio_test

    try:
        return await start_studio_test(token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{token}/test/{job_id}")
async def studio_test_status(token: str, job_id: str) -> dict[str, Any]:
    from workflow_builder.studio import studio_test_job

    try:
        return await studio_test_job(token, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{token}/activate")
async def studio_activate(
    token: str,
    body: StudioActivateRequest | None = None,
) -> dict[str, Any]:
    from workflow_builder.studio import activate_from_studio
    from workflow_builder.store import WorkflowTestRequired

    try:
        request = body or StudioActivateRequest()
        return await activate_from_studio(
            token,
            shadow_mode=request.shadow_mode,
            shadow_allowlist=request.shadow_allowlist,
        )
    except WorkflowTestRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{token}/chat")
async def studio_chat(
    token: str,
    body: StudioChatRequest,
) -> dict[str, Any]:
    from workflow_builder.studio import apply_studio_chat_edit

    try:
        result = await apply_studio_chat_edit(token, body.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        code = 404 if "invalid or expired" in message or "not found" in message else 422
        raise HTTPException(status_code=code, detail=message) from exc
    if not result.get("updated"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Workflow edit did not pass validation",
                "errors": result.get("errors") or [],
                "warnings": result.get("warnings") or [],
            },
        )
    return result


@ws_router.websocket("/ws/wf/{token}")
async def studio_updates(websocket: WebSocket, token: str) -> None:
    """Push Studio row changes; polling DB keeps this correct across API workers."""
    from workflow_builder.studio import get_studio_session

    await websocket.accept()
    last_version = ""
    try:
        while True:
            session = await get_studio_session(token)
            if not session:
                await websocket.close(code=4404, reason="Studio link invalid or expired")
                return
            session.pop("token", None)
            version = "|".join(
                [
                    str(session.get("updated_at") or ""),
                    str(session.get("status") or ""),
                    str(session.get("test_results") or ""),
                ]
            )
            if version != last_version:
                await websocket.send_json({"type": "studio_state", "session": session})
                last_version = version
            try:
                # Receive is only a disconnect detector; updates are server-pushed.
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except TimeoutError:
                pass
    except WebSocketDisconnect:
        return
