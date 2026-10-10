"""Core resolve_identity() — Redis-first identity resolution."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from identity.bot_detection import BotDetector
from identity.cache import IdentityCache
from identity.models import (
    Channel,
    IdentityErrorCode,
    IdentityResolutionError,
    IdentityResult,
)
from shared.db import get_pool
from shared.redis_keys import channel_account_key, identity_key, owner_check_key

logger = logging.getLogger(__name__)

# Same person on IG/FB uses one IGSID/PSID for DMs and comments, but
# identity_map is unique on (business_id, channel, external_id).
_CHANNEL_SIBLINGS: dict[Channel, Channel] = {
    Channel.INSTAGRAM_DM: Channel.INSTAGRAM_COMMENT,
    Channel.INSTAGRAM_COMMENT: Channel.INSTAGRAM_DM,
    Channel.FACEBOOK_MESSENGER: Channel.FACEBOOK_COMMENT,
    Channel.FACEBOOK_COMMENT: Channel.FACEBOOK_MESSENGER,
    # WhatsApp Calling reuses the WhatsApp phone number id and the caller's
    # phone, so voice resolves through the whatsapp row. One-way on purpose:
    # inbound text must not fall back to a voice row.
    Channel.VOICE: Channel.WHATSAPP,
}


def sibling_channel(channel: Channel) -> Channel | None:
    """Channel to retry a lookup against, else None."""
    return _CHANNEL_SIBLINGS.get(channel)


def _normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]+", "", phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


async def resolve_identity(
    channel: str,
    sender_external_id: str,
    receiving_account_id: str,
    sender_display_name: Optional[str] = None,
) -> IdentityResult:
    """
    Resolve which business, whether owner, customer_id, and bot suspicion.
    Called on EVERY inbound message BEFORE agent processing.
    """
    start_time = time.monotonic()

    if not sender_external_id or not str(sender_external_id).strip():
        raise IdentityResolutionError(
            IdentityErrorCode.MISSING_SENDER_ID,
            "sender_external_id is required",
        )

    try:
        channel_enum = Channel(channel)
    except ValueError as e:
        raise IdentityResolutionError(
            IdentityErrorCode.INVALID_CHANNEL,
            f"Unrecognized channel: {channel!r}. "
            f"Valid channels: {[c.value for c in Channel]}",
        ) from e

    sender_external_id = sender_external_id.strip()
    receiving_account_id = receiving_account_id.strip()

    if channel_enum in (Channel.WHATSAPP, Channel.VOICE, Channel.SMS):
        sender_external_id = _normalize_phone(sender_external_id)
    elif channel_enum in (Channel.EMAIL, Channel.GMAIL, Channel.OUTLOOK):
        sender_external_id = sender_external_id.lower()

    cache = IdentityCache()
    pool = get_pool()

    business_id = await _resolve_business_id(
        cache, pool, channel_enum, receiving_account_id
    )

    is_owner = await _check_is_owner(
        cache, pool, business_id, channel_enum, sender_external_id
    )

    if is_owner:
        elapsed_ms = (time.monotonic() - start_time) * 1000
        logger.info(
            "Identity resolved: owner | business=%s channel=%s elapsed=%.1fms",
            business_id,
            channel,
            elapsed_ms,
        )
        return IdentityResult(
            business_id=business_id,
            customer_id=None,
            is_owner=True,
            is_new_customer=False,
            customer_name=sender_display_name,
            is_bot_suspected=False,
        )

    bot_detector = BotDetector()
    is_bot, bot_layers = await bot_detector.check(
        business_id=business_id,
        sender_external_id=sender_external_id,
        channel=channel_enum,
    )

    customer_id, customer_name, is_new = await _resolve_customer(
        cache,
        pool,
        business_id,
        channel_enum,
        sender_external_id,
        sender_display_name,
    )

    elapsed_ms = (time.monotonic() - start_time) * 1000
    logger.info(
        "Identity resolved: %s | business=%s customer=%s new=%s bot=%s elapsed=%.1fms",
        "new_customer" if is_new else "known_customer",
        business_id,
        customer_id,
        is_new,
        is_bot,
        elapsed_ms,
    )

    return IdentityResult(
        business_id=business_id,
        customer_id=customer_id,
        is_owner=False,
        is_new_customer=is_new,
        customer_name=customer_name,
        is_bot_suspected=is_bot,
        bot_detection_layers=bot_layers,
    )


async def _resolve_business_id(
    cache: IdentityCache,
    pool,
    channel: Channel,
    receiving_account_id: str,
) -> str:
    cache_key = channel_account_key(channel.value, receiving_account_id)
    cached = await cache.get(cache_key)
    if cached:
        return cached

    if channel in (Channel.GMAIL, Channel.OUTLOOK, Channel.SLACK):
        try:
            row = await pool.fetchrow(
                """
                SELECT business_id::text
                FROM connected_accounts
                WHERE business_id = $1::uuid
                  AND platform = $2
                  AND is_communication_channel = true
                  AND is_active = true
                """,
                receiving_account_id,
                channel.value,
            )
        except Exception as e:
            raise IdentityResolutionError(
                IdentityErrorCode.IDENTITY_RESOLUTION_FAILED,
                f"Database error during channel account lookup: {e}",
                retryable=True,
            ) from e
        if not row:
            raise IdentityResolutionError(
                IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT,
                f"No active connected account for {channel.value}",
            )
        business_id = row["business_id"]
        await cache.set(cache_key, business_id, ttl=3600)
        return business_id

    if channel == Channel.WEB_WIDGET:
        try:
            row = await pool.fetchrow(
                """
                SELECT id::text AS business_id
                FROM businesses
                WHERE id = $1::uuid AND is_active = true
                """,
                receiving_account_id,
            )
        except Exception as e:
            logger.error("DB error resolving widget business_id: %s", e)
            raise IdentityResolutionError(
                IdentityErrorCode.IDENTITY_RESOLUTION_FAILED,
                f"Database error during business lookup: {e}",
                retryable=True,
            ) from e
        if not row:
            raise IdentityResolutionError(
                IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT,
                f"No active business for web_widget:{receiving_account_id}",
            )
        business_id = row["business_id"]
        await cache.set(cache_key, business_id, ttl=3600)
        return business_id

    lookup_channel = channel
    try:
        row = await pool.fetchrow(
            """
            SELECT business_id::text
            FROM business_channels
            WHERE channel = $1::channel_type
              AND channel_account_id = $2
              AND is_active = true
            LIMIT 1
            """,
            lookup_channel.value,
            receiving_account_id,
        )
        sibling = sibling_channel(channel)
        if not row and sibling is not None:
            lookup_channel = sibling
            row = await pool.fetchrow(
                """
                SELECT business_id::text
                FROM business_channels
                WHERE channel = $1::channel_type
                  AND channel_account_id = $2
                  AND is_active = true
                LIMIT 1
                """,
                lookup_channel.value,
                receiving_account_id,
            )
            if row:
                logger.info(
                    "Resolved %s via sibling channel %s account=%s",
                    channel.value,
                    sibling.value,
                    receiving_account_id,
                )
    except Exception as e:
        logger.error("DB error resolving business_id: %s", e)
        raise IdentityResolutionError(
            IdentityErrorCode.IDENTITY_RESOLUTION_FAILED,
            f"Database error during business lookup: {e}",
            retryable=True,
        ) from e

    if not row:
        raise IdentityResolutionError(
            IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT,
            f"No active business_channels row for {channel.value}:{receiving_account_id}",
        )

    business_id = row["business_id"]
    await cache.set(cache_key, business_id, ttl=3600)
    return business_id


async def _check_is_owner(
    cache: IdentityCache,
    pool,
    business_id: str,
    channel: Channel,
    sender_external_id: str,
) -> bool:
    cache_key = owner_check_key(business_id, channel.value, sender_external_id)
    cached = await cache.get(cache_key)
    if cached == "1":
        return True
    if cached == "0":
        return False

    is_owner = False
    try:
        if channel in (Channel.WHATSAPP, Channel.VOICE, Channel.SMS):
            owner_phone = await pool.fetchval(
                "SELECT owner_phone FROM businesses WHERE id = $1::uuid AND is_active = true",
                business_id,
            )
            if owner_phone and _normalize_phone(sender_external_id) == _normalize_phone(
                owner_phone
            ):
                is_owner = True

        elif channel == Channel.EMAIL:
            owner_email = await pool.fetchval(
                "SELECT owner_email FROM businesses WHERE id = $1::uuid AND is_active = true",
                business_id,
            )
            if (
                owner_email
                and sender_external_id.lower().strip() == owner_email.lower().strip()
            ):
                is_owner = True

        elif channel in (
            Channel.INSTAGRAM_DM,
            Channel.INSTAGRAM_COMMENT,
            Channel.FACEBOOK_MESSENGER,
            Channel.FACEBOOK_COMMENT,
        ):
            match = await pool.fetchval(
                """
                SELECT 1
                FROM identity_map im
                JOIN customers c ON c.id = im.customer_id
                JOIN businesses b ON b.id = $1::uuid
                WHERE im.business_id = $1::uuid
                  AND im.channel = $2::channel_type
                  AND im.external_id = $3
                  AND (
                      c.phone = b.owner_phone
                      OR (b.owner_email IS NOT NULL AND c.email = b.owner_email)
                  )
                """,
                business_id,
                channel.value,
                sender_external_id,
            )
            if match:
                is_owner = True

    except Exception as e:
        logger.error("DB error during owner check: %s", e)
        return False

    await cache.set(cache_key, "1" if is_owner else "0", ttl=3600 if is_owner else 1800)
    return is_owner


async def _resolve_customer(
    cache: IdentityCache,
    pool,
    business_id: str,
    channel: Channel,
    sender_external_id: str,
    sender_display_name: Optional[str],
) -> tuple[str, Optional[str], bool]:
    cache_key = identity_key(channel.value, sender_external_id, business_id)
    cached_json = await cache.get(cache_key)
    if cached_json:
        try:
            data = json.loads(cached_json)
            return data["customer_id"], data.get("customer_name"), False
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupted identity cache for key=%s", cache_key)

    try:
        row = await pool.fetchrow(
            """
            SELECT im.customer_id::text, c.name AS customer_name
            FROM identity_map im
            JOIN customers c ON c.id = im.customer_id
            WHERE im.business_id = $1::uuid
              AND im.channel = $2::channel_type
              AND im.external_id = $3
            LIMIT 1
            """,
            business_id,
            channel.value,
            sender_external_id,
        )
    except Exception as e:
        logger.error("DB error resolving customer: %s", e)
        raise IdentityResolutionError(
            IdentityErrorCode.IDENTITY_RESOLUTION_FAILED,
            f"Database error during customer lookup: {e}",
            retryable=True,
        ) from e

    if not row:
        sibling = sibling_channel(channel)
        if sibling is not None:
            try:
                row = await pool.fetchrow(
                    """
                    SELECT im.customer_id::text, c.name AS customer_name
                    FROM identity_map im
                    JOIN customers c ON c.id = im.customer_id
                    WHERE im.business_id = $1::uuid
                      AND im.channel = $2::channel_type
                      AND im.external_id = $3
                    LIMIT 1
                    """,
                    business_id,
                    sibling.value,
                    sender_external_id,
                )
            except Exception as e:
                logger.error("DB error resolving sibling identity: %s", e)
                row = None
            if row:
                try:
                    await pool.execute(
                        """
                        INSERT INTO identity_map (
                            business_id, customer_id, channel, external_id, display_name
                        )
                        VALUES ($1::uuid, $2::uuid, $3::channel_type, $4, $5)
                        ON CONFLICT (business_id, channel, external_id) DO NOTHING
                        """,
                        business_id,
                        row["customer_id"],
                        channel.value,
                        sender_external_id,
                        sender_display_name,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to attach sibling identity mapping: %s", exc
                    )

    if row:
        customer_id = row["customer_id"]
        customer_name = row["customer_name"] or sender_display_name
        try:
            await pool.execute(
                "UPDATE customers SET last_seen_at = NOW() WHERE id = $1::uuid",
                customer_id,
            )
        except Exception:
            pass
        if sender_display_name and sender_display_name != row.get("customer_name"):
            try:
                await pool.execute(
                    """
                    UPDATE identity_map SET display_name = $4
                    WHERE business_id = $1::uuid AND channel = $2::channel_type
                      AND external_id = $3
                      AND (display_name IS NULL OR display_name != $4)
                    """,
                    business_id,
                    channel.value,
                    sender_external_id,
                    sender_display_name,
                )
            except Exception:
                pass

        await cache.set(
            cache_key,
            json.dumps({"customer_id": customer_id, "customer_name": customer_name}),
            ttl=3600,
        )
        return customer_id, customer_name, False

    return await _create_new_customer(
        pool,
        cache,
        business_id,
        channel,
        sender_external_id,
        sender_display_name,
        cache_key,
    )


async def _create_new_customer(
    pool,
    cache: IdentityCache,
    business_id: str,
    channel: Channel,
    sender_external_id: str,
    sender_display_name: Optional[str],
    identity_cache_key: str,
) -> tuple[str, Optional[str], bool]:
    phone = None
    email = None
    if channel in (Channel.WHATSAPP, Channel.VOICE, Channel.SMS):
        phone = sender_external_id
    elif channel in (Channel.EMAIL, Channel.GMAIL, Channel.OUTLOOK):
        email = sender_external_id

    customer_name = sender_display_name

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                customer_id = await conn.fetchval(
                    """
                    INSERT INTO customers (
                        business_id, name, phone, email,
                        primary_channel, first_seen_at, last_seen_at
                    )
                    VALUES ($1::uuid, $2, $3, $4, $5::channel_type, NOW(), NOW())
                    RETURNING id::text
                    """,
                    business_id,
                    customer_name,
                    phone,
                    email,
                    channel.value,
                )
                await conn.execute(
                    """
                    INSERT INTO identity_map (
                        business_id, customer_id, channel, external_id, display_name
                    )
                    VALUES ($1::uuid, $2::uuid, $3::channel_type, $4, $5)
                    ON CONFLICT (business_id, channel, external_id) DO NOTHING
                    """,
                    business_id,
                    customer_id,
                    channel.value,
                    sender_external_id,
                    sender_display_name,
                )
    except Exception as e:
        logger.error("Failed to create new customer: %s", e)
        try:
            row = await pool.fetchrow(
                """
                SELECT im.customer_id::text, c.name AS customer_name
                FROM identity_map im
                JOIN customers c ON c.id = im.customer_id
                WHERE im.business_id = $1::uuid AND im.channel = $2::channel_type
                  AND im.external_id = $3
                """,
                business_id,
                channel.value,
                sender_external_id,
            )
            if row:
                customer_id = row["customer_id"]
                customer_name = row["customer_name"]
                await cache.set(
                    identity_cache_key,
                    json.dumps(
                        {"customer_id": customer_id, "customer_name": customer_name}
                    ),
                    ttl=3600,
                )
                return customer_id, customer_name, False
        except Exception:
            pass
        raise IdentityResolutionError(
            IdentityErrorCode.CUSTOMER_CREATION_FAILED,
            f"Could not create customer: {e}",
            retryable=True,
        ) from e

    # After race with ON CONFLICT DO NOTHING, re-read map in case another won
    mapped = await pool.fetchval(
        """
        SELECT customer_id::text FROM identity_map
        WHERE business_id = $1::uuid AND channel = $2::channel_type AND external_id = $3
        """,
        business_id,
        channel.value,
        sender_external_id,
    )
    if mapped and mapped != customer_id:
        customer_id = mapped

    await cache.set(
        identity_cache_key,
        json.dumps({"customer_id": customer_id, "customer_name": customer_name}),
        ttl=3600,
    )
    return customer_id, customer_name, True
