"""Logging filters that keep credentials out of HTTP request URLs."""

from __future__ import annotations

import logging
import re

_SECRET_QUERY_PARAM = re.compile(
    r"(?i)(\b|%26)("
    r"access_token|input_token|client_secret|fb_exchange_token|"
    r"code|appsecret_proof"
    r")(=|%3d)((?:(?!%26)[^&\s\"])+)"
)


class RedactHttpCredentials(logging.Filter):
    """Redact Meta/OAuth credentials before an httpx record is rendered."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SECRET_QUERY_PARAM.sub(r"\1\2\3[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_httpx_credential_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if any(isinstance(item, RedactHttpCredentials) for item in httpx_logger.filters):
        return
    httpx_logger.addFilter(RedactHttpCredentials())
