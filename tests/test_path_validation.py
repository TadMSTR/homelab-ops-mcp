"""Tests for absolute-path enforcement across the file tools and run_command.

Regression cover for the tilde defect: ``Path("~/x")`` is not absolute, so it
resolved against the server's working directory, created a literal ``~``
directory there, and returned success.
"""

import pytest

from homelab_ops_mcp.server import (
    PathValidationError,
    _resolve_path,
    edit_file,
    read_directory,
    read_file,
    run_command,
    write_file,
)


# --- _resolve_path ------------------------------------------------------------
def test_resolve_path_accepts_absolute(tmp_path):
    assert _resolve_path(str(tmp_path)) == tmp_path


def test_resolve_path_rejects_tilde_naming_the_path():
    with pytest.raises(PathValidationError) as excinfo:
        _resolve_path("~/.claude/memory/note.md")
    msg = str(excinfo.value)
    assert "must be absolute" in msg
    # the offending path is echoed back so a calling agent self-corrects
    assert "~/.claude/memory/note.md" in msg


def test_resolve_path_rejects_relative():
    with pytest.raises(PathValidationError, match="must be absolute"):
        _resolve_path("relative/file.txt")


def test_resolve_path_rejects_empty():
    with pytest.raises(PathValidationError, match="must be absolute"):
        _resolve_path("")


def test_resolve_path_rejects_absolute_tilde_component():
    """Backstop: an absolute path may still carry a literal ``~`` component."""
    with pytest.raises(PathValidationError) as excinfo:
        _resolve_path("/home/ted/repos/~/.claude/note.md")
    msg = str(excinfo.value)
    assert "component '~'" in msg
    assert "/home/ted/repos/~/.claude/note.md" in msg


def test_resolve_path_allows_tilde_inside_a_filename(tmp_path):
    """Only a whole component named ``~`` is refused, not the character."""
    p = tmp_path / "backup~"
    assert _resolve_path(str(p)) == p


# --- file tools ---------------------------------------------------------------
@pytest.mark.parametrize("bad", ["~/x.md", "relative.md", "/home/ted/~/x.md"])
def test_read_file_rejects(bad):
    r = read_file(bad)
    assert "error" in r
    assert bad in r["error"]


@pytest.mark.parametrize("bad", ["~/x.md", "relative.md", "/home/ted/~/x.md"])
def test_write_file_rejects(bad):
    r = write_file(bad, "content")
    assert "error" in r
    assert bad in r["error"]
    assert "bytes_written" not in r


@pytest.mark.parametrize("bad", ["~/x.md", "relative.md", "/home/ted/~/x.md"])
def test_edit_file_rejects(bad):
    r = edit_file(bad, "a", "b")
    assert "error" in r
    assert bad in r["error"]


@pytest.mark.parametrize("bad", ["~/dir", "relative", "/home/ted/~/dir"])
def test_read_directory_rejects(bad):
    r = read_directory(bad)
    assert "error" in r
    assert bad in r["error"]


def test_write_file_tilde_creates_no_junk_tree(monkeypatch, tmp_path):
    """The defect itself: a tilde write used to create a ``~`` dir under cwd."""
    monkeypatch.chdir(tmp_path)
    r = write_file("~/.claude/memory/note.md", "content")
    assert "error" in r
    assert not (tmp_path / "~").exists()
    assert list(tmp_path.iterdir()) == []


def test_write_file_absolute_still_works(tmp_path):
    """Control: the same call with an absolute path succeeds."""
    target = tmp_path / "note.md"
    r = write_file(str(target), "content")
    assert r["bytes_written"] == 7
    assert target.read_text() == "content"


# --- run_command --------------------------------------------------------------
def test_run_command_rejects_tilde_cwd():
    r = run_command("echo hi", cwd="~/repos")
    assert r["exit_code"] == -1
    assert "must be absolute" in r["stderr"]
    assert "~/repos" in r["stderr"]
    assert r["stdout"] == ""


def test_run_command_rejects_relative_cwd():
    r = run_command("echo hi", cwd="repos")
    assert r["exit_code"] == -1
    assert "must be absolute" in r["stderr"]


def test_run_command_rejects_tilde_component_cwd():
    r = run_command("echo hi", cwd="/home/ted/~/repos")
    assert r["exit_code"] == -1
    assert "component '~'" in r["stderr"]


def test_run_command_absolute_cwd_still_works(tmp_path):
    """Control: an absolute cwd is unaffected."""
    r = run_command("pwd", cwd=str(tmp_path))
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == str(tmp_path)


def test_run_command_tilde_inside_command_is_untouched(tmp_path):
    """Tilde in the command string goes through bash, which expands it."""
    r = run_command("echo ~", cwd=str(tmp_path))
    assert r["exit_code"] == 0
    assert r["stdout"].strip().startswith("/")
    assert "~" not in r["stdout"]
