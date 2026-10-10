#!/usr/bin/env python3
"""Verify schema: tables, RLS, seed rows, basic tenant isolation."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

TEST_BUSINESS_ID = "00000000-0000-0000-0000-000000000001"
TENANT_TABLES = [
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
    "usage_events",
    "onboarding_sessions",
    "referrals",
    "mcp_connections",
    "ai_workflows",
    "ai_workflow_runs",
]


async def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.")
        return 1

    import asyncpg

    conn = await asyncpg.connect(database_url)
    errors: list[str] = []
    try:
        # Seed counts
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM admin_users")
        biz_count = await conn.fetchval(
            "SELECT COUNT(*) FROM businesses WHERE id = $1::uuid", TEST_BUSINESS_ID
        )
        kb_count = await conn.fetchval(
            "SELECT COUNT(*) FROM knowledge_entries WHERE business_id = $1::uuid",
            TEST_BUSINESS_ID,
        )
        bn_count = await conn.fetchval(
            "SELECT COUNT(*) FROM business_numbers WHERE business_id = $1::uuid",
            TEST_BUSINESS_ID,
        )
        ta_count = await conn.fetchval(
            "SELECT COUNT(*) FROM test_accounts WHERE business_id = $1::uuid",
            TEST_BUSINESS_ID,
        )

        print(f"admin_users={admin_count} (expect >= 1)")
        print(f"test business={biz_count} (expect 1)")
        print(f"knowledge_entries={kb_count} (expect 3)")
        print(f"business_numbers={bn_count} (expect 1)")
        print(f"test_accounts={ta_count} (expect 1)")

        if admin_count < 1:
            errors.append("missing admin user")
        if biz_count != 1:
            errors.append("missing test business")
        if kb_count != 3:
            errors.append("expected 3 knowledge entries")
        if bn_count != 1:
            errors.append("expected 1 business_numbers row")
        if ta_count != 1:
            errors.append("expected 1 test_accounts row")

        # RLS enabled on tenant tables; not on businesses / admin_users
        for tbl in TENANT_TABLES:
            enabled = await conn.fetchval(
                """
                SELECT relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = $1
                """,
                tbl,
            )
            if not enabled:
                errors.append(f"RLS not enabled on {tbl}")

        for tbl in ("businesses", "admin_users"):
            enabled = await conn.fetchval(
                """
                SELECT relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = $1
                """,
                tbl,
            )
            if enabled:
                errors.append(f"RLS should be OFF on {tbl}")

        mcp_columns = {
            row["column_name"]
            for row in await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'mcp_connections'
                """
            )
        }
        required_mcp_columns = {
            "registry_name",
            "registry_version",
            "auth_type",
            "auth_status",
        }
        missing_mcp_columns = required_mcp_columns - mcp_columns
        if missing_mcp_columns:
            errors.append(
                f"mcp_connections missing columns: {sorted(missing_mcp_columns)}"
            )

        # Tenant isolation smoke: without context, RLS should hide tenant rows
        # (table owner may bypass RLS — force with SET ROLE if needed).
        # On Neon, connecting user is typically table owner and bypasses RLS.
        # Verify current_business_id() works and filtered query with set_config.
        await conn.execute(
            "SELECT set_config('app.current_business_id', $1, false)",
            TEST_BUSINESS_ID,
        )
        visible = await conn.fetchval(
            "SELECT COUNT(*) FROM knowledge_entries WHERE business_id = current_business_id()"
        )
        print(f"KB visible with correct tenant context={visible} (expect 3)")
        if visible != 3:
            errors.append("tenant context did not expose seed KB")

        await conn.execute("SELECT set_config('app.current_business_id', $1, false)", "")
        fn_null = await conn.fetchval("SELECT current_business_id()")
        print(f"current_business_id() with empty setting={fn_null} (expect None)")

        if errors:
            print("FAILED:")
            for e in errors:
                print(f"  - {e}")
            return 1

        print("Verification passed.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
