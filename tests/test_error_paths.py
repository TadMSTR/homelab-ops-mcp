"""Cover the PermissionError and generic-exception branches of each tool."""

import pytest

from homelab_ops_mcp import server
from homelab_ops_mcp.server import (
    edit_file,
    list_processes,
    read_directory,
    read_file,
    write_file,
)


def _raise(exc):
    def _inner(*a, **k):
        raise exc

    return _inner


# --- read_file ---------------------------------------------------------------
# read_file streams the file through _read_lines_capped; fail there.
def test_read_file_permission_denied(monkeypatch, sample_file):
    monkeypatch.setattr(server, "_read_lines_capped", _raise(PermissionError()))
    r = read_file(str(sample_file))
    assert "permission denied" in r["error"].lower()


def test_read_file_generic_error(monkeypatch, sample_file):
    monkeypatch.setattr(server, "_read_lines_capped", _raise(ValueError("boom")))
    r = read_file(str(sample_file))
    assert r["error"] == "boom"


# --- write_file --------------------------------------------------------------
def test_write_file_permission_denied(monkeypatch, tmp_path):
    # write_file writes atomically via a temp file it creates in the target dir; fail there.
    monkeypatch.setattr(server, "_create_temp", _raise(PermissionError()))
    r = write_file(str(tmp_path / "x.txt"), "data")
    assert "permission denied" in r["error"].lower()


def test_write_file_generic_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_create_temp", _raise(ValueError("nope")))
    r = write_file(str(tmp_path / "x.txt"), "data")
    assert r["error"] == "nope"


# --- edit_file ---------------------------------------------------------------
# edit_file reads raw bytes through _read_regular, so the round-trip is lossless; fail there.
def test_edit_file_permission_denied(monkeypatch, sample_file):
    monkeypatch.setattr(server, "_read_regular", _raise(PermissionError()))
    r = edit_file(str(sample_file), "line1", "X")
    assert "permission denied" in r["error"].lower()


def test_edit_file_generic_error(monkeypatch, sample_file):
    monkeypatch.setattr(server, "_read_regular", _raise(RuntimeError("kaboom")))
    r = edit_file(str(sample_file), "line1", "X")
    assert r["error"] == "kaboom"


# --- read_directory ----------------------------------------------------------
def test_read_directory_inner_permission_denied(monkeypatch, tmp_path):
    monkeypatch.setattr(server.Path, "iterdir", _raise(PermissionError()))
    r = read_directory(str(tmp_path))
    # Inner PermissionError is caught and reported as an entry, not top-level error.
    assert r["entries"][0]["error"] == "permission denied"


def test_read_directory_generic_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server.Path, "is_dir", _raise(RuntimeError("bad")))
    r = read_directory(str(tmp_path))
    assert r["error"] == "bad"


# --- list_processes ----------------------------------------------------------
def test_list_processes_generic_error(monkeypatch):
    monkeypatch.setattr(server.psutil, "process_iter", _raise(RuntimeError("psutil down")))
    r = list_processes()
    assert r["error"] == "psutil down"


def test_list_processes_skips_dead_procs(monkeypatch):
    class _Dead:
        @property
        def info(self):
            raise server.psutil.NoSuchProcess(pid=1)

    real_iter = server.psutil.process_iter

    def _iter(attrs):
        yield _Dead()
        yield from real_iter(attrs)

    monkeypatch.setattr(server.psutil, "process_iter", _iter)
    r = list_processes(limit=5)
    assert r["count"] >= 0  # dead proc silently skipped, no crash


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
