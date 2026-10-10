"""Load and validate the Stage 9 JSONL eval dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

CATEGORIES = ("faq", "products", "sales", "support", "edge")
AGENTS = frozenset({"inquiry_agent", "sales_agent", "support_agent"})
_MIN_PER_CATEGORY = 10
_DATASET_DIR = Path(__file__).resolve().parent / "dataset"
_FILES = {
    "faq": "faq_queries.jsonl",
    "products": "product_queries.jsonl",
    "sales": "sales_queries.jsonl",
    "support": "support_queries.jsonl",
    "edge": "edge_cases.jsonl",
}


class EvalCase(TypedDict, total=False):
    id: str
    category: str
    input: str
    expected_agent: str
    retrieval_context: list[str]
    expected_output: str
    blocked: bool


def _parse_line(path: Path, line_no: int, raw: str) -> EvalCase:
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}:{line_no}: invalid JSON ({exc})") from exc
    case_id = str(data.get("id") or "").strip()
    category = str(data.get("category") or "").strip()
    text = str(data.get("input") or "").strip()
    agent = str(data.get("expected_agent") or "").strip()
    ctx = data.get("retrieval_context")
    if not case_id:
        raise ValueError(f"{path.name}:{line_no}: missing id")
    if category not in CATEGORIES:
        raise ValueError(f"{path.name}:{line_no}: bad category {category!r}")
    if not text:
        raise ValueError(f"{path.name}:{line_no}: missing input")
    if agent not in AGENTS:
        raise ValueError(f"{path.name}:{line_no}: bad expected_agent {agent!r}")
    if not isinstance(ctx, list) or not ctx or not all(isinstance(x, str) and x.strip() for x in ctx):
        raise ValueError(f"{path.name}:{line_no}: retrieval_context must be a non-empty list of strings")
    case: EvalCase = {
        "id": case_id,
        "category": category,
        "input": text,
        "expected_agent": agent,
        "retrieval_context": [x.strip() for x in ctx],
        "blocked": bool(data.get("blocked")),
    }
    expected = data.get("expected_output")
    if isinstance(expected, str) and expected.strip():
        case["expected_output"] = expected.strip()
    return case


def load_eval_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for category, filename in _FILES.items():
        path = _DATASET_DIR / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            case = _parse_line(path, line_no, raw)
            if case["category"] != category:
                raise ValueError(f"{path.name}:{line_no}: category mismatch (file is {category})")
            if case["id"] in seen:
                raise ValueError(f"duplicate case id {case['id']}")
            seen.add(case["id"])
            cases.append(case)
    counts = {cat: 0 for cat in CATEGORIES}
    for case in cases:
        counts[case["category"]] += 1
    missing = [f"{cat}={n}" for cat, n in counts.items() if n < _MIN_PER_CATEGORY]
    if missing:
        raise ValueError(f"need at least {_MIN_PER_CATEGORY} cases per category; got {', '.join(missing)}")
    if len(cases) < 50:
        raise ValueError(f"need at least 50 cases, got {len(cases)}")
    return cases
