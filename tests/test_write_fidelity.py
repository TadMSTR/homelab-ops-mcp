"""write_file / edit_file: links, byte fidelity, and a bounded no-match hint.

Fingerprints: a99d0dcbffb92837 (symlink replaced / hard link split),
0e5f7a9995fbc098 (lossy edit round-trip), b1080ef8fe49a31a (closest-match cost).
"""

import os
import time

import pytest

from homelab_ops_mcp import server
from homelab_ops_mcp.server import _closest_match, edit_file, write_file


# --- symlinks are written through -------------------------------------------
@pytest.fixture()
def link(tmp_path):
    real = tmp_path / "real.conf"
    real.write_text("a = 1\n")
    lnk = tmp_path / "link.conf"
    lnk.symlink_to(real)
    return lnk, real


def test_write_file_writes_through_a_symlink(link):
    lnk, real = link
    r = write_file(str(lnk), "a = 2\n")
    assert lnk.is_symlink()
    assert real.read_text() == "a = 2\n"
    assert r["written_to"] == str(real)


def test_edit_file_writes_through_a_symlink(link):
    lnk, real = link
    r = edit_file(str(lnk), "1", "3")
    assert r["status"] == "ok"
    assert lnk.is_symlink()
    assert real.read_text() == "a = 3\n"
    assert r["written_to"] == str(real)


def test_write_through_a_chain_of_symlinks(link, tmp_path):
    lnk, real = link
    outer = tmp_path / "outer"
    outer.symlink_to(lnk)
    write_file(str(outer), "chained\n")
    assert outer.is_symlink() and lnk.is_symlink()
    assert real.read_text() == "chained\n"


def test_dangling_symlink_creates_its_target(tmp_path):
    target = tmp_path / "new.txt"
    lnk = tmp_path / "lnk"
    lnk.symlink_to(target)
    write_file(str(lnk), "x")
    assert lnk.is_symlink()
    assert target.read_text() == "x"


def test_write_through_keeps_the_target_mode(link):
    lnk, real = link
    real.chmod(0o640)
    write_file(str(lnk), "b\n")
    assert real.stat().st_mode & 0o777 == 0o640


def test_plain_file_reports_no_written_to(tmp_path):
    r = write_file(str(tmp_path / "f"), "x")
    assert "written_to" not in r


def test_no_temp_file_left_beside_the_link(link, tmp_path):
    lnk, _ = link
    write_file(str(lnk), "b\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.conf", "real.conf"]


# --- hard links are refused, not split ---------------------------------------
@pytest.fixture()
def hardlinked(tmp_path):
    h1 = tmp_path / "h1"
    h1.write_text("old\n")
    h2 = tmp_path / "h2"
    os.link(h1, h2)
    return h1, h2


def test_write_file_refuses_a_hard_linked_file(hardlinked):
    h1, h2 = hardlinked
    r = write_file(str(h1), "new\n")
    assert "hard links" in r["error"]
    assert h1.read_text() == h2.read_text() == "old\n"
    assert os.stat(h1).st_ino == os.stat(h2).st_ino


def test_edit_file_refuses_a_hard_linked_file(hardlinked):
    h1, h2 = hardlinked
    r = edit_file(str(h1), "old", "new")
    assert "hard links" in r["error"]
    assert os.stat(h1).st_ino == os.stat(h2).st_ino
    assert h2.read_text() == "old\n"


def test_edit_file_dry_run_refuses_a_hard_linked_file(hardlinked):
    """A dry run must not promise an edit the real call would refuse."""
    h1, _ = hardlinked
    r = edit_file(str(h1), "old", "new", dry_run=True)
    assert "hard links" in r["error"]


# --- edit_file changes only the matched bytes --------------------------------
def test_crlf_line_endings_survive_an_edit(tmp_path):
    p = tmp_path / "win.ini"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    r = edit_file(str(p), "two", "TWO")
    assert r["status"] == "ok"
    assert p.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_lf_old_str_matches_in_a_crlf_file_and_new_str_follows(tmp_path):
    p = tmp_path / "win.ini"
    p.write_bytes(b"[a]\r\nk=1\r\n[b]\r\n")
    r = edit_file(str(p), "[a]\nk=1\n", "[a]\nk=2\nj=3\n")
    assert r["line_endings"] == "crlf"
    assert p.read_bytes() == b"[a]\r\nk=2\r\nj=3\r\n[b]\r\n"


def test_crlf_old_str_matches_directly(tmp_path):
    p = tmp_path / "win.ini"
    p.write_bytes(b"a\r\nb\r\n")
    r = edit_file(str(p), "a\r\nb", "x\r\ny")
    assert "line_endings" not in r
    assert p.read_bytes() == b"x\r\ny\r\n"


def test_mixed_endings_elsewhere_are_untouched(tmp_path):
    p = tmp_path / "mixed"
    p.write_bytes(b"lf\ncrlf\r\ncr\rend")
    edit_file(str(p), "end", "END")
    assert p.read_bytes() == b"lf\ncrlf\r\ncr\rEND"


def test_non_utf8_file_is_refused_and_left_alone(tmp_path):
    p = tmp_path / "latin1.txt"
    original = b"one\nlatin1:\xe9\xff\n"
    p.write_bytes(original)
    r = edit_file(str(p), "one", "ONE")
    assert "not valid UTF-8" in r["error"]
    assert p.read_bytes() == original


def test_utf8_bom_survives_an_edit(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbfhello\n")
    edit_file(str(p), "hello", "world")
    assert p.read_bytes() == b"\xef\xbb\xbfworld\n"


def test_dry_run_reports_the_on_disk_size(tmp_path):
    p = tmp_path / "win.ini"
    p.write_bytes(b"a\r\nb\r\n")
    r = edit_file(str(p), "a", "A", dry_run=True)
    assert r["bytes_before"] == 6
    assert r["bytes_after"] == 6


# --- the no-match hint is bounded --------------------------------------------
def test_large_old_str_gets_no_hint_quickly(tmp_path):
    """Measured before the bound: 2 KiB old_str against 80 KiB took 168 s."""
    lines = [f"    value_{i} = compute(alpha, beta, gamma)  # {i}\n" for i in range(2000)]
    p = tmp_path / "big.py"
    p.write_text("".join(lines))
    stale = "".join(lines[500:600]).replace("compute", "compuet")  # ~5 KiB, no match
    started = time.monotonic()
    r = edit_file(str(p), stale, "x")
    assert time.monotonic() - started < 3
    assert "not found" in r["error"]
    assert "closest_match" not in r


def test_hint_scan_stops_at_its_time_budget(monkeypatch):
    clock = iter([0.0, 0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    content = "".join(f"line {i}\n" for i in range(50))
    assert _closest_match(content, "line 4O\n") is None


def test_small_typo_still_gets_a_hint(tmp_path):
    """The bound must not cost the common case its hint."""
    p = tmp_path / "f.py"
    p.write_text("".join(f"x{i} = {i}\n" for i in range(500)) + "def handler(request):\n")
    r = edit_file(str(p), "def handlr(request):\n", "x")
    assert r["closest_match"] == "def handler(request):\n"


def test_overlong_window_is_skipped_without_cost(tmp_path):
    """A window more than 3x old_str's length cannot reach the quoting threshold."""
    content = "z" * 500_000 + "\nhello world\n"
    assert _closest_match(content, "hello wrld\n")["closest_match"] == "hello world\n"
