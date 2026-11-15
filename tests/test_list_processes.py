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
    # Any host running this suite has more than three processes.
    assert r["count"] == 3
    assert len(r["processes"]) == 3


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
    # A python child that outlives the 0.5 s sampling interval. The runner itself is no
    # match: under `uv run pytest` its process name is "pytest", and transient python
    # processes may exit mid-interval and be dropped.
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        r = list_processes(name_filter="python", limit=100_000)
    finally:
        child.kill()
        child.wait()
    # Non-empty first: all() over an empty list is True, so a filter that
    # dropped everything would otherwise pass.
    assert child.pid in {p["pid"] for p in r["processes"]}
    assert all("python" in (p["name"] or "").lower() for p in r["processes"])


def test_cpu_is_sampled_over_an_interval_on_the_first_call():
    """vikunja#1250: a busy process ranks above an idle one on the very first call.

    Both are fresh pids, so psutil has never sampled them: without priming and a wait,
    both read 0.0 and the order between them is arbitrary.
    """
    import subprocess
    import sys
    import time

    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    idle = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        r = list_processes(sort_by="cpu", limit=100_000)
    finally:
        busy.kill()
        idle.kill()
        busy.wait()
        idle.wait()
    by_pid = {p["pid"]: p for p in r["processes"]}
    assert by_pid[busy.pid]["cpu_percent"] > 20
    assert by_pid[idle.pid]["cpu_percent"] < 5
    assert r["cpu_sample_interval_s"] == 0.5


def test_sampling_waits_the_reported_interval(monkeypatch):
    from homelab_ops_mcp import server

    slept = []
    monkeypatch.setattr(server.time, "sleep", slept.append)
    r = list_processes(limit=1)
    assert slept == [0.5]
    assert r["cpu_sample_interval_s"] == 0.5


def test_process_that_exits_during_the_interval_is_skipped(monkeypatch):
    from homelab_ops_mcp import server

    class _Gone:
        calls = 0

        def __init__(self):
            self.info = {"pid": 999_999, "name": "gone", "memory_percent": 0.0, "status": "running"}

        def cpu_percent(self, interval=None):
            _Gone.calls += 1
            if _Gone.calls > 1:
                raise server.psutil.NoSuchProcess(pid=999_999)
            return 0.0

    real_iter = server.psutil.process_iter

    def _iter(attrs):
        yield _Gone()
        yield from real_iter(attrs)

    monkeypatch.setattr(server.psutil, "process_iter", _iter)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    r = list_processes(limit=100_000)
    assert _Gone.calls == 2
    assert 999_999 not in {p["pid"] for p in r["processes"]}
