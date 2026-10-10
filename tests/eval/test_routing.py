"""Stage 9 routing accuracy on the 50 labeled cases (T9.5)."""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from tests.eval.dataset import load_eval_cases
from tests.eval.harness import route_case

load_dotenv()

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

ROUTING_THRESHOLD = 0.85


def _has_gemini() -> bool:
    if os.environ.get("GOOGLE_API_KEY"):
        return True
    try:
        from agent.settings import get_agent_settings

        return bool(get_agent_settings().google_api_key)
    except Exception:
        return False


@pytest.mark.skipif(
    os.environ.get("RUN_EVAL") != "1",
    reason="Set RUN_EVAL=1 to run live routing evaluation",
)
async def test_t9_5_router_accuracy_at_least_85_percent() -> None:
    assert _has_gemini(), "RUN_EVAL=1 requires GOOGLE_API_KEY"
    cases = load_eval_cases()
    mismatches: list[str] = []
    by_category: dict[str, list[str]] = {}
    for case in cases:
        result = await route_case(case)
        got = str(result.get("agent") or "")
        expected = case["expected_agent"]
        if got != expected:
            line = f"{case['id']}: expected {expected}, got {got}"
            mismatches.append(line)
            by_category.setdefault(case["category"], []).append(line)
    accuracy = (len(cases) - len(mismatches)) / len(cases)
    if accuracy < ROUTING_THRESHOLD:
        detail = "\n".join(mismatches)
        cats = "\n".join(f"  {k}: {len(v)}" for k, v in sorted(by_category.items()))
        pytest.fail(
            f"T9.5 routing accuracy {accuracy:.1%} < {ROUTING_THRESHOLD:.0%}\n"
            f"By category:\n{cats}\n{detail}"
        )
    assert accuracy >= ROUTING_THRESHOLD
