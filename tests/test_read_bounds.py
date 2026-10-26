"""read_file ranges and size cap, and read_multiple_files' shared budget.

Fingerprints: 0523865f5fbb7d8b (start clamp unpinned), 7b1595070a8263ec
(end_line <= 0), f37b6ce5386cdd83 (no size cap on reads).
"""

import time

import pytest

from homelab_ops_mcp.server import _OUTPUT_LIMIT_ENV_VAR, read_file, read_multiple_files


@pytest.fixture()
def hundred(tmp_path):
    p = tmp_path / "hundred.txt"
    p.write_text("".join(f"line{i}\n" for i in range(1, 101)))
    return p


@pytest.fixture()
def small_cap(monkeypatch):
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, "1000")
    return 1000


# --- the start clamp, pinned where an unclamped slice differs ----------------
def test_negative_start_reads_from_the_head_not_the_tail(hundred):
    """``lines[-2:3]`` would be empty; ``lines[-2:]`` the tail. The clamp gives the head."""
    r = read_file(str(hundred), start_line=-1, end_line=3)
    assert r["content"] == "line1\nline2\nline3\n"
    assert r["returned_lines"] == "1-3"


def test_zero_start_reads_from_the_head(hundred):
    r = read_file(str(hundred), start_line=0, end_line=2)
    assert r["content"] == "line1\nline2\n"
    assert r["returned_lines"] == "1-2"


# --- end_line validation -----------------------------------------------------
@pytest.mark.parametrize("end", [0, -1, -50])
def test_end_line_below_one_is_refused(hundred, end):
    r = read_file(str(hundred), end_line=end)
    assert "end_line must be at least 1" in r["error"]
    assert "content" not in r


def test_end_before_start_is_refused(hundred):
    r = read_file(str(hundred), start_line=10, end_line=5)
    assert "before start_line" in r["error"]


def test_start_past_the_end_returns_nothing_and_says_so(hundred):
    r = read_file(str(hundred), start_line=500)
    assert r["content"] == ""
    assert r["returned_lines"] == "none"
    assert r["total_lines"] == 100


def test_end_past_the_end_is_clamped_in_the_label(hundred):
    r = read_file(str(hundred), start_line=99, end_line=1000)
    assert r["content"] == "line99\nline100\n"
    assert r["returned_lines"] == "99-100"


# --- line counting and fidelity ----------------------------------------------
def test_last_line_without_newline_is_counted(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"a\nb")
    r = read_file(str(p))
    assert r["total_lines"] == 2
    assert r["content"] == "a\nb"


def test_empty_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"")
    r = read_file(str(p))
    assert r["total_lines"] == 0
    assert r["content"] == ""
    assert r["truncated"] is False


def test_crlf_is_returned_as_on_disk(tmp_path):
    """What read_file shows must be what edit_file can match."""
    p = tmp_path / "f"
    p.write_bytes(b"one\r\ntwo\r\n")
    r = read_file(str(p))
    assert r["content"] == "one\r\ntwo\r\n"
    assert r["total_lines"] == 2


def test_line_longer_than_a_read_chunk_is_one_line(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * 200_000 + b"\nshort\n")
    r = read_file(str(p), start_line=2, end_line=2)
    assert r["content"] == "short\n"
    assert r["total_lines"] == 2


# --- the size cap ------------------------------------------------------------
def test_full_read_is_capped_and_flagged(tmp_path, small_cap):
    p = tmp_path / "big"
    p.write_text("y" * 99 + "\n" * 1 + ("z" * 99 + "\n") * 50)  # 5100 bytes
    r = read_file(str(p))
    assert len(r["content"].encode()) == small_cap
    assert r["truncated"] is True
    # Reading stopped at the cap, so the rest was not counted.
    assert r["total_lines"] is None
    # Says where it stopped, so the caller can continue from there.
    assert r["returned_lines"] == "1-10"


def test_short_file_is_not_flagged(sample_file, small_cap):
    r = read_file(str(sample_file))
    assert r["truncated"] is False
    assert "returned_lines" not in r


def test_ranged_read_deep_in_a_capped_file_still_works(tmp_path, small_cap):
    """The cap bounds what is returned, not how far into the file a range may start."""
    p = tmp_path / "big"
    p.write_text("".join(f"{i:099d}\n" for i in range(1, 501)))  # 50 KB
    r = read_file(str(p), start_line=400, end_line=402)
    assert r["content"].splitlines() == [f"{i:099d}" for i in (400, 401, 402)]
    assert r["truncated"] is False
    assert r["total_lines"] is None


def test_read_multiple_files_shares_one_budget(tmp_path, small_cap):
    paths = []
    for name in "abc":
        p = tmp_path / name
        p.write_text("q" * 600)
        paths.append(str(p))
    r = read_multiple_files(paths)
    files = r["files"]
    assert files[paths[0]]["truncated"] is False
    assert files[paths[1]]["truncated"] is True
    assert len(files[paths[1]]["content"]) == small_cap - 600
    assert "output budget" in files[paths[2]]["error"]
    total = sum(len(f.get("content", "")) for f in files.values())
    assert total == small_cap


# --- reading stops when the range or budget is done (CR-01) -------------------
def test_short_range_at_the_head_of_a_huge_file_does_not_read_it_all(tmp_path):
    """Measured before: a 5-line range on 200 MB scanned all 2,000,000 lines."""
    p = tmp_path / "huge"
    with open(p, "wb") as f:
        f.write(b"one\ntwo\nthree\n")
        f.seek(4 * 1024**3)  # sparse: 4 GiB of zeros, no newlines
        f.write(b"\n")
    started = time.monotonic()
    r = read_file(str(p), start_line=1, end_line=2)
    assert time.monotonic() - started < 1
    assert r["content"] == "one\ntwo\n"
    assert r["total_lines"] is None


def test_range_reaching_the_end_still_counts_every_line(hundred):
    r = read_file(str(hundred), start_line=95, end_line=100)
    assert r["total_lines"] == 100


def test_full_read_under_the_cap_counts_every_line(hundred):
    assert read_file(str(hundred))["total_lines"] == 100


# --- the cap counts decoded text (F-06) ---------------------------------------
def test_invalid_utf8_cannot_return_more_than_the_cap(tmp_path, small_cap):
    """Each 0xFF becomes U+FFFD (3 bytes); counting raw bytes returned 3x the cap."""
    p = tmp_path / "binary"
    p.write_bytes(b"\xff" * 2000)
    r = read_file(str(p))
    assert len(r["content"].encode()) <= small_cap
    assert r["truncated"] is True


def test_cap_never_splits_a_character(tmp_path, small_cap):
    p = tmp_path / "euro"
    p.write_text("€" * 2000)  # 3 bytes each
    r = read_file(str(p))
    assert set(r["content"]) == {"€"}
    assert len(r["content"].encode()) == 999


def test_character_split_across_read_chunks_is_decoded_whole(tmp_path):
    p = tmp_path / "split"
    body = "x" * (65536 - 1) + "€" + "\n"  # the € straddles the first readline chunk
    p.write_text(body)
    assert read_file(str(p))["content"] == body


def test_truncated_utf8_sequence_at_end_of_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"ok\xe2\x82")  # the first two bytes of a three-byte character
    assert read_file(str(p))["content"] == "ok�"


def test_a_line_none_of_which_fit_is_not_reported_returned(tmp_path, monkeypatch):
    """R2-02: the budget ran out inside line 2's first character."""
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, "4")
    p = tmp_path / "f"
    p.write_bytes("ab\n€x\n".encode())
    r = read_file(str(p))
    assert r["content"] == "ab\n"
    assert r["returned_lines"] == "1-1"
    assert r["truncated"] is True


def test_a_dropped_tail_sets_truncated(tmp_path, monkeypatch):
    """R2-02: the final U+FFFD did not fit, and was dropped silently."""
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, "3")
    p = tmp_path / "f"
    p.write_bytes(b"abc\xe2\x82")
    r = read_file(str(p))
    assert r["content"] == "abc"
    assert r["truncated"] is True
