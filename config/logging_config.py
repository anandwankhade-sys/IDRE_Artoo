# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

import logging
import sys

import structlog

from config.settings import settings


def configure_logging() -> None:
    """Configure structlog for structured JSON output to stderr."""
    # Ensure UTF-8 encoding on Windows where the default stream encoding is cp1252.
    # Without this, emoji and other non-ASCII characters in log messages raise
    # UnicodeEncodeError at runtime.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Standard library logging config (for libraries that use stdlib logging)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
