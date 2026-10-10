"""Cheap schema check so default pytest still validates the eval JSONL."""

from __future__ import annotations

from agent.guardrails import check_injection

from tests.eval.dataset import CATEGORIES, load_eval_cases


def test_eval_dataset_counts_and_blocked_cases_match_rails() -> None:
    cases = load_eval_cases()
    assert len(cases) >= 50
    counts = {cat: 0 for cat in CATEGORIES}
    for case in cases:
        counts[case["category"]] += 1
        if case.get("blocked"):
            assert check_injection(case["input"]), case["id"]
        else:
            assert not check_injection(case["input"]), case["id"]
    assert all(n >= 10 for n in counts.values()), counts
