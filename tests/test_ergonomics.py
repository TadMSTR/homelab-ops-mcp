"""dry_run, closest-match feedback, read_multiple_files, execution_time."""

import pytest

from homelab_ops_mcp.server import (
    _CLOSEST_MATCH_MAX_BYTES,
    _DIFF_MAX_LINES,
    _closest_match,
    edit_file,
    read_multiple_files,
    run_command,
)


# --- edit_file dry_run --------------------------------------------------------
def test_dry_run_reports_the_edit_without_writing(sample_file):
    before = sample_file.read_text()
    r = edit_file(str(sample_file), "line2", "LINE2", dry_run=True)
    assert r["status"] == "ok"
    assert r["dry_run"] is True
    assert r["matches_replaced"] == 1
    assert sample_file.read_text() == before, "dry run must not touch the file"


def test_dry_run_reports_the_size_change(sample_file):
    r = edit_file(str(sample_file), "line2", "a much longer replacement", dry_run=True)
    assert r["bytes_before"] == 18
    assert r["bytes_after"] > r["bytes_before"]


def test_dry_run_defaults_to_off(sample_file):
    r = edit_file(str(sample_file), "line2", "LINE2")
    assert r["dry_run"] is False
    assert "LINE2" in sample_file.read_text()


def test_dry_run_field_is_always_present(sample_file):
    """Absence and falsity must not look the same to a caller."""
    assert "dry_run" in edit_file(str(sample_file), "line2", "LINE2")


def test_dry_run_still_rejects_multiple_matches(tmp_path):
    p = tmp_path / "dup.txt"
    p.write_text("aa\naa\n")
    r = edit_file(str(p), "aa", "bb", dry_run=True)
    assert "exactly once" in r["error"]
    assert p.read_text() == "aa\naa\n"


# --- closest-match feedback ---------------------------------------------------
def test_no_match_reports_the_closest_passage(tmp_path):
    p = tmp_path / "conf.py"
    p.write_text("alpha = 1\nbeta_value = 22\ngamma = 3\n")
    r = edit_file(str(p), "beta_value = 99", "x")
    assert "not found" in r["error"]
    assert r["closest_match"] == "beta_value = 22\n"
    assert r["closest_match_line"] == 2
    assert r["similarity"] > 0.8
    # the diff pinpoints the differing characters, not just the line
    assert "^^" in r["diff"]


def test_diff_marks_the_differing_characters(tmp_path):
    p = tmp_path / "conf.py"
    p.write_text("timeout = 30\n")
    r = edit_file(str(p), "timeout = 60\n", "x")
    # ndiff's "?" lines point at the characters that differ
    assert "?" in r["diff"]
    assert "- timeout = 60" in r["diff"]
    assert "+ timeout = 30" in r["diff"]


def test_no_hint_when_nothing_resembles_the_request(tmp_path):
    p = tmp_path / "conf.py"
    p.write_text("alpha\nbeta\ngamma\n")
    r = edit_file(str(p), "completely unrelated content here", "x")
    assert "not found" in r["error"]
    assert "closest_match" not in r, "a bad guess is worse than none"


def test_hint_is_absent_for_a_very_large_file(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("x" * (_CLOSEST_MATCH_MAX_BYTES + 1))
    r = edit_file(str(p), "y" * 40, "z")
    assert "not found" in r["error"]
    assert "closest_match" not in r


def test_multi_line_old_str_matches_a_multi_line_window(tmp_path):
    p = tmp_path / "conf.py"
    p.write_text("one\ntwo\nthree\nfour\n")
    r = edit_file(str(p), "two\nTHREE\n", "x")
    assert r["closest_match"] == "two\nthree\n"
    assert r["closest_match_line"] == 2


def test_diff_is_bounded(tmp_path):
    p = tmp_path / "big.py"
    p.write_text("".join(f"line {i}\n" for i in range(200)))
    r = edit_file(str(p), "".join(f"lineX {i}\n" for i in range(200)), "x")
    if "diff" in r:
        assert len(r["diff"].splitlines()) <= _DIFF_MAX_LINES + 1


def test_closest_match_on_an_empty_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("")
    r = edit_file(str(p), "anything", "x")
    assert "not found" in r["error"]
    assert "closest_match" not in r


def test_closest_match_helper_returns_none_when_below_threshold():
    assert _closest_match("aaaa\n", "zzzzzzzzzzzz\n") is None


# --- read_multiple_files ------------------------------------------------------
def test_reads_several_files(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A\n")
    b.write_text("B\n")
    r = read_multiple_files([str(a), str(b)])
    assert r["count"] == 2
    assert r["ok"] == 2
    assert r["failed"] == 0
    assert r["files"][str(a)]["content"] == "A\n"
    assert r["files"][str(b)]["content"] == "B\n"


def test_one_bad_path_does_not_fail_the_others(tmp_path):
    good = tmp_path / "a.txt"
    good.write_text("A\n")
    missing = str(tmp_path / "nope.txt")
    r = read_multiple_files([str(good), missing])
    assert r["ok"] == 1
    assert r["failed"] == 1
    assert r["files"][str(good)]["content"] == "A\n"
    assert "error" in r["files"][missing]


def test_path_validation_applies(tmp_path):
    r = read_multiple_files(["~/x.md", "relative.md"])
    assert r["ok"] == 0
    assert r["failed"] == 2
    for path in ("~/x.md", "relative.md"):
        assert path in r["files"][path]["error"]


def test_empty_list(tmp_path):
    r = read_multiple_files([])
    assert r == {"count": 0, "ok": 0, "failed": 0, "files": {}}


def test_duplicate_paths_collapse(tmp_path):
    """Keyed by path, so a repeated path is one entry — and count says so."""
    a = tmp_path / "a.txt"
    a.write_text("A\n")
    r = read_multiple_files([str(a), str(a)])
    assert len(r["files"]) == 1
    assert r["count"] == 2


# --- execution_time -----------------------------------------------------------
def test_execution_time_present_and_positive():
    r = run_command("echo hi")
    assert isinstance(r["execution_time"], float)
    assert r["execution_time"] >= 0


def test_execution_time_reflects_a_slow_command():
    r = run_command("sleep 0.3", timeout=5)
    assert r["execution_time"] >= 0.3


def test_execution_time_present_on_timeout():
    r = run_command("sleep 5", timeout=1)
    assert r["exit_code"] == -1
    assert r["execution_time"] >= 1


@pytest.mark.parametrize("command", ["true", "exit 3", "echo x 1>&2"])
def test_execution_time_present_on_every_outcome(command):
    assert "execution_time" in run_command(command)
