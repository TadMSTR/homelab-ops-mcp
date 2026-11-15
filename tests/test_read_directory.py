"""Tests for read_directory."""

from homelab_ops_mcp.server import read_directory


def test_lists_entries(tmp_path):
    (tmp_path / "f1.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    r = read_directory(str(tmp_path))
    names = {e["name"]: e for e in r["entries"]}
    assert r["count"] == 2
    assert names["f1.txt"]["type"] == "file"
    assert names["f1.txt"]["size_bytes"] == 1
    assert names["sub"]["type"] == "dir"
    assert "children" not in names["sub"]


def test_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("x")
    r = read_directory(str(tmp_path), recursive=True, max_depth=2)
    sub_entry = next(e for e in r["entries"] if e["name"] == "sub")
    assert sub_entry["children"][0]["name"] == "inner.txt"


def test_recursive_depth_limit(tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("x")
    r = read_directory(str(tmp_path), recursive=True, max_depth=1)
    a_entry = next(e for e in r["entries"] if e["name"] == "a")
    # depth 1 stops before recursing into a/
    assert "children" not in a_entry


def test_not_found(tmp_path):
    r = read_directory(str(tmp_path / "nope"))
    assert "not found" in r["error"].lower()


def test_not_a_directory(sample_file):
    r = read_directory(str(sample_file))
    assert "not a directory" in r["error"].lower()


# --- vikunja#1249: types from lstat, symlinks not followed, bounded walk -----------


def test_types_come_from_lstat(tmp_path):
    import os
    import socket

    (tmp_path / "f").write_text("abc")
    (tmp_path / "d").mkdir()
    (tmp_path / "link-to-file").symlink_to(tmp_path / "f")
    (tmp_path / "link-to-dir").symlink_to(tmp_path / "d")
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    os.mkfifo(tmp_path / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(tmp_path / "sock"))
    try:
        r = read_directory(str(tmp_path), recursive=True)
    finally:
        sock.close()
    by = {e["name"]: e for e in r["entries"]}
    assert by["f"]["type"] == "file"
    assert by["f"]["size_bytes"] == 3
    assert by["d"]["type"] == "dir"
    for name, target in (
        ("link-to-file", "f"),
        ("link-to-dir", "d"),
        ("dangling", "missing"),
    ):
        assert by[name]["type"] == "symlink"
        assert by[name]["target"] == str(tmp_path / target)
        assert "size_bytes" not in by[name]
    assert by["fifo"]["type"] == "other"
    assert by["sock"]["type"] == "other"
    # A link to a directory is not recursed into, even with recursive=True.
    assert "children" not in by["link-to-dir"]
    assert "children" in by["d"]


def test_symlink_loop_is_reported_not_walked(tmp_path):
    loop = tmp_path / "a"
    loop.mkdir()
    (loop / "back").symlink_to(tmp_path)
    r = read_directory(str(tmp_path), recursive=True, max_depth=10)
    [a] = r["entries"]
    [back] = a["children"]
    assert back["type"] == "symlink"
    assert "children" not in back
    assert r["total_entries"] == 2
    assert r["truncated"] is False
    assert "truncated_at" not in r


def test_entry_cap_truncates_and_says_where(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    monkeypatch.setattr(server, "_DIR_MAX_ENTRIES", 5)
    first = tmp_path / "a"
    first.mkdir()
    for i in range(4):
        (first / f"f{i}").write_text("")
    (tmp_path / "b").mkdir()
    (tmp_path / "c").write_text("")
    r = read_directory(str(tmp_path), recursive=True)
    # a + its four files = 5; the walk stops inside a's parent before b.
    assert r["total_entries"] == 5
    assert r["truncated"] is True
    assert r["truncated_at"] == str(tmp_path)
    assert [e["name"] for e in r["entries"]] == ["a"]
    assert len(r["entries"][0]["children"]) == 4


def test_entry_cap_reports_the_deepest_stopping_directory(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    monkeypatch.setattr(server, "_DIR_MAX_ENTRIES", 3)
    deep = tmp_path / "a"
    deep.mkdir()
    for i in range(5):
        (deep / f"f{i}").write_text("")
    r = read_directory(str(tmp_path), recursive=True)
    assert r["total_entries"] == 3
    assert r["truncated_at"] == str(deep)


def test_exactly_at_the_cap_is_not_truncated(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    monkeypatch.setattr(server, "_DIR_MAX_ENTRIES", 3)
    for i in range(3):
        (tmp_path / f"f{i}").write_text("")
    r = read_directory(str(tmp_path))
    assert r["total_entries"] == 3
    assert r["truncated"] is False


def test_real_cap_on_a_large_tree(tmp_path):
    from homelab_ops_mcp import server

    for d in range(11):
        sub = tmp_path / f"d{d:02}"
        sub.mkdir()
        for i in range(1000):
            (sub / f"f{i:04}").touch()
    r = read_directory(str(tmp_path), recursive=True)
    assert r["truncated"] is True
    assert r["total_entries"] == server._DIR_MAX_ENTRIES == 10_000


def test_max_depth_is_clamped(tmp_path):
    p = tmp_path
    for i in range(12):
        p = p / f"l{i}"
    p.mkdir(parents=True)
    r = read_directory(str(tmp_path), recursive=True, max_depth=50)
    assert r["max_depth"] == 10
    depth, node = 0, r["entries"]
    while node and "children" in node[0]:
        node = node[0]["children"]
        depth += 1
    assert depth == 9  # levels 1..10 listed; l9's children are not
    r = read_directory(str(tmp_path), recursive=True, max_depth=0)
    assert r["max_depth"] == 1
    assert "children" not in r["entries"][0]


def test_non_recursive_omits_max_depth(tmp_path):
    r = read_directory(str(tmp_path))
    assert "max_depth" not in r
    assert r == {
        "path": str(tmp_path),
        "entries": [],
        "count": 0,
        "total_entries": 0,
        "truncated": False,
    }


def test_lstat_failure_is_reported_as_unknown(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    (tmp_path / "x").write_text("")
    real = server.Path.lstat

    def _lstat(self, *a, **k):
        if self.name == "x":
            raise FileNotFoundError("gone")
        return real(self, *a, **k)

    monkeypatch.setattr(server.Path, "lstat", _lstat)
    r = read_directory(str(tmp_path))
    assert r["entries"] == [{"name": "x", "path": str(tmp_path / "x"), "type": "unknown"}]


# --- quickwins audit: bounded read, no empty children at the cap, OSError per directory ---


def test_a_huge_directory_is_read_only_up_to_the_budget(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    monkeypatch.setattr(server, "_DIR_MAX_ENTRIES", 5)
    for i in range(50):
        (tmp_path / f"f{i:02}").touch()
    pulled = []
    real = server.os.scandir

    class _Counting:
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *a):
            return self._it.__exit__(*a)

        def __iter__(self):
            for de in self._it:
                pulled.append(de.name)
                yield de

    monkeypatch.setattr(server.os, "scandir", lambda p: _Counting(real(p)))
    r = read_directory(str(tmp_path))
    assert r["total_entries"] == 5
    assert r["truncated"] is True
    assert r["truncated_at"] == str(tmp_path)
    # budget + 1 (to learn there was more), not all 50
    assert len(pulled) == 6


def test_a_subdirectory_reached_at_the_cap_is_marked_not_empty(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    monkeypatch.setattr(server, "_DIR_MAX_ENTRIES", 4)
    for i in range(3):
        (tmp_path / f"a{i}").touch()
    zz = tmp_path / "zz"
    zz.mkdir()
    for i in range(5):
        (zz / f"f{i}").touch()
    calls = []
    real = server.os.scandir
    monkeypatch.setattr(server.os, "scandir", lambda p: calls.append(str(p)) or real(p))
    r = read_directory(str(tmp_path), recursive=True)
    by = {e["name"]: e for e in r["entries"]}
    assert r["total_entries"] == 4
    assert r["truncated"] is True
    assert r["truncated_at"] == str(zz)
    assert by["zz"]["truncated"] is True
    assert "children" not in by["zz"]
    assert str(zz) not in calls  # never read


def _also_fail_iterdir(monkeypatch, server, scandir):
    """Fail Path.iterdir the same way, so the test also fails on an iterdir-based walk."""
    real = server.Path.iterdir

    def _iterdir(self):
        scandir(self).close()
        return real(self)

    monkeypatch.setattr(server.Path, "iterdir", _iterdir)


def test_a_vanished_subdirectory_keeps_its_siblings(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    (tmp_path / "keep.txt").write_text("x")
    gone = tmp_path / "gone"
    gone.mkdir()
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "inner").touch()
    real = server.os.scandir

    def _scandir(p):
        if str(p) == str(gone):
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real(p)

    monkeypatch.setattr(server.os, "scandir", _scandir)
    _also_fail_iterdir(monkeypatch, server, _scandir)
    r = read_directory(str(tmp_path), recursive=True)
    assert "error" not in r
    by = {e["name"]: e for e in r["entries"]}
    assert by["keep.txt"]["type"] == "file"
    assert by["ok"]["children"][0]["name"] == "inner"
    assert by["gone"]["children"] == [{"name": str(gone), "error": "No such file or directory"}]


def test_a_subdirectory_listing_error_is_logged(tmp_path, monkeypatch, recorder):
    from homelab_ops_mcp import server

    gone = tmp_path / "gone"
    gone.mkdir()
    real = server.os.scandir

    def _scandir(p):
        if str(p) == str(gone):
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real(p)

    monkeypatch.setattr(server.os, "scandir", _scandir)
    _also_fail_iterdir(monkeypatch, server, _scandir)
    read_directory(str(tmp_path), recursive=True)
    (kw,) = recorder.of("read_directory.list_error")
    assert kw["path"] == str(gone)
    assert kw["error"] == "No such file or directory"


def test_a_subdirectory_replaced_by_a_file_keeps_its_siblings(tmp_path, monkeypatch):
    from homelab_ops_mcp import server

    (tmp_path / "keep.txt").write_text("x")
    d = tmp_path / "d"
    d.mkdir()
    real = server.os.scandir

    def _scandir(p):
        if str(p) == str(d):
            raise NotADirectoryError(20, "Not a directory", str(p))
        return real(p)

    monkeypatch.setattr(server.os, "scandir", _scandir)
    _also_fail_iterdir(monkeypatch, server, _scandir)
    r = read_directory(str(tmp_path), recursive=True)
    by = {e["name"]: e for e in r["entries"]}
    assert "keep.txt" in by
    assert by["d"]["children"][0]["error"] == "Not a directory"
