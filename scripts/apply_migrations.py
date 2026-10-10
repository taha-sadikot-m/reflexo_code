#!/usr/bin/env python3
"""Apply numbered SQL migrations in db/migrations/ against DATABASE_URL."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

MIGRATIONS_DIR = ROOT / "db" / "migrations"

# Expected application tables after a full migrate (excludes schema_migrations)
EXPECTED_TABLES = {
    "businesses",
    "business_channels",
    "customers",
    "identity_map",
    "business_numbers",
    "knowledge_entries",
    "structured_entities",
    "documents",
    "conversations",
    "messages",
    "workflow_rules",
    "workflow_executions",
    "knowledge_gaps",
    "test_accounts",
    "admin_users",
    "usage_events",
    "onboarding_sessions",
    "referrals",
    "integration_setup_links",
    "mcp_connections",
    "ai_workflows",
    "ai_workflow_runs",
    "workflow_studio_sessions",
    "whatsapp_templates",
    "connected_accounts",
    "webhook_subscriptions",
    "channel_messages",
}


async def ensure_migrations_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def applied_filenames(conn) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {r["filename"] for r in rows}


def migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return files


async def apply_one(conn, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES ($1)",
            path.name,
        )
    print(f"  applied {path.name}")


async def list_public_tables(conn) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        """
    )
    return {r["tablename"] for r in rows}


async def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
        return 1

    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        await ensure_migrations_table(conn)
        done = await applied_filenames(conn)
        pending = [p for p in migration_files() if p.name not in done]

        if not pending:
            print("No pending migrations.")
        else:
            print(f"Applying {len(pending)} migration(s)...")
            for path in pending:
                try:
                    await apply_one(conn, path)
                except Exception as exc:
                    print(f"FAILED on {path.name}: {exc}")
                    return 1
            print("Migrations complete.")

        tables = await list_public_tables(conn)
        missing = EXPECTED_TABLES - tables
        if missing:
            print(f"WARNING: expected tables missing: {sorted(missing)}")
            return 1

        extras = tables - EXPECTED_TABLES - {"schema_migrations"}
        if extras:
            print(f"NOTE: unexpected public tables present: {sorted(extras)}")

        print(f"OK — {len(EXPECTED_TABLES)} application tables present.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
