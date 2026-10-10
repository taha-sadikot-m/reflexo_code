"""HMAC-SHA256 webhook signature verification for Meta."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
    """
    Verify X-Hub-Signature-256: sha256=<hex_digest>.

    Returns False if header is missing/malformed or digest does not match.
    """
    if not app_secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[7:]
    computed_sig = hmac.new(
        app_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)
