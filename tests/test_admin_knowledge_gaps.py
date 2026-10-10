"""Admin knowledge gap resolve/ignore and safe list columns."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.auth import AdminUser, get_current_admin
from admin.knowledge_ops import GAP_LIST_COLUMNS

BIZ = "00000000-0000-0000-0000-000000000001"
GAP_OPEN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
GAP_DONE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _user(role: str) -> AdminUser:
    return AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email=f"{role}@reflexo.ai",
        name=role.title(),
        role=role,
        is_active=True,
    )


class GapPool:
    def __init__(self) -> None:
        self.gaps: dict[str, dict[str, Any]] = {
            GAP_OPEN: {
                "id": GAP_OPEN,
                "question": "Do you deliver to Pune?",
                "status": "open",
                "answer": None,
                "knowledge_entry_id": None,
            },
            GAP_DONE: {
                "id": GAP_DONE,
                "question": "Already answered",
                "status": "resolved",
                "answer": "Yes",
                "knowledge_entry_id": "e1",
            },
        }
        self.entries: list[dict[str, Any]] = []
        self.executed: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(sql.split()).lower()
        if "from businesses where id" in q:
            return {"id": BIZ, "name": "Shop"}
        if "from knowledge_gaps" in q:
            return self.gaps.get(str(args[0]))
        if "insert into knowledge_entries" in q:
            row = {
                "id": "entry-new",
                "question": args[1],
                "answer": args[2],
                "category": args[3],
                "source": args[5],
            }
            self.entries.append(row)
            return row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(sql.split()).lower()
        self.executed.append(q)
        if "from knowledge_gaps" in q:
            assert "embedding" not in q
            wanted = None
            if "status::text = $2" in q and len(args) > 1:
                wanted = str(args[1])
            rows = []
            for gap in self.gaps.values():
                if wanted and gap["status"] != wanted:
                    continue
                rows.append(
                    {
                        "id": gap["id"],
                        "question": gap["question"],
                        "context": None,
                        "status": gap["status"],
                        "occurrence_count": 2,
                        "conversation_id": None,
                        "last_occurred_at": None,
                        "created_at": None,
                        "answer": gap["answer"],
                        "knowledge_entry_id": gap.get("knowledge_entry_id"),
                    }
                )
            return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        q = " ".join(sql.split()).lower()
        self.executed.append(q)
        if "update knowledge_gaps" in q:
            gap_id = str(args[2] if "knowledge_entry_id" in q else args[0])
            if "status = 'resolved'" in q:
                gap_id = str(args[2])
                self.gaps[gap_id]["status"] = "resolved"
                self.gaps[gap_id]["answer"] = args[0]
                self.gaps[gap_id]["knowledge_entry_id"] = str(args[1])
            elif "status = 'ignored'" in q:
                self.gaps[str(args[0])]["status"] = "ignored"
            return "UPDATE 1"
        return "UPDATE 1"


def _admin_app(pool: GapPool, role: str = "admin") -> FastAPI:
    from admin.routes.knowledge import router as kr

    app = FastAPI()
    app.include_router(kr)
    app.dependency_overrides[get_current_admin] = lambda: _user(role)
    return app


def test_gaps_list_sql_omits_embedding():
    assert "embedding" not in GAP_LIST_COLUMNS.lower()
    assert "question" in GAP_LIST_COLUMNS
    assert "occurrence_count" in GAP_LIST_COLUMNS


def test_support_cannot_resolve_gap(monkeypatch):
    from admin.routes.knowledge import router as kr

    app = FastAPI()
    app.include_router(kr)
    app.dependency_overrides[get_current_admin] = lambda: _user("support")
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/knowledge/{BIZ}/gaps/{GAP_OPEN}/resolve",
        json={"answer": "Yes, we deliver to Pune."},
    )
    assert res.status_code == 403


def test_resolve_gap_creates_kb_entry(monkeypatch):
    pool = GapPool()
    app = _admin_app(pool)
    monkeypatch.setattr("admin.routes.knowledge.require_business", AsyncMock(return_value={"id": BIZ}))
    monkeypatch.setattr("admin.routes.knowledge.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.knowledge.audit", AsyncMock())
    monkeypatch.setattr("admin.knowledge_ops.embed", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr("admin.knowledge_ops.vec_literal", lambda _v: "[0.1,0.2]")
    monkeypatch.setattr("admin.knowledge_ops.cache_delete_prefix", AsyncMock())
    client = TestClient(app)
    res = client.post(
        f"/api/admin/knowledge/{BIZ}/gaps/{GAP_OPEN}/resolve",
        json={"answer": "Yes, we deliver to Pune within 2 days.", "category": "policy"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "resolved"
    assert body["knowledge_entry_id"] == "entry-new"
    assert pool.gaps[GAP_OPEN]["status"] == "resolved"
    assert pool.entries[0]["answer"] == "Yes, we deliver to Pune within 2 days."
    assert pool.entries[0]["source"] == "admin"


def test_resolve_already_resolved_is_409(monkeypatch):
    pool = GapPool()
    app = _admin_app(pool)
    monkeypatch.setattr("admin.routes.knowledge.require_business", AsyncMock(return_value={"id": BIZ}))
    monkeypatch.setattr("admin.routes.knowledge.get_admin_pool", AsyncMock(return_value=pool))
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        f"/api/admin/knowledge/{BIZ}/gaps/{GAP_DONE}/resolve",
        json={"answer": "Nope"},
    )
    assert res.status_code == 409


def test_ignore_gap(monkeypatch):
    pool = GapPool()
    app = _admin_app(pool)
    monkeypatch.setattr("admin.routes.knowledge.require_business", AsyncMock(return_value={"id": BIZ}))
    monkeypatch.setattr("admin.routes.knowledge.get_admin_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("admin.routes.knowledge.audit", AsyncMock())
    client = TestClient(app)
    res = client.post(f"/api/admin/knowledge/{BIZ}/gaps/{GAP_OPEN}/ignore")
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert pool.gaps[GAP_OPEN]["status"] == "ignored"


def test_gaps_list_defaults_to_open(monkeypatch):
    from admin.routes.conversations import router as cr

    pool = GapPool()
    app = FastAPI()
    app.include_router(cr)
    app.dependency_overrides[get_current_admin] = lambda: _user("support")
    monkeypatch.setattr("admin.routes.conversations.require_business", AsyncMock(return_value={"id": BIZ}))
    monkeypatch.setattr("admin.routes.conversations.get_admin_pool", AsyncMock(return_value=pool))
    client = TestClient(app)
    res = client.get(f"/api/admin/conversations/{BIZ}/gaps")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == GAP_OPEN
    assert "embedding" not in items[0]
