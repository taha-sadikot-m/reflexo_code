"""Background stateful test jobs: progress, readiness gating, and polling APIs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from admin.routes.workflows import router as workflows_router
from workflow_builder import test_jobs

BIZ = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WF = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class FakeProgressStore:
    """In-memory stand-in for ai_workflows.metadata.test_progress."""

    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.writes: list[dict[str, Any]] = []

    async def save(
        self,
        _business_id: str,
        _workflow_id: str,
        progress: dict[str, Any],
        *,
        expected_job_id: str | None = None,
    ) -> bool:
        current = (self.value or {}).get("job_id")
        if expected_job_id and current and current != expected_job_id:
            return False
        self.value = dict(progress)
        self.writes.append(dict(progress))
        return True

    async def load(self, _business_id: str, _workflow_id: str) -> dict[str, Any] | None:
        return dict(self.value) if self.value else None


@pytest.fixture
def progress_store(monkeypatch: pytest.MonkeyPatch) -> FakeProgressStore:
    store = FakeProgressStore()
    monkeypatch.setattr(test_jobs, "save_test_progress", store.save)
    monkeypatch.setattr(test_jobs, "load_test_progress", store.load)
    monkeypatch.setattr("workflow_builder.store.save_test_progress", store.save)
    return store


def _user(role: str = "admin") -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email="admin@reflexo.ai",
        name="Admin",
        role=role,
        is_active=True,
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(workflows_router)
    app.dependency_overrides[get_current_admin] = lambda: _user()
    return app


async def _drain() -> None:
    """Let the detached job task finish."""
    for _ in range(50):
        await asyncio.sleep(0)
        if not test_jobs._TASKS:
            return
    await asyncio.gather(*list(test_jobs._TASKS), return_exceptions=True)


@pytest.mark.asyncio
async def test_job_reports_each_event_and_final_results(
    progress_store: FakeProgressStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workflow_builder.deployer.check_temporal_ready",
        AsyncMock(return_value={"ready": True, "pollers": 1}),
    )

    async def _run(*, business_id, workflow_id, run_agent_pipeline, progress):
        del business_id, workflow_id
        assert run_agent_pipeline is False
        await progress({"stage": "running", "events_total": 2, "events_processed": 0})
        for index in (1, 2):
            await progress(
                {
                    "stage": "running",
                    "events_processed": index,
                    "last_event": {"event": index, "matched": True, "outcome": "success"},
                }
            )
        return {"all_passed": True, "state_checks": [], "title": "Workflow test passed"}

    monkeypatch.setattr("workflow_builder.owner_test.run_owner_facing_test", _run)

    job = await test_jobs.start_test_job(BIZ, WF)
    assert job["stage"] == "queued"
    await _drain()

    final = await test_jobs.get_test_job(BIZ, WF, job_id=job["job_id"])
    assert final is not None
    assert final["stage"] == "completed"
    assert final["all_passed"] is True
    assert final["events_processed"] == 2
    assert [event["event"] for event in final["events"]] == [1, 2]
    assert any(write.get("stage") == "checking_temporal" for write in progress_store.writes)


@pytest.mark.asyncio
async def test_job_fails_fast_when_worker_is_missing(
    progress_store: FakeProgressStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workflow_builder.deployer.check_temporal_ready",
        AsyncMock(
            return_value={
                "ready": False,
                "reason": "No worker is polling task queue 'reflexo-workflows'",
                "hint": "Start the workflow worker: python -m workflow_builder.worker",
            }
        ),
    )
    never = AsyncMock(side_effect=AssertionError("scenario must not run"))
    monkeypatch.setattr("workflow_builder.owner_test.run_owner_facing_test", never)

    job = await test_jobs.start_test_job(BIZ, WF)
    await _drain()

    final = await test_jobs.get_test_job(BIZ, WF, job_id=job["job_id"])
    assert final is not None
    assert final["stage"] == "failed"
    assert "No worker" in final["error"]
    assert "workflow_builder.worker" in final["hint"]
    never.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_start_is_rejected_while_running(
    progress_store: FakeProgressStore,
) -> None:
    progress_store.value = {
        "job_id": "abc",
        "stage": "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with pytest.raises(test_jobs.TestJobRunning):
        await test_jobs.start_test_job(BIZ, WF)


@pytest.mark.asyncio
async def test_stale_job_does_not_block_and_reports_failed(
    progress_store: FakeProgressStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(timezone.utc) - timedelta(seconds=test_jobs.STALE_AFTER_SECONDS + 60)
    progress_store.value = {
        "job_id": "abc",
        "stage": "running",
        "updated_at": old.isoformat(),
    }
    reported = await test_jobs.get_test_job(BIZ, WF, job_id="abc")
    assert reported is not None
    assert reported["stage"] == "failed"

    monkeypatch.setattr(
        "workflow_builder.deployer.check_temporal_ready",
        AsyncMock(return_value={"ready": True}),
    )
    monkeypatch.setattr(
        "workflow_builder.owner_test.run_owner_facing_test",
        AsyncMock(return_value={"all_passed": True}),
    )
    job = await test_jobs.start_test_job(BIZ, WF)
    assert job["job_id"] != "abc"
    await _drain()


def test_admin_test_endpoint_returns_202_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "admin.routes.workflows.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )
    monkeypatch.setattr(
        "workflow_builder.store.get_workflow",
        AsyncMock(return_value={"id": WF, "name": "Coupon"}),
    )
    monkeypatch.setattr(
        "workflow_builder.test_jobs.start_test_job",
        AsyncMock(return_value={"job_id": "job-1", "stage": "queued"}),
    )
    monkeypatch.setattr("admin.routes.workflows.audit", AsyncMock())

    client = TestClient(_app())
    res = client.post(f"/api/admin/workflows/{BIZ}/ai/{WF}/test")
    assert res.status_code == 202
    assert res.json()["job_id"] == "job-1"


def test_admin_job_polling_returns_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.workflows.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )
    monkeypatch.setattr(
        "workflow_builder.test_jobs.get_test_job",
        AsyncMock(return_value={"job_id": "job-1", "stage": "running", "events_processed": 2}),
    )
    client = TestClient(_app())
    res = client.get(f"/api/admin/workflows/{BIZ}/ai/{WF}/test/job-1")
    assert res.status_code == 200
    assert res.json()["events_processed"] == 2


def test_admin_job_polling_404_for_unknown_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.workflows.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )
    monkeypatch.setattr(
        "workflow_builder.test_jobs.get_test_job",
        AsyncMock(return_value=None),
    )
    client = TestClient(_app(), raise_server_exceptions=False)
    res = client.get(f"/api/admin/workflows/{BIZ}/ai/{WF}/test/nope")
    assert res.status_code == 404


def test_duplicate_admin_start_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "admin.routes.workflows.require_business",
        AsyncMock(return_value={"id": BIZ}),
    )
    monkeypatch.setattr(
        "workflow_builder.store.get_workflow",
        AsyncMock(return_value={"id": WF}),
    )
    monkeypatch.setattr(
        "workflow_builder.test_jobs.start_test_job",
        AsyncMock(side_effect=test_jobs.TestJobRunning("A test is already running")),
    )
    client = TestClient(_app(), raise_server_exceptions=False)
    res = client.post(f"/api/admin/workflows/{BIZ}/ai/{WF}/test")
    assert res.status_code == 409


def _readiness_client(pollers: list[Any]):
    class _Service:
        async def describe_task_queue(self, _request):
            return type("Info", (), {"pollers": pollers})()

    return type("SC", (), {"workflow_service": _Service()})()


class _Poller:
    """Stand-in for a Temporal poller with a protobuf timestamp."""

    def __init__(self, age_seconds: float) -> None:
        seen = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.last_access_time = type(
            "Ts", (), {"ToDatetime": lambda _self, s=seen: s.replace(tzinfo=None)}
        )()


@pytest.mark.asyncio
async def test_readiness_reports_missing_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow_builder import deployer

    client = type("C", (), {"service_client": _readiness_client([])})()
    monkeypatch.setattr(deployer, "_client", AsyncMock(return_value=client))
    result = await deployer.check_temporal_ready()
    assert result["ready"] is False
    assert "No worker is polling" in result["reason"]
    assert "workflow_builder.worker" in result["hint"]


@pytest.mark.asyncio
async def test_readiness_ignores_stale_pollers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporal still lists pollers minutes after a worker exits."""
    from workflow_builder import deployer

    stale = _Poller(deployer.POLLER_FRESH_SECONDS + 120)
    client = type("C", (), {"service_client": _readiness_client([stale])})()
    monkeypatch.setattr(deployer, "_client", AsyncMock(return_value=client))
    result = await deployer.check_temporal_ready()
    assert result["ready"] is False
    assert "No worker is polling" in result["reason"]


@pytest.mark.asyncio
async def test_readiness_accepts_live_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow_builder import deployer

    client = type("C", (), {"service_client": _readiness_client([_Poller(2)])})()
    monkeypatch.setattr(deployer, "_client", AsyncMock(return_value=client))
    result = await deployer.check_temporal_ready()
    assert result["ready"] is True
    assert result["pollers"] == 1


@pytest.mark.asyncio
async def test_readiness_reports_unreachable_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder import deployer

    monkeypatch.setattr(
        deployer,
        "_client",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )
    result = await deployer.check_temporal_ready()
    assert result["ready"] is False
    assert "did not respond" in result["reason"]
