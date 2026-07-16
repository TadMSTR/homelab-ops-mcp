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
