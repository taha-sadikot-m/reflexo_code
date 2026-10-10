"""Direct Google Calendar client using Channel Framework credentials."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from channel_framework.credentials import get_valid_tokens

BASE = "https://www.googleapis.com/calendar/v3"


async def _request(
    business_id: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    tokens = await get_valid_tokens(business_id, "google_calendar")
    if not tokens:
        # Gmail OAuth can carry Calendar scopes in deployments using one Google account.
        tokens = await get_valid_tokens(business_id, "gmail")
    if not tokens:
        raise RuntimeError("Google Calendar is not connected")
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {tokens['access_token']}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(method, f"{BASE}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}


async def list_events(
    business_id: str,
    time_min: str,
    time_max: str,
    max_results: int = 20,
) -> dict[str, Any]:
    return await _request(
        business_id,
        "GET",
        "/calendars/primary/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 100)),
        },
    )


async def create_event(
    business_id: str,
    summary: str,
    start: str,
    end: str,
    attendee_email: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]
    if description:
        event["description"] = description
    return await _request(
        business_id,
        "POST",
        "/calendars/primary/events",
        params={"sendUpdates": "all"},
        json=event,
    )


async def check_availability(
    business_id: str,
    date: str,
    duration_minutes: int = 30,
    business_hours_start: str = "09:00",
    business_hours_end: str = "18:00",
) -> dict[str, Any]:
    start = datetime.fromisoformat(f"{date}T{business_hours_start}:00+05:30")
    end = datetime.fromisoformat(f"{date}T{business_hours_end}:00+05:30")
    result = await list_events(business_id, start.isoformat(), end.isoformat(), 100)
    busy = []
    for event in result.get("items") or []:
        event_start = (event.get("start") or {}).get("dateTime")
        event_end = (event.get("end") or {}).get("dateTime")
        if event_start and event_end:
            busy.append((datetime.fromisoformat(event_start), datetime.fromisoformat(event_end)))
    slots = []
    cursor = start
    duration = timedelta(minutes=max(5, duration_minutes))
    while cursor + duration <= end:
        slot_end = cursor + duration
        if not any(cursor < busy_end and slot_end > busy_start for busy_start, busy_end in busy):
            slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
        cursor = slot_end
    return {"date": date, "available_slots": slots}
