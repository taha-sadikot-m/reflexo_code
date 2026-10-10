"""Root pytest hooks: keep Gemini eval opt-in."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_EVAL_HINT = "eval tests skipped; run with: pytest tests/eval -m eval"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = (config.option.markexpr or "").strip()
    run_eval = os.getenv("RUN_EVAL") == "1" or (
        bool(markexpr) and "eval" in markexpr and "not eval" not in markexpr
    )
    if run_eval:
        return
    skip_eval = pytest.mark.skip(reason=_EVAL_HINT)
    for item in items:
        if item.get_closest_marker("eval"):
            item.add_marker(skip_eval)
