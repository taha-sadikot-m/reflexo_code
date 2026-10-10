"""DeepEval judge metrics for Stage 9 (Gemini)."""

from __future__ import annotations

from deepeval.metrics import (
    AnswerRelevancyMetric,
    FaithfulnessMetric,
    HallucinationMetric,
)
from deepeval.models import GeminiModel
from deepeval.test_case import LLMTestCase

from tests.eval.dataset import EvalCase

FAITHFULNESS_THRESHOLD = 0.7
RELEVANCY_THRESHOLD = 0.7
# DeepEval 4.x flipped HallucinationMetric: higher = more factual alignment
# (warn_score_direction_flipped). Success is score >= threshold, same as the others.
HALLUCINATION_THRESHOLD = 0.5
JUDGE_MODEL = "gemini-2.5-flash"


def gemini_judge() -> GeminiModel:
    from agent.settings import get_agent_settings

    settings = get_agent_settings()
    return GeminiModel(model=JUDGE_MODEL, api_key=settings.google_api_key or None)


def quality_metrics() -> list:
    model = gemini_judge()
    return [
        FaithfulnessMetric(threshold=FAITHFULNESS_THRESHOLD, model=model, include_reason=True),
        AnswerRelevancyMetric(threshold=RELEVANCY_THRESHOLD, model=model, include_reason=True),
        HallucinationMetric(threshold=HALLUCINATION_THRESHOLD, model=model, include_reason=True),
    ]


def make_test_case(case: EvalCase, actual_output: str) -> LLMTestCase:
    facts = list(case["retrieval_context"])
    kwargs: dict = {
        "input": case["input"],
        "actual_output": actual_output,
        "retrieval_context": facts,
        "context": facts,
        "name": case["id"],
    }
    expected = case.get("expected_output")
    if expected:
        kwargs["expected_output"] = expected
    return LLMTestCase(**kwargs)
