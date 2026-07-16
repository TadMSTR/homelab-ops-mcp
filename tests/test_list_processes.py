"""Tests for list_processes."""

from homelab_ops_mcp.server import list_processes


def test_returns_processes():
    r = list_processes()
    assert r["count"] > 0
    assert r["sort_by"] == "cpu"
    first = r["processes"][0]
    assert {"pid", "name", "cpu_percent", "mem_percent", "status"} <= set(first)


def test_limit_applied():
    r = list_processes(limit=3)
    assert r["count"] <= 3
    assert len(r["processes"]) <= 3


def test_sort_by_pid_ascending():
    r = list_processes(sort_by="pid", limit=50)
    pids = [p["pid"] for p in r["processes"]]
    assert pids == sorted(pids)


def test_sort_by_mem():
    r = list_processes(sort_by="mem", limit=10)
    mems = [p["mem_percent"] for p in r["processes"]]
    assert mems == sorted(mems, reverse=True)


def test_unknown_sort_falls_back_to_cpu():
    r = list_processes(sort_by="bogus", limit=5)
    assert r["sort_by"] == "bogus"  # echoed back, but sorted by cpu
    cpus = [p["cpu_percent"] for p in r["processes"]]
    assert cpus == sorted(cpus, reverse=True)


def test_name_filter():
    # The test runner itself is a python process; filter should match it.
    r = list_processes(name_filter="python", limit=100)
    assert all("python" in (p["name"] or "").lower() for p in r["processes"])
