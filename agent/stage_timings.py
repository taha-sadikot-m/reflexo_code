"""Ordered per-stage timings for one pipeline turn.

The simulator used to display a hardcoded 0ms for every stage because nothing
measured them. Stages are recorded as the turn progresses, so a slow or failing
stage is visible instead of inferred.
"""

from __future__ import annotations

import time
from typing import Any


class StageTimings:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self._last = self._started
        self._stages: list[dict[str, Any]] = []

    def mark(self, name: str, *, status: str = "ok", detail: str = "") -> float:
        """Close the current stage and return its duration in milliseconds."""
        now = time.monotonic()
        ms = round((now - self._last) * 1000, 2)
        self._last = now
        self._stages.append(
            {"name": name, "ms": ms, "status": status, "detail": detail}
        )
        return ms

    @property
    def total_ms(self) -> float:
        return round((time.monotonic() - self._started) * 1000, 2)

    def to_list(self) -> list[dict[str, Any]]:
        return [dict(stage) for stage in self._stages]

    def as_map(self) -> dict[str, float]:
        """Stage name to milliseconds, summing stages recorded more than once."""
        totals: dict[str, float] = {}
        for stage in self._stages:
            totals[str(stage["name"])] = round(
                totals.get(str(stage["name"]), 0.0) + float(stage["ms"]), 2
            )
        return totals


def timings_map(stage_timings: list[dict[str, Any]] | None) -> dict[str, float]:
    """Rebuild the name-to-ms map from a serialized stage list."""
    totals: dict[str, float] = {}
    for stage in stage_timings or []:
        name = str(stage.get("name") or "")
        if not name:
            continue
        try:
            ms = float(stage.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0.0
        totals[name] = round(totals.get(name, 0.0) + ms, 2)
    return totals
