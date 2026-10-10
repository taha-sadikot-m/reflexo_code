"""Run Stage 9 eval and write EVAL_BASELINE.md (always writes scores)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "EVAL_BASELINE.md"

load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))

from tests.eval.dataset import load_eval_cases  # noqa: E402
from tests.eval.harness import generate_response, route_case  # noqa: E402
from tests.eval.metrics_factory import (  # noqa: E402
    FAITHFULNESS_THRESHOLD,
    HALLUCINATION_THRESHOLD,
    RELEVANCY_THRESHOLD,
    make_test_case,
    quality_metrics,
)


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _has_key() -> bool:
    import os

    if os.environ.get("GOOGLE_API_KEY"):
        return True
    from agent.settings import get_agent_settings

    return bool(get_agent_settings().google_api_key)


async def _retry(fn, *, attempts: int = 3, delay: float = 2.0):
    last = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            await asyncio.sleep(delay * (i + 1))
    raise last


async def _run_routing(cases) -> dict[str, Any]:
    rows = []
    for case in cases:
        result = await _retry(lambda: route_case(case))
        got = str(result.get("agent") or "")
        ok = got == case["expected_agent"]
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expected": case["expected_agent"],
                "got": got,
                "ok": ok,
            }
        )
        await asyncio.sleep(0.15)
    accuracy = sum(1 for r in rows if r["ok"]) / len(rows)
    return {"accuracy": accuracy, "rows": rows}


async def _run_quality(cases) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    names = [
        "FaithfulnessMetric",
        "AnswerRelevancyMetric",
        "HallucinationMetric",
    ]
    for case in cases:
        generated = await _retry(lambda: generate_response(case))
        test_case = make_test_case(case, generated["actual_output"])
        metrics = quality_metrics()
        names = [type(m).__name__ for m in metrics]
        scores: dict[str, float | None] = {}
        reasons: dict[str, str] = {}
        for metric, name in zip(metrics, names, strict=True):
            await _retry(lambda m=metric: m.a_measure(test_case))
            raw = getattr(metric, "score", None)
            scores[name] = float(raw) if raw is not None else None
            reasons[name] = str(getattr(metric, "reason", "") or "")
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "blocked": generated.get("blocked"),
                "actual_output": generated["actual_output"],
                "scores": scores,
                "reasons": reasons,
            }
        )
        await asyncio.sleep(0.2)
    means: dict[str, float] = {}
    for name in names:
        values = [r["scores"][name] for r in rows if r["scores"].get(name) is not None]
        means[name] = mean(values) if values else 0.0
    return {"means": means, "rows": rows}


def _worst(quality: dict[str, Any], n: int = 5) -> list[dict[str, Any]]:
    ranked = []
    for row in quality["rows"]:
        faith = row["scores"].get("FaithfulnessMetric")
        rel = row["scores"].get("AnswerRelevancyMetric")
        hall = row["scores"].get("HallucinationMetric")
        # Higher hallucination is worse; lower faith/rel is worse.
        penalty = 0.0
        if faith is not None:
            penalty += max(0.0, FAITHFULNESS_THRESHOLD - faith)
        if rel is not None:
            penalty += max(0.0, RELEVANCY_THRESHOLD - rel)
        if hall is not None:
            penalty += max(0.0, HALLUCINATION_THRESHOLD - hall)
        ranked.append((penalty, row))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [row for penalty, row in ranked[:n] if penalty > 0 or True][:n]


def _render(routing: dict[str, Any], quality: dict[str, Any]) -> str:
    means = quality["means"]
    faith = means.get("FaithfulnessMetric", 0.0)
    rel = means.get("AnswerRelevancyMetric", 0.0)
    hall = means.get("HallucinationMetric", 0.0)
    acc = routing["accuracy"]
    t92 = "PASS" if faith >= FAITHFULNESS_THRESHOLD else "FAIL"
    t93 = "PASS" if rel >= RELEVANCY_THRESHOLD else "FAIL"
    t94 = "PASS" if hall >= HALLUCINATION_THRESHOLD else "FAIL"
    t95 = "PASS" if acc >= 0.85 else "FAIL"
    mismatches = [r for r in routing["rows"] if not r["ok"]]
    by_cat: dict[str, int] = defaultdict(int)
    for row in mismatches:
        by_cat[row["category"]] += 1
    worst = _worst(quality)
    lines = [
        "# EVAL_BASELINE",
        "",
        f"- Date (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        f"- Git: `{_git_sha()}`",
        f"- Cases: {len(routing['rows'])}",
        "",
        "## Thresholds (Stage 9)",
        "",
        "| Check | Threshold | Observed | Result |",
        "|-------|-----------|----------|--------|",
        f"| T9.2 Faithfulness (mean) | ≥ {FAITHFULNESS_THRESHOLD} | {faith:.3f} | {t92} |",
        f"| T9.3 Answer relevancy (mean) | ≥ {RELEVANCY_THRESHOLD} | {rel:.3f} | {t93} |",
        f"| T9.4 HallucinationMetric (DeepEval 4.x, higher = more aligned) | ≥ {HALLUCINATION_THRESHOLD} | {hall:.3f} | {t94} |",
        f"| T9.5 Router accuracy | ≥ 0.85 | {acc:.1%} | {t95} |",
        "",
        "## Routing mismatches",
        "",
    ]
    if not mismatches:
        lines.append("None.")
    else:
        lines.append(f"{len(mismatches)} mismatches by category: {dict(by_cat)}")
        lines.append("")
        for row in mismatches:
            lines.append(f"- `{row['id']}`: expected `{row['expected']}`, got `{row['got']}`")
    lines += ["", "## Worst 5 quality cases (for Stage 10)", ""]
    for row in worst:
        lines.append(f"### `{row['id']}` ({row['category']})")
        lines.append("")
        lines.append(
            f"- Faithfulness: {row['scores'].get('FaithfulnessMetric')}"
            f" | Relevancy: {row['scores'].get('AnswerRelevancyMetric')}"
            f" | Hallucination: {row['scores'].get('HallucinationMetric')}"
        )
        snippet = (row["actual_output"] or "").replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        lines.append(f"- Output: {snippet}")
        faith_r = row["reasons"].get("FaithfulnessMetric") or ""
        if faith_r:
            lines.append(f"- Faithfulness reason: {faith_r[:400]}")
        lines.append("")
    lines += [
        "## Notes",
        "",
        "Quality eval uses gold `retrieval_context` stubs (not live hybrid RRF).",
        "Injection cases use input rails and the canned refusal; they are not sent to specialists.",
        "DeepEval 4.x flipped HallucinationMetric (higher score = more factual alignment). "
        "The old MVP note 'lower is better / < 0.5' does not apply to this library version.",
        "Missed relevancy cases are listed above for Stage 10; specialists were not rewritten in Stage 9.",
        "",
    ]
    return "\n".join(lines) + "\n"


async def _main() -> int:
    if not _has_key():
        print("GOOGLE_API_KEY is required to record EVAL_BASELINE.md", file=sys.stderr)
        return 2
    cases = load_eval_cases()
    print(f"Routing {len(cases)} cases...")
    routing = await _run_routing(cases)
    print(f"Routing accuracy: {routing['accuracy']:.1%}")
    print("Scoring quality (specialist + DeepEval judges)...")
    quality = await _run_quality(cases)
    print("Means:", quality["means"])
    BASELINE_PATH.write_text(_render(routing, quality), encoding="utf-8")
    print(f"Wrote {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
