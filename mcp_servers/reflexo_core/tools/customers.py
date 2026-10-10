"""Customer management tools."""

from __future__ import annotations

import json
from typing import Any

from mcp_servers.reflexo_core.utils.db_helpers import business_exists, row_to_dict, tenant_connection
from mcp_servers.reflexo_core.utils.errors import tool_error


async def classify_contact_impl(
    business_id: str,
    customer_id: str,
    contact_type: str,
) -> dict[str, Any] | None:
    normalized = contact_type.strip().lower()
    if normalized not in {"unknown", "personal", "business"}:
        raise ValueError("contact_type must be unknown, personal, or business")
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE customers
            SET contact_type = $3, updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            RETURNING id, contact_type
            """,
            customer_id,
            business_id,
            normalized,
        )
    return row_to_dict(row) if row else None


async def get_contact_type_impl(
    business_id: str,
    customer_id: str,
) -> dict[str, Any] | None:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, contact_type
            FROM customers
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            customer_id,
            business_id,
        )
    return row_to_dict(row) if row else None


async def list_personal_contacts_impl(
    business_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    async with tenant_connection(business_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, phone, email, contact_type, last_seen_at
            FROM customers
            WHERE business_id = $1::uuid AND contact_type = 'personal'
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT $2
            """,
            business_id,
            max(1, min(limit, 500)),
        )
    return [row_to_dict(row) for row in rows]


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_customer(business_id: str, customer_id: str) -> dict[str, Any]:
        """Get full customer profile including channel identities."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM customers WHERE id = $1::uuid AND business_id = $2::uuid",
                customer_id,
                business_id,
            )
            if not row:
                return tool_error("ENTRY_NOT_FOUND", f"Customer {customer_id} not found")
            channels = await conn.fetch(
                """
                SELECT channel FROM identity_map
                WHERE customer_id = $1::uuid AND business_id = $2::uuid
                """,
                customer_id,
                business_id,
            )

        data = row_to_dict(row)
        data["channels"] = [c["channel"] for c in channels]
        prefs = data.get("preferences")
        if isinstance(prefs, str):
            data["preferences"] = json.loads(prefs)
        return data

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def search_customers(
        business_id: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search customers by name, phone, email, or tags (trigram / array)."""
        if not query or not query.strip():
            return tool_error("EMPTY_QUERY", "query is empty string")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        limit = max(1, min(limit, 50))
        q = query.strip()
        async with tenant_connection(business_id) as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM customers
                WHERE business_id = $1::uuid
                  AND (
                    name ILIKE '%' || $2 || '%'
                    OR phone ILIKE '%' || $2 || '%'
                    OR email ILIKE '%' || $2 || '%'
                    OR $2 = ANY(tags)
                    OR similarity(COALESCE(name, ''), $2) > 0.3
                  )
                ORDER BY last_seen_at DESC
                LIMIT $3
                """,
                business_id,
                q,
                limit,
            )
        customers = []
        for r in rows:
            d = row_to_dict(r)
            if isinstance(d.get("preferences"), str):
                d["preferences"] = json.loads(d["preferences"])
            customers.append(d)
        return {"customers": customers, "total_count": len(customers)}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def update_customer(
        business_id: str,
        customer_id: str,
        name: str | None = None,
        email: str | None = None,
        preferences: dict | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Update customer profile. Merges preferences; replaces tags if provided."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM customers WHERE id = $1::uuid AND business_id = $2::uuid",
                customer_id,
                business_id,
            )
            if not existing:
                return tool_error("ENTRY_NOT_FOUND", f"Customer {customer_id} not found")

            new_name = name if name is not None else existing["name"]
            new_email = email if email is not None else existing["email"]
            new_notes = notes if notes is not None else existing["notes"]
            new_tags = tags if tags is not None else list(existing["tags"] or [])

            old_prefs = existing["preferences"]
            if isinstance(old_prefs, str):
                old_prefs = json.loads(old_prefs)
            old_prefs = old_prefs or {}
            if preferences is not None:
                new_prefs = {**old_prefs, **preferences}
            else:
                new_prefs = old_prefs

            await conn.execute(
                """
                UPDATE customers
                SET name = $1, email = $2, notes = $3, tags = $4::text[],
                    preferences = $5::jsonb, updated_at = now()
                WHERE id = $6::uuid AND business_id = $7::uuid
                """,
                new_name,
                new_email,
                new_notes,
                new_tags,
                json.dumps(new_prefs),
                customer_id,
                business_id,
            )

        return {"id": customer_id, "status": "updated"}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def link_owner_channel_identity(
        business_id: str,
        channel: str,
        external_id: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Link an Instagram/Facebook sender ID (IGSID/PSID) to the business owner.

        After this, DMs from that external_id resolve as is_owner=true so the
        owner can continue onboarding / management from IG or FB Messenger.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            from identity.owner_link import link_owner_channel_identity as link_fn

            return await link_fn(
                business_id,
                channel,
                external_id,
                display_name=display_name,
            )
        except ValueError as exc:
            return tool_error("INVALID_INPUT", str(exc))
        except Exception as exc:
            return tool_error("LINK_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def classify_contact(
        business_id: str,
        customer_id: str,
        contact_type: str,
    ) -> dict[str, Any]:
        """Classify a Smart Number contact as personal, business, or unknown."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        try:
            row = await classify_contact_impl(
                business_id,
                customer_id,
                contact_type,
            )
        except ValueError as exc:
            return tool_error("INVALID_INPUT", str(exc))
        if not row:
            return tool_error("ENTRY_NOT_FOUND", f"Customer {customer_id} not found")
        return row

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_contact_type(
        business_id: str,
        customer_id: str,
    ) -> dict[str, Any]:
        """Read a customer's Smart Number classification."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        row = await get_contact_type_impl(business_id, customer_id)
        if not row:
            return tool_error("ENTRY_NOT_FOUND", f"Customer {customer_id} not found")
        return row

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def list_personal_contacts(
        business_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List contacts excluded from business automations."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        contacts = await list_personal_contacts_impl(
            business_id,
            limit=limit,
        )
        return {"contacts": contacts, "total_count": len(contacts)}
