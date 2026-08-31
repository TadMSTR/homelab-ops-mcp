"""Tests for run_command and the PM2 IPC env-sanitisation fix (HLOPS-1)."""

from homelab_ops_mcp import server
from homelab_ops_mcp.server import _PM2_IPC_ENV_VARS, _clean_env, run_command


def test_echo_success():
    r = run_command("echo hello")
    assert r["exit_code"] == 0
    assert r["stdout"] == "hello\n"
    assert r["stderr"] == ""


def test_nonzero_exit_code():
    r = run_command("exit 3")
    assert r["exit_code"] == 3


def test_stderr_captured():
    r = run_command("echo oops 1>&2")
    assert r["stderr"] == "oops\n"
    assert r["exit_code"] == 0


def test_cwd_respected(tmp_path):
    (tmp_path / "marker").write_text("x")
    r = run_command("ls", cwd=str(tmp_path))
    assert "marker" in r["stdout"]


def test_timeout():
    r = run_command("sleep 5", timeout=1)
    assert r["exit_code"] == -1
    assert "timed out" in r["stderr"]


def test_clean_env_strips_pm2_vars(monkeypatch):
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    monkeypatch.setenv("KEEP_ME", "yes")

    env = _clean_env()

    for var in _PM2_IPC_ENV_VARS:
        assert var not in env
    assert env["KEEP_ME"] == "yes"


def test_run_command_child_does_not_see_pm2_vars(monkeypatch):
    """Regression for HLOPS-1: children must not inherit PM2 IPC vars."""
    monkeypatch.setenv("NODE_CHANNEL_FD", "3")
    monkeypatch.setenv("NODE_CHANNEL_SERIALIZATION_MODE", "json")
    monkeypatch.setenv("NODE_UNIQUE_ID", "0")

    r = run_command("echo fd=[$NODE_CHANNEL_FD] mode=[$NODE_CHANNEL_SERIALIZATION_MODE]")
    assert r["exit_code"] == 0
    assert r["stdout"] == "fd=[] mode=[]\n"


def test_run_command_exception_path(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(server.subprocess, "run", boom)
    r = run_command("echo hi")
    assert r["exit_code"] == -1
    assert "subprocess exploded" in r["stderr"]
