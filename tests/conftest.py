"""Shared pytest fixtures."""

import pytest


@pytest.fixture()
def sample_file(tmp_path):
    """A small multi-line text file; returns its Path."""
    p = tmp_path / "sample.txt"
    p.write_text("line1\nline2\nline3\n")
    return p
