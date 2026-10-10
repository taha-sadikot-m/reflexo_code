"""Worker logging with safe correlation and rotating persistence."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

from shared.logging_filters import install_httpx_credential_filter

_correlation_id: ContextVar[str] = ContextVar("worker_correlation_id", default="-")


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        return True


@contextmanager
def correlation_context(correlation_id: str) -> Iterator[None]:
    token = _correlation_id.set(correlation_id or "-")
    try:
        yield
    finally:
        _correlation_id.reset(token)


def configure_worker_logging(
    *,
    level: str,
    log_file: str,
    max_bytes: int,
    backup_count: int,
    root: Path,
) -> Path | None:
    """Configure console logging and an optional rotating worker log."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] corr=%(correlation_id)s %(message)s"
    )
    correlation_filter = CorrelationFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(correlation_filter)
    handlers: list[logging.Handler] = [console]

    resolved: Path | None = None
    if log_file.strip():
        candidate = Path(log_file)
        resolved = candidate if candidate.is_absolute() else root / candidate
        resolved.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            resolved,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(correlation_filter)
        handlers.append(rotating)

    logging.basicConfig(level=numeric_level, handlers=handlers, force=True)
    install_httpx_credential_filter()
    return resolved
