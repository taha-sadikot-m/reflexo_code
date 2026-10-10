"""Unit tests for OnboardingProgress."""

from __future__ import annotations

from onboarding.progress import OnboardingProgress, apply_progress_updates


def test_essential_incomplete_by_default():
    p = OnboardingProgress(business_id="b1")
    assert not p.is_essential_complete()
    assert "business_name" in p.get_required_remaining()
    assert p.get_completion_percentage() == 0


def test_essential_complete_with_skips():
    p = OnboardingProgress(
        business_id="b1",
        business_name="Cool Style",
        owner_name="Ravi",
        what_they_sell="clothes",
        payment_methods=["UPI"],
        business_hours={"mon": {"open": "10:00", "close": "20:00"}},
        skipped_steps=["return_policy", "delivery_info"],
    )
    assert p.is_essential_complete()
    assert p.get_required_remaining() == []
    assert p.get_completion_percentage() == 100


def test_apply_progress_updates_append_increment_dedup():
    p = OnboardingProgress(business_id="b1")
    apply_progress_updates(
        p,
        {
            "business_name": "Shop",
            "entities_added": 2,
            "entity_names": ["Tee"],
            "entity_types_used": ["product"],
            "faqs_added": 1,
            "faq_topics": ["returns"],
            "skipped_steps": ["instagram"],
        },
    )
    apply_progress_updates(
        p,
        {
            "entities_added": 1,
            "entity_names": ["Jeans"],
            "entity_types_used": ["product", "service"],
            "skipped_steps": ["instagram", "persona"],
        },
    )
    assert p.business_name == "Shop"
    assert p.entities_added == 3
    assert p.entity_names == ["Tee", "Jeans"]
    assert p.entity_types_used == ["product", "service"]
    assert p.faqs_added == 1
    assert p.skipped_steps == ["instagram", "persona"]


def test_progress_summary_contains_sections():
    p = OnboardingProgress(
        business_id="b1",
        business_name="Cool Style",
        owner_name="Ravi",
    )
    summary = p.generate_progress_summary()
    assert "Cool Style" in summary
    assert "STILL NEEDED" in summary
    assert "Ravi" in summary


def test_from_json_roundtrip():
    p = OnboardingProgress(
        business_id="b1",
        business_name="X",
        payment_methods=["COD"],
        entities_added=2,
    )
    restored = OnboardingProgress.from_json(p.to_json())
    assert restored.business_name == "X"
    assert restored.payment_methods == ["COD"]
    assert restored.entities_added == 2
