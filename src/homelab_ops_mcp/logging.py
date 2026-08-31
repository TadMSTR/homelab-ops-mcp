"""Structured logging setup.

JSON logging via structlog is on by default. Two environment variables tune it:

- ``LOG_LEVEL`` — one of DEBUG/INFO/WARNING/ERROR (default ``INFO``).
- ``LOG_FILE``  — if set, logs are appended to this path instead of stderr.

Everything lands as JSON on one stream, including records from libraries that
log through the standard library rather than structlog. Third-party records are
run through the same processor chain via ``ProcessorFormatter``, so uvicorn and
fastmcp output is machine-readable alongside this server's own events instead of
plain text interleaved with them.

Sensitive payloads (command text, file contents) are logged at DEBUG only; INFO
records carry non-sensitive metadata (paths, cwd, exit codes) suitable for an
audit trail without leaking data.
"""

import contextlib
import logging
import os
import sys
from typing import TextIO

import structlog

_configured = False
_handler: logging.Handler | None = None

# Run over this server's own events and over foreign stdlib records alike, so a
# uvicorn line and a run_command line carry the same keys.
_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
]


def _open_stream() -> TextIO:
    """The log destination: ``LOG_FILE`` if set and openable, else stderr."""
    log_file = os.environ.get("LOG_FILE")
    if not log_file:
        return sys.stderr
    try:
        # Line-buffered append; never crash the server over a bad LOG_FILE.
        return open(log_file, "a", buffering=1)
    except OSError:
        return sys.stderr


def configure_logging() -> structlog.stdlib.BoundLogger:
    """Configure structlog for JSON output and return a bound logger.

    Idempotent — repeated calls reuse the first configuration.
    """
    global _configured, _handler

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if not _configured:
        structlog.configure(
            processors=[
                *_SHARED_PROCESSORS,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        handler = logging.StreamHandler(_open_stream())
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=_SHARED_PROCESSORS,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )

        root = logging.getLogger()
        # Replace only the handler this module installed. Clearing the list
        # outright would also remove handlers the host process owns.
        if _handler is not None:
            root.removeHandler(_handler)
        root.addHandler(handler)
        root.setLevel(level)
        _handler = handler
        _configured = True

    return structlog.get_logger("homelab_ops")


def tame_library_logging() -> None:
    """Stop libraries writing plain text alongside the structured stream.

    Two separate problems, both of which end up in the same file once a process
    manager merges stderr:

    **uvicorn's access log** — one line per request, and the request volume here
    is entirely MCP tool traffic. A rotated day-file held 154,109 access lines
    against 15,089 structured events, so 91% of it was untyped noise saying
    nothing the tool events did not already say. Errors still log; only the 200s
    go. This is belt and braces with ``access_log=False`` in the uvicorn config,
    which is the flag that actually takes effect.

    **fastmcp's own logger** — fastmcp sets ``propagate = False`` on it and
    attaches Rich handlers, which puts its records beyond the JSON formatter on
    the root logger. The startup line is trivial, but the same handlers render
    exception tracebacks as multi-line plain text, which is the case that
    matters when something is actually wrong. Hand the logger back so its
    records come out as JSON like everything else.
    """
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False

    # Ask fastmcp not to reinstall its handlers, in case it configures again
    # after this point. Guarded: this is a setting, not a stable API.
    with contextlib.suppress(Exception):
        import fastmcp

        fastmcp.settings.log_enabled = False

    fastmcp_log = logging.getLogger("fastmcp")
    fastmcp_log.handlers = []
    fastmcp_log.propagate = True
