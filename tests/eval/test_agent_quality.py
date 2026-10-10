"""Stage 9 DeepEval faithfulness / relevancy / hallucination (T9.2–T9.4)."""

from __future__ import annotations

import os

import pytest
from deepeval import assert_test
from dotenv import load_dotenv

from tests.eval.dataset import load_eval_cases
from tests.eval.harness import generate_response
from tests.eval.metrics_factory import make_test_case, quality_metrics

load_dotenv()

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]


def _has_gemini() -> bool:
    if os.environ.get("GOOGLE_API_KEY"):
        return True
    try:
        from agent.settings import get_agent_settings

        return bool(get_agent_settings().google_api_key)
    except Exception:
        return False


def _case_ids() -> list[str]:
    return [c["id"] for c in load_eval_cases()]


@pytest.mark.skipif(
    os.environ.get("RUN_EVAL") != "1",
    reason="Set RUN_EVAL=1 to run live DeepEval quality checks",
)
@pytest.mark.parametrize("case_id", _case_ids())
async def test_t9_agent_quality_per_case(case_id: str) -> None:
    assert _has_gemini(), "RUN_EVAL=1 requires GOOGLE_API_KEY"
    case = next(c for c in load_eval_cases() if c["id"] == case_id)
    generated = await generate_response(case)
    test_case = make_test_case(case, generated["actual_output"])
    assert_test(test_case, quality_metrics())
