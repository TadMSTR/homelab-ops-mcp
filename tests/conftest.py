"""Shared pytest fixtures."""

import pytest

from homelab_ops_mcp import server


class LogRecorder:
    """Stands in for the module logger and keeps every event's kwargs."""

    def __init__(self):
        self.events = []

    def _record(self, event, **kw):
        self.events.append((event, kw))

    debug = info = warning = error = _record

    def of(self, event):
        """Every recorded kwargs dict for one event name, in order."""
        return [kw for name, kw in self.events if name == event]


@pytest.fixture()
def recorder(monkeypatch):
    r = LogRecorder()
    monkeypatch.setattr(server, "log", r)
    return r


@pytest.fixture()
def sample_file(tmp_path):
    """A small multi-line text file; returns its Path."""
    p = tmp_path / "sample.txt"
    p.write_text("line1\nline2\nline3\n")
    return p
