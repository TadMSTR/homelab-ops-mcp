"""The end of the logging pipeline: everything on one stream, all of it JSON.

The complaint these cover is measured, not hypothetical: a rotated day-file held
154,109 uvicorn access lines against 15,089 structured events, so 91% of the
file was plain text interleaved with the JSON.
"""

import json
import logging

import pytest

from homelab_ops_mcp import logging as hlog


@pytest.fixture()
def logfile(monkeypatch, tmp_path):
    """Configure logging to a fresh file, and put it back afterwards.

    The teardown matters: leaving a handler bound to a deleted tmp file would
    break logging for every test that ran later.
    """
    path = tmp_path / "server.log"
    hlog._configured = False
    monkeypatch.setenv("LOG_FILE", str(path))
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    hlog.configure_logging()
    yield path
    hlog._configured = False
    monkeypatch.delenv("LOG_FILE", raising=False)
    hlog.configure_logging()


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- everything comes out as JSON --------------------------------------------
def test_own_events_are_json(logfile):
    hlog.configure_logging().info("own.event", key="value")
    (rec,) = [r for r in _records(logfile) if r["event"] == "own.event"]
    assert rec["key"] == "value"
    assert rec["level"] == "info"
    assert rec["logger"] == "homelab_ops"
    assert "timestamp" in rec


def test_foreign_stdlib_record_is_json_not_plain_text(logfile):
    """A uvicorn record used to land as plain text beside the JSON stream."""
    logging.getLogger("uvicorn.error").info("Started server process [123]")
    (rec,) = [r for r in _records(logfile) if r["logger"] == "uvicorn.error"]
    assert rec["event"] == "Started server process [123]"
    assert rec["level"] == "info"


def test_every_line_written_is_valid_json(logfile):
    hlog.configure_logging().info("own.event")
    logging.getLogger("fastmcp.server").warning("a library warning")
    logging.getLogger("mcp.server.streamable_http_manager").info("session started")
    for line in logfile.read_text().splitlines():
        if line.strip():
            json.loads(line)  # raises if any line is plain text


def test_logger_field_distinguishes_the_source(logfile):
    hlog.configure_logging().info("own.event")
    logging.getLogger("uvicorn.error").info("theirs")
    sources = {r["logger"] for r in _records(logfile)}
    assert {"homelab_ops", "uvicorn.error"} <= sources


def test_foreign_exception_is_json_with_the_traceback(logfile):
    """The case that matters: fastmcp's Rich handlers rendered these as plain text."""
    hlog.tame_library_logging()
    try:
        raise RuntimeError("library blew up")
    except RuntimeError:
        logging.getLogger("fastmcp.server").exception("handler failed")
    (rec,) = [r for r in _records(logfile) if r["event"] == "handler failed"]
    assert rec["level"] == "error"
    assert "RuntimeError: library blew up" in rec["exception"]


def test_unreclaimed_fastmcp_exception_never_reaches_the_json_stream(logfile):
    """The control for the test above — without the reclaim it is lost.

    Restores fastmcp's own arrangement (Rich handler, propagate off) and shows
    the record does not arrive, so the reclaim is load-bearing rather than
    decorative.
    """
    fastmcp_log = logging.getLogger("fastmcp")
    saved_handlers, saved_propagate = fastmcp_log.handlers[:], fastmcp_log.propagate
    fastmcp_log.handlers = [logging.NullHandler()]
    fastmcp_log.propagate = False
    try:
        try:
            raise RuntimeError("library blew up")
        except RuntimeError:
            logging.getLogger("fastmcp.server").exception("swallowed")
        assert [r for r in _records(logfile) if r["event"] == "swallowed"] == []
    finally:
        fastmcp_log.handlers, fastmcp_log.propagate = saved_handlers, saved_propagate


def test_level_filtering_applies_to_foreign_records(logfile):
    logging.getLogger("uvicorn.error").debug("should not appear")
    assert [r for r in _records(logfile) if r["event"] == "should not appear"] == []


# --- tame_library_logging -----------------------------------------------------
def test_access_log_is_silenced():
    hlog.tame_library_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is False


def test_access_records_reach_nothing(logfile):
    hlog.tame_library_logging()
    logging.getLogger("uvicorn.access").info('127.0.0.1:0 - "POST /mcp HTTP/1.1" 200 OK')
    assert [r for r in _records(logfile) if "POST /mcp" in str(r.get("event", ""))] == []


def test_uvicorn_errors_still_get_through(logfile):
    """Only the 200s go — an error must not be silenced with them."""
    hlog.tame_library_logging()
    logging.getLogger("uvicorn.error").error("something broke")
    assert [r for r in _records(logfile) if r["event"] == "something broke"]


def test_fastmcp_logger_is_reclaimed():
    """fastmcp sets propagate=False and attaches Rich handlers; take it back."""
    fastmcp_log = logging.getLogger("fastmcp")
    fastmcp_log.propagate = False
    fastmcp_log.addHandler(logging.NullHandler())
    hlog.tame_library_logging()
    assert fastmcp_log.handlers == []
    assert fastmcp_log.propagate is True


def test_fastmcp_is_asked_not_to_reconfigure():
    hlog.tame_library_logging()
    import fastmcp

    assert fastmcp.settings.log_enabled is False


def test_reclaimed_fastmcp_records_are_json(logfile):
    hlog.tame_library_logging()
    logging.getLogger("fastmcp.server").info("Starting MCP server")
    (rec,) = [r for r in _records(logfile) if r["event"] == "Starting MCP server"]
    assert rec["logger"] == "fastmcp.server"


# --- handler hygiene ----------------------------------------------------------
def test_reconfiguring_does_not_stack_handlers(monkeypatch, tmp_path):
    root = logging.getLogger()
    hlog._configured = False
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "s0.log"))
    hlog.configure_logging()
    baseline = len(root.handlers)
    try:
        for i in range(1, 4):
            hlog._configured = False
            monkeypatch.setenv("LOG_FILE", str(tmp_path / f"s{i}.log"))
            hlog.configure_logging()
            assert len(root.handlers) == baseline, (
                "each reconfigure must replace its own handler, not add one"
            )
        assert root.handlers.count(hlog._handler) == 1
    finally:
        hlog._configured = False
        monkeypatch.delenv("LOG_FILE", raising=False)
        hlog.configure_logging()


def test_configure_leaves_foreign_root_handlers_alone(monkeypatch, tmp_path):
    """Clearing root.handlers outright would remove handlers the host owns."""
    root = logging.getLogger()
    theirs = logging.NullHandler()
    root.addHandler(theirs)
    try:
        hlog._configured = False
        monkeypatch.setenv("LOG_FILE", str(tmp_path / "s.log"))
        hlog.configure_logging()
        assert theirs in root.handlers
    finally:
        root.removeHandler(theirs)
        hlog._configured = False
        monkeypatch.delenv("LOG_FILE", raising=False)
        hlog.configure_logging()
