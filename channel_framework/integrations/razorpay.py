"""Direct Razorpay API client using per-business Basic Auth credentials."""

from __future__ import annotations

from typing import Any

import httpx

from channel_framework.credentials import load_tokens

BASE_URL = "https://api.razorpay.com/v1"


async def _request(
    business_id: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    credentials = await load_tokens(business_id, "razorpay")
    if not credentials or not credentials.get("refresh_token"):
        raise RuntimeError("Razorpay is not connected")
    async with httpx.AsyncClient(
        timeout=30.0,
        auth=(credentials["access_token"], credentials["refresh_token"]),
    ) as client:
        response = await client.request(
            method,
            f"{BASE_URL}/{path.lstrip('/')}",
            json=json_body,
        )
        response.raise_for_status()
        return response.json()


async def create_payment_link(
    business_id: str,
    *,
    amount_paise: int,
    description: str,
    customer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    return await _request(
        business_id,
        "POST",
        "payment_links",
        json_body={
            "amount": amount_paise,
            "currency": "INR",
            "description": description,
            "customer": customer or {},
        },
    )


async def get_payment_link_status(business_id: str, link_id: str) -> dict[str, Any]:
    return await _request(business_id, "GET", f"payment_links/{link_id}")


async def check_payment_status(business_id: str, payment_id: str) -> dict[str, Any]:
    return await _request(business_id, "GET", f"payments/{payment_id}")


async def create_order(
    business_id: str,
    *,
    amount_paise: int,
    receipt: str,
) -> dict[str, Any]:
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    return await _request(
        business_id,
        "POST",
        "orders",
        json_body={
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
        },
    )


async def initiate_refund(
    business_id: str,
    payment_id: str,
    *,
    amount_paise: int | None = None,
) -> dict[str, Any]:
    body = {"amount": amount_paise} if amount_paise is not None else {}
    return await _request(
        business_id,
        "POST",
        f"payments/{payment_id}/refund",
        json_body=body,
    )
