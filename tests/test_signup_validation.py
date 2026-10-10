"""Signup request validation tests (no DB)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.signup import SignupRequest


def test_signup_phone_valid() -> None:
    req = SignupRequest(phone="+919876543210")
    assert req.phone == "+919876543210"


def test_signup_phone_normalizes_spaces() -> None:
    req = SignupRequest(phone="+91 98765 43210")
    assert req.phone == "+919876543210"


def test_signup_phone_rejects_landline() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(phone="+911234567890")
