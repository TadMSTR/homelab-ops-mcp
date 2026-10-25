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


# --- links planted in a shared sticky directory are not followed -------------
@pytest.mark.parametrize(
    ("link_uid", "dir_mode", "dir_uid", "allowed"),
    [
        (1000, 0o41777, 0, True),  # follower owns the link
        (2000, 0o41777, 2000, True),  # link owner owns the directory
        (2000, 0o41777, 0, False),  # /tmp, someone else's link
        (2000, 0o40777, 0, True),  # world-writable but not sticky
        (2000, 0o41755, 0, True),  # sticky but not world-writable
    ],
)
def test_protected_symlinks_rule(link_uid, dir_mode, dir_uid, allowed):
    assert server._link_hop_allowed(link_uid, dir_mode, dir_uid, euid=1000) is allowed


@pytest.fixture()
def sticky(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    yield shared
    shared.chmod(0o755)


def _as_foreign_link(monkeypatch, link):
    """Make ``link`` look owned by another uid (creating one for real needs root)."""
    real_lstat = os.lstat

    def lstat(p, *a, **k):
        st = real_lstat(p, *a, **k)
        if os.fspath(p) == os.fspath(link):
            fields = list(st)
            fields[4] = os.geteuid() + 4242  # st_uid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(server.os, "lstat", lstat)


def test_foreign_link_in_a_sticky_directory_is_refused(monkeypatch, tmp_path, sticky):
    victim = tmp_path / "authorized_keys"
    victim.write_text("original\n")
    planted = sticky / "report.txt"
    planted.symlink_to(victim)
    _as_foreign_link(monkeypatch, planted)
    for result in (
        write_file(str(planted), "attacker\n"),
        edit_file(str(planted), "original", "attacker"),
    ):
        # The refusal explains itself rather than reading as a plain EACCES (F-05).
        assert "not followed" in result["error"]
    assert victim.read_text() == "original\n"


def test_unsafe_link_refusal_has_its_own_log_event(monkeypatch, tmp_path, sticky, recorder):
    victim = tmp_path / "v"
    victim.write_text("original\n")
    planted = sticky / "report.txt"
    planted.symlink_to(victim)
    _as_foreign_link(monkeypatch, planted)
    write_file(str(planted), "x")
    edit_file(str(planted), "original", "x")
    assert recorder.of("write_file.unsafe_link_refused") == [{"path": str(planted)}]
    assert recorder.of("edit_file.unsafe_link_refused") == [{"path": str(planted)}]
    assert recorder.of("write_file.permission_denied") == []


def test_foreign_directory_link_in_a_sticky_directory_is_refused(monkeypatch, tmp_path, sticky):
    """F-01: /tmp/<dir> -> ~/.ssh, then a write to /tmp/<dir>/authorized_keys.

    The check used to cover only the final component and its hops, so a link
    in a directory component went through realpath unchecked.
    """
    victim_dir = tmp_path / "dot_ssh"
    victim_dir.mkdir()
    victim = victim_dir / "authorized_keys"
    victim.write_text("original\n")
    planted = sticky / "somedir"
    planted.symlink_to(victim_dir)
    _as_foreign_link(monkeypatch, planted)
    for result in (
        write_file(str(planted / "authorized_keys"), "attacker\n"),
        edit_file(str(planted / "authorized_keys"), "original", "attacker"),
        write_file(str(planted / "new_file"), "attacker\n"),
    ):
        assert "not followed" in result["error"]
    assert victim.read_text() == "original\n"
    assert sorted(p.name for p in victim_dir.iterdir()) == ["authorized_keys"]


def test_own_directory_link_is_followed(tmp_path, sticky):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (sticky / "dirlink").symlink_to(real_dir)
    r = write_file(str(sticky / "dirlink" / "f"), "x")
    assert (real_dir / "f").read_text() == "x"
    assert r["written_to"] == str(real_dir / "f")


def test_link_planted_at_a_missing_name_after_the_check_is_replaced_not_followed(
    monkeypatch, tmp_path
):
    """F-01 second aspect: the final name did not exist when checked, then a link appears.

    The rename replaces the directory entry, so the late link is overwritten
    rather than written through.
    """
    victim = tmp_path / "victim"
    victim.write_text("original\n")
    name = tmp_path / "report.txt"
    real_resolve = server._checked_resolve

    def resolve_then_plant(path):
        resolved = real_resolve(path)
        name.symlink_to(victim)
        return resolved

    monkeypatch.setattr(server, "_checked_resolve", resolve_then_plant)
    assert write_file(str(name), "new\n")["bytes_written"] == 4
    assert victim.read_text() == "original\n"
    assert not name.is_symlink()
    assert name.read_text() == "new\n"


def test_directories_are_opened_by_the_kernel_not_resolved_in_userspace(monkeypatch, link):
    """realpath follows links in userspace, out of reach of protected_symlinks."""

    def forbidden(*a, **k):
        raise AssertionError("os.path.realpath must not be used on the write path")

    monkeypatch.setattr(server.os.path, "realpath", forbidden)
    lnk, real = link
    write_file(str(lnk), "a = 9\n")
    edit_file(str(lnk), "9", "8")
    assert real.read_text() == "a = 8\n"


def test_a_kernel_refusal_of_the_directory_stops_the_write(monkeypatch, tmp_path):
    """What protected_symlinks looks like from here: EACCES opening the directory."""
    real_open = os.open

    def deny_dirs(p, flags, *a, **k):
        if flags & os.O_DIRECTORY:
            raise PermissionError(13, "Permission denied", p)
        return real_open(p, flags, *a, **k)

    monkeypatch.setattr(server.os, "open", deny_dirs)
    target = tmp_path / "f"
    assert "Permission denied" in write_file(str(target), "x")["error"]
    assert not target.exists()


def test_dangling_link_into_a_missing_directory_creates_it(tmp_path):
    """CR-02: create_dirs applies to the directory the write lands in."""
    lnk = tmp_path / "lnk"
    lnk.symlink_to(tmp_path / "new" / "deeper" / "f.txt")
    r = write_file(str(lnk), "x")
    assert (tmp_path / "new" / "deeper" / "f.txt").read_text() == "x"
    assert lnk.is_symlink()
    assert "error" not in r


def test_writing_to_a_directory_is_refused(tmp_path):
    (tmp_path / "d").mkdir()
    assert "is a directory" in write_file(str(tmp_path / "d"), "x")["error"]


# --- edit_file reads only bounded regular files (F-04) ------------------------
def test_edit_file_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    r = edit_file(str(fifo), "a", "b")
    assert time.monotonic() - started < 2
    assert "not a regular file" in r["error"]


def test_edit_file_refuses_a_device(tmp_path):
    assert "not a regular file" in edit_file("/dev/null", "a", "b")["error"]


def test_edit_file_refuses_a_file_over_the_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_EDIT_MAX_BYTES", 100)
    p = tmp_path / "big"
    p.write_text("x" * 101)
    r = edit_file(str(p), "x", "y")
    assert "over edit_file's 100-byte limit" in r["error"]
    assert p.read_text() == "x" * 101


def test_edit_file_in_a_missing_directory_is_not_found(tmp_path):
    r = edit_file(str(tmp_path / "nope" / "f"), "a", "b")
    assert r["error"].startswith("File not found")


def test_foreign_link_later_in_the_chain_is_refused(monkeypatch, tmp_path, sticky):
    victim = tmp_path / "victim"
    victim.write_text("original\n")
    hop2 = sticky / "hop2"
    hop2.symlink_to(victim)
    hop1 = tmp_path / "hop1"
    hop1.symlink_to(hop2)
    _as_foreign_link(monkeypatch, hop2)
    assert "error" in write_file(str(hop1), "attacker\n")
    assert victim.read_text() == "original\n"


def test_own_link_in_a_sticky_directory_is_followed(tmp_path, sticky):
    target = tmp_path / "mine"
    target.write_text("a\n")
    lnk = sticky / "lnk"
    lnk.symlink_to(target)
    write_file(str(lnk), "b\n")
    assert target.read_text() == "b\n"


def test_link_loop_is_an_error(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    assert "error" in write_file(str(a), "x")


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


def test_lf_old_str_matching_several_crlf_spans_reports_the_count(tmp_path):
    p = tmp_path / "win.ini"
    p.write_bytes(b"a\r\nb\r\na\r\nb\r\n")
    r = edit_file(str(p), "a\nb", "x")
    assert "matched 2 times" in r["error"]
    assert p.read_bytes() == b"a\r\nb\r\na\r\nb\r\n"


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


# --- link targets as they occur in the wild -----------------------------------
def test_relative_link_with_dot_dot_is_written_through(tmp_path):
    """The common shape: ~/.claude/CLAUDE.md -> ../repos/x/CLAUDE.md."""
    repo = tmp_path / "repos" / "x"
    repo.mkdir(parents=True)
    real = repo / "CLAUDE.md"
    real.write_text("v1\n")
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    lnk = home / "CLAUDE.md"
    lnk.symlink_to("../../repos/./x/CLAUDE.md")
    r = edit_file(str(lnk), "v1", "v2")
    assert r["status"] == "ok"
    assert lnk.is_symlink()
    assert real.read_text() == "v2\n"
    assert r["written_to"] == str(real)


def test_path_with_dot_and_dot_dot_components(tmp_path):
    (tmp_path / "a").mkdir()
    r = write_file(f"{tmp_path}/a/./../a/f.txt", "x")
    assert (tmp_path / "a" / "f.txt").read_text() == "x"
    assert "written_to" not in r


def test_relative_link_through_a_linked_directory(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "f").write_text("a\n")
    (tmp_path / "dirlink").symlink_to("real")
    (tmp_path / "flink").symlink_to("dirlink/f")
    write_file(str(tmp_path / "flink"), "b\n")
    assert (real_dir / "f").read_text() == "b\n"


def test_edit_refuses_a_file_that_grows_past_the_limit_while_read(monkeypatch):
    """/proc files report st_size 0 and then return content."""
    monkeypatch.setattr(server, "_EDIT_MAX_BYTES", 10)
    r = edit_file("/proc/self/status", "Name", "x")
    assert "grew past" in r["error"]
