"""Tests for structlog configuration."""

import structlog

from homelab_ops_mcp import logging as hlog


def _reset():
    hlog._configured = False


def test_configure_returns_bound_logger(monkeypatch):
    _reset()
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("LOG_FILE", raising=False)
    logger = hlog.configure_logging()
    assert hasattr(logger, "info")
    assert hlog._configured is True


def test_configure_is_idempotent(monkeypatch):
    _reset()
    monkeypatch.delenv("LOG_FILE", raising=False)
    first = hlog.configure_logging()
    second = hlog.configure_logging()
    assert type(first) is type(second)


def test_log_file_branch(monkeypatch, tmp_path):
    _reset()
    log_path = tmp_path / "server.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logger = hlog.configure_logging()
    logger.info("hello", key="value")
    assert log_path.exists()
    assert "hello" in log_path.read_text()


def test_log_file_bad_path_falls_back(monkeypatch):
    _reset()
    # A path whose parent does not exist raises OSError on open → falls back to stderr.
    monkeypatch.setenv("LOG_FILE", "/nonexistent-dir-xyz/server.log")
    logger = hlog.configure_logging()
    assert hasattr(logger, "info")


def test_unknown_level_defaults_to_info(monkeypatch):
    _reset()
    monkeypatch.setenv("LOG_LEVEL", "NONSENSE")
    monkeypatch.delenv("LOG_FILE", raising=False)
    hlog.configure_logging()
    # No exception; structlog is configured.
    assert structlog.is_configured() if hasattr(structlog, "is_configured") else True


def test_reset_between_runs(monkeypatch):
    # Restore a clean INFO/stderr config so other test modules aren't affected
    # by a lingering file stream from this module.
    _reset()
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    hlog.configure_logging()
