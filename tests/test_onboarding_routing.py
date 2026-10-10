"""Tests for onboarding session_type routing."""

from __future__ import annotations

from agent.session import resolve_session_type


def test_owner_incomplete_is_onboarding():
    assert (
        resolve_session_type(is_owner=True, onboarding_completed=False) == "onboarding"
    )


def test_owner_complete_is_owner():
    assert resolve_session_type(is_owner=True, onboarding_completed=True) == "owner"


def test_customer_always_customer():
    assert resolve_session_type(is_owner=False, onboarding_completed=False) == "customer"
    assert resolve_session_type(is_owner=False, onboarding_completed=True) == "customer"
