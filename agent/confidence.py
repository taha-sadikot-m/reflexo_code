"""Post-hoc confidence scoring from tool usage signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_CONFIG_TOOLS = frozenset({"get_business_hours", "get_dynamic_data"})
_ENTITY_TOOLS = frozenset({"search_entities", "list_entities", "get_entity"})

LOW_CONFIDENCE_CUSTOMER_TEXT = (
    "Let me check with the team and get back to you."
)

_HEDGE_MARKERS = (
    "based on what i know",
    "i believe",
    "i think",
    "from what i can tell",
    "as far as i know",
    "i'm not entirely sure",
    "i am not entirely sure",
)


@dataclass
class ConfidenceAssessment:
    score: float
    tier: str
    reasons: list[str] = field(default_factory=list)
    should_escalate: bool = False
    hedge_response: bool = False


def _successful_tool_results(
    tool_results: list[dict[str, Any]], tool_names: frozenset[str]
) -> list[dict[str, Any]]:
    return [
        result
        for result in tool_results
        if result.get("tool_name") in tool_names
        and not result.get("error")
        and not result.get("error_type")
    ]


def _has_entity_evidence(tool_results: list[dict[str, Any]]) -> bool:
    for result in _successful_tool_results(tool_results, _ENTITY_TOOLS):
        tool_name = result.get("tool_name")
        if tool_name == "search_entities":
            if (result.get("total_results") or 0) > 0 or result.get("results"):
                return True
        elif tool_name == "list_entities":
            if (result.get("total_count") or 0) > 0 or result.get("entities"):
                return True
        elif tool_name == "get_entity":
            if result.get("id") or result.get("name") or result.get("entity_type"):
                return True
    return False


def has_authoritative_evidence(
    tool_results: list[dict[str, Any]],
    search_results: list[dict[str, Any]] | None,
) -> bool:
    """Return whether this turn retrieved usable business-owned answer data."""
    if search_results:
        return True
    if _has_entity_evidence(tool_results):
        return True
    return bool(_successful_tool_results(tool_results, _CONFIG_TOOLS))


def should_force_low_confidence_escalation(
    assessment: ConfidenceAssessment,
    tool_calls_made: list[str],
    tool_results: list[dict[str, Any]],
    search_results: list[dict[str, Any]] | None,
) -> bool:
    """Gate automatic gap/escalation on the absence of grounded answer data."""
    return (
        assessment.tier == "LOW"
        and "escalate_to_owner" not in tool_calls_made
        and not has_authoritative_evidence(tool_results, search_results)
    )


def calculate_confidence(
    tool_calls_made: list[str],
    tool_results: list[dict[str, Any]],
    search_results: list[dict[str, Any]] | None,
    gap_logged: bool,
    escalation_triggered: bool,
) -> ConfidenceAssessment:
    score = 0.0
    reasons: list[str] = []

    if search_results:
        max_confidence = max((r.get("confidence", 0) or 0 for r in search_results), default=0)
        if max_confidence > 0.7:
            score += 0.40
            reasons.append(f"KB search returned high-confidence result ({max_confidence:.2f})")
        elif max_confidence > 0.5:
            score += 0.20
            reasons.append(f"KB search returned moderate result ({max_confidence:.2f})")
        else:
            score -= 0.20
            reasons.append(f"KB search results were low confidence ({max_confidence:.2f})")

    entity_evidence = _has_entity_evidence(tool_results)
    if entity_evidence:
        score += 0.40
        reasons.append("Entity catalog returned matching results")

    used_config_tools = [t for t in tool_calls_made if t in _CONFIG_TOOLS]
    config_evidence = bool(_successful_tool_results(tool_results, _CONFIG_TOOLS))
    if used_config_tools and config_evidence:
        score += 0.15
        reasons.append("Business context data was available")

    if "get_customer" in tool_calls_made:
        score += 0.10
        reasons.append("Customer profile available for context")

    failed_tools = [r for r in tool_results if r.get("error")]
    if tool_calls_made and not failed_tools:
        score += 0.15
        reasons.append("All tool calls succeeded")
    elif failed_tools:
        score -= 0.15
        reasons.append(f"{len(failed_tools)} tool call(s) failed")

    if (
        not search_results
        and "search_knowledge" in tool_calls_made
        and not entity_evidence
        and not config_evidence
    ):
        score -= 0.30
        reasons.append("KB search returned no results")

    if gap_logged:
        score -= 0.40
        reasons.append("Knowledge gap was logged (AI does not know the answer)")

    if escalation_triggered:
        score -= 0.50
        reasons.append("Escalation was triggered")

    # No tools used (greeting etc.) — treat as moderate-high default
    if not tool_calls_made and not gap_logged and not escalation_triggered:
        score = 0.85
        reasons.append("Direct response without tools")

    # Successful authoritative retrieval should not fall into LOW.
    authoritative_evidence = has_authoritative_evidence(tool_results, search_results)
    if (
        authoritative_evidence
        and not failed_tools
        and not gap_logged
        and not escalation_triggered
        and score < 0.55
    ):
        score = 0.55
        reasons.append("Floor applied for successful authoritative retrieval")

    score = max(0.0, min(1.0, score))

    if score > 0.8:
        tier = "HIGH"
    elif score > 0.4:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    return ConfidenceAssessment(
        score=score,
        tier=tier,
        reasons=reasons,
        should_escalate=(tier == "LOW"),
        hedge_response=(tier == "MEDIUM"),
    )


def extract_search_results_from_tool_payloads(
    tool_calls_made: list[str],
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Best-effort extract KB hits from tool result payloads."""
    if "search_knowledge" not in tool_calls_made:
        return None
    hits: list[dict[str, Any]] = []
    for r in tool_results:
        if r.get("tool_name") != "search_knowledge":
            continue
        results = r.get("results") or r.get("entries") or []
        if isinstance(results, list):
            hits.extend(x for x in results if isinstance(x, dict))
    return hits or None


def maybe_hedge_response(text: str, assessment: ConfidenceAssessment) -> str:
    """Prepend light uncertainty language for MEDIUM-tier replies (§5.2)."""
    if not assessment.hedge_response or not (text or "").strip():
        return text
    lower = text.lower()
    if any(marker in lower for marker in _HEDGE_MARKERS):
        return text
    stripped = text.strip()
    if stripped and stripped[0].isupper() and len(stripped) > 1:
        stripped = stripped[0].lower() + stripped[1:]
    return f"Based on what I know, {stripped}"


def extract_gap_occurrence_count(tool_results: list[dict[str, Any]]) -> int | None:
    """Return the highest occurrence_count from log_gap tool results, if any."""
    best: int | None = None
    for r in tool_results:
        if r.get("tool_name") != "log_gap":
            continue
        count = r.get("occurrence_count")
        if isinstance(count, int):
            best = count if best is None else max(best, count)
    return best
