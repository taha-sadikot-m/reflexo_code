"""Structured pipeline traces for the admin Test Simulator."""

from __future__ import annotations

import time
from typing import Any


class PipelineTrace:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.steps: list[dict[str, Any]] = []

    def add(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: str = "",
        data: dict[str, Any] | None = None,
        ms: float | None = None,
    ) -> None:
        self.steps.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "data": data or {},
                # None (not 0) when a stage is not measured, so the UI can omit
                # the timing instead of implying it took no time.
                "ms": round(ms, 2) if ms is not None else None,
            }
        )

    @property
    def total_ms(self) -> float:
        return round((time.monotonic() - self.started) * 1000, 2)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "total_ms": self.total_ms}

    def to_visual_dict(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for i, step in enumerate(self.steps):
            nid = f"n{i}"
            nodes.append(
                {
                    "id": nid,
                    "type": "trace",
                    "position": {"x": i * 220, "y": 40},
                    "data": {
                        "label": step["name"],
                        "status": step["status"],
                        "detail": step["detail"],
                        "ms": step["ms"],
                        "payload": step["data"],
                    },
                }
            )
            if i:
                edges.append(
                    {
                        "id": f"e{i-1}-{i}",
                        "source": f"n{i-1}",
                        "target": nid,
                    }
                )
        return {"nodes": nodes, "edges": edges, "total_ms": self.total_ms}
