"""Structured logging setup.

JSON logging via structlog is on by default. Two environment variables tune it:

- ``LOG_LEVEL`` — one of DEBUG/INFO/WARNING/ERROR (default ``INFO``).
- ``LOG_FILE``  — if set, logs are appended to this path instead of stderr.

Sensitive payloads (command text, file contents) are logged at DEBUG only; INFO
records carry non-sensitive metadata (paths, cwd, exit codes) suitable for an
audit trail without leaking data.
"""

import logging
import os
import sys
from typing import TextIO

import structlog

_configured = False


def configure_logging() -> structlog.stdlib.BoundLogger:
    """Configure structlog for JSON output and return a bound logger.

    Idempotent — repeated calls reuse the first configuration.
    """
    global _configured

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if not _configured:
        stream: TextIO = sys.stderr
        log_file = os.environ.get("LOG_FILE")
        if log_file:
            # Line-buffered append; never crash the server over a bad LOG_FILE.
            try:
                stream = open(log_file, "a", buffering=1)  # noqa: SIM115
            except OSError:
                stream = sys.stderr

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            logger_factory=structlog.PrintLoggerFactory(file=stream),
            cache_logger_on_first_use=True,
        )
        _configured = True

    return structlog.get_logger("homelab_ops")
