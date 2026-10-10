"""T12.3: production packages must not import the Nango client."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DIRS = (
    "admin",
    "agent",
    "api",
    "channel_framework",
    "identity",
    "mcp_servers",
    "onboarding",
    "shared",
    "voice",
    "workflow_builder",
)


def test_production_python_has_no_nango_client_import():
    offenders = []
    for directory in PRODUCTION_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "integrations.nango_client" in text or "from integrations.nango_client" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_nango_runtime_removed_from_compose_and_core():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "nango-server" not in compose
    assert "nango-db" not in compose
    core = (ROOT / "mcp_servers" / "reflexo_core" / "server.py").read_text(encoding="utf-8")
    assert "init_nango_client" not in core
    assert "register_nango" not in core
