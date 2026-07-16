"""Tests for read_file, write_file, and edit_file."""

from homelab_ops_mcp.server import edit_file, read_file, write_file


# --- read_file ---------------------------------------------------------------
def test_read_file_full(sample_file):
    r = read_file(str(sample_file))
    assert r["total_lines"] == 3
    assert r["content"] == "line1\nline2\nline3\n"


def test_read_file_range(sample_file):
    r = read_file(str(sample_file), start_line=2, end_line=3)
    assert r["returned_lines"] == "2-3"
    assert r["content"] == "line2\nline3\n"


def test_read_file_range_clamped(sample_file):
    r = read_file(str(sample_file), start_line=-5, end_line=99)
    assert r["content"] == "line1\nline2\nline3\n"


def test_read_file_not_found(tmp_path):
    r = read_file(str(tmp_path / "nope.txt"))
    assert "not found" in r["error"].lower()


def test_read_file_not_a_file(tmp_path):
    r = read_file(str(tmp_path))
    assert "not a file" in r["error"].lower()


# --- write_file --------------------------------------------------------------
def test_write_file_basic(tmp_path):
    target = tmp_path / "out.txt"
    r = write_file(str(target), "hello")
    assert r["bytes_written"] == 5
    assert target.read_text() == "hello"


def test_write_file_creates_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "out.txt"
    r = write_file(str(target), "deep")
    assert r["bytes_written"] == 4
    assert target.read_text() == "deep"


def test_write_file_no_create_dirs(tmp_path):
    target = tmp_path / "missing" / "out.txt"
    r = write_file(str(target), "x", create_dirs=False)
    assert "error" in r


# --- edit_file ---------------------------------------------------------------
def test_edit_file_single_match(sample_file):
    r = edit_file(str(sample_file), "line2", "LINE2")
    assert r["status"] == "ok"
    assert r["matches_replaced"] == 1
    assert "LINE2" in sample_file.read_text()


def test_edit_file_zero_matches(sample_file):
    r = edit_file(str(sample_file), "absent", "x")
    assert "not found" in r["error"].lower()
    assert sample_file.read_text() == "line1\nline2\nline3\n"


def test_edit_file_multiple_matches(tmp_path):
    p = tmp_path / "dup.txt"
    p.write_text("aa\naa\n")
    r = edit_file(str(p), "aa", "bb")
    assert "exactly once" in r["error"]
    assert p.read_text() == "aa\naa\n"


def test_edit_file_not_found(tmp_path):
    r = edit_file(str(tmp_path / "nope.txt"), "a", "b")
    assert "not found" in r["error"].lower()
