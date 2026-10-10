"""Unhandled errors must stay inside the CORS layer so browsers see the real status."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import JsonErrorMiddleware, create_app

ORIGIN = "http://localhost:3000"


def _boom_app() -> FastAPI:
    app = create_app()

    @app.get("/__boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("synthetic failure")

    return app


def test_unhandled_error_returns_json_with_cors_header() -> None:
    client = TestClient(_boom_app(), raise_server_exceptions=False)
    res = client.get("/__boom", headers={"Origin": ORIGIN})
    assert res.status_code == 500
    assert res.headers.get("access-control-allow-origin") == ORIGIN
    assert res.json()["detail"] == "Internal Server Error"


def test_error_middleware_is_inside_cors_middleware() -> None:
    from fastapi.middleware.cors import CORSMiddleware

    classes = [m.cls for m in create_app().user_middleware]
    assert classes.index(CORSMiddleware) < classes.index(JsonErrorMiddleware)
