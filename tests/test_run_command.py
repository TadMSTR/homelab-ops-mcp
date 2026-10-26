"""Tests for run_command and the PM2 IPC env-sanitisation fix (HLOPS-1)."""

import shlex
import time

import psutil
import pytest

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

    # Retargeted from subprocess.run onto Popen: capture is now incremental, so
    # run() is no longer the call that can fail here.
    monkeypatch.setattr(server.subprocess, "Popen", boom)
    r = run_command("echo hi")
    assert r["exit_code"] == -1
    assert "subprocess exploded" in r["stderr"]


# --- the timeout bounds the exit, not just the pipes (28af99c350d74f2f) -------
def test_timeout_applies_after_the_command_closes_its_stdio():
    """``exec >/dev/null 2>&1`` hits EOF on both pipes at once and keeps running.

    The deadline used to be checked only while a pipe was open, after which
    proc.wait() blocked with no bound and nothing was killed.
    """
    started = time.monotonic()
    r = run_command("exec >/dev/null 2>&1; sleep 6", timeout=1)
    assert time.monotonic() - started < 4
    assert r["exit_code"] == -1
    assert "timed out" in r["stderr"]


def test_detached_stdio_command_is_killed_at_the_deadline(tmp_path):
    pidfile = tmp_path / "pid"
    run_command(f"echo $$ > {shlex.quote(str(pidfile))}; exec >/dev/null 2>&1; sleep 30", timeout=1)
    pid = int(pidfile.read_text())
    # Reaped as well as signalled: the pid no longer exists at all.
    assert not psutil.pid_exists(pid)


def test_detached_stdio_command_that_finishes_in_time_reports_its_exit():
    r = run_command("exec >/dev/null 2>&1; exit 4", timeout=5)
    assert r["exit_code"] == 4
    assert r["error"] is None


def test_unreapable_child_does_not_hang_the_call(monkeypatch):
    """After SIGKILL the reap is bounded too (a D-state process cannot be reaped)."""
    real_popen = server.subprocess.Popen

    class Stuck(real_popen):
        def wait(self, timeout=None):
            raise server.subprocess.TimeoutExpired(self.args, timeout)

    monkeypatch.setattr(server.subprocess, "Popen", Stuck)
    r = run_command("exec >/dev/null 2>&1; sleep 30", timeout=1)
    assert r["exit_code"] == -1
    assert "timed out" in r["stderr"]


# --- one response shape; tool failures set error (478494ad697df5d2) ----------
KEYS = {"stdout", "stderr", "exit_code", "truncated", "execution_time", "error"}


def test_invalid_cwd_has_the_full_shape_and_an_error():
    r = run_command("true", cwd="relative/dir")
    assert set(r) == KEYS
    assert r["exit_code"] == -1
    assert "absolute" in r["error"]
    assert r["truncated"] is False


def test_spawn_failure_sets_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("EMFILE")

    monkeypatch.setattr(server.subprocess, "Popen", boom)
    r = run_command("true")
    assert set(r) == KEYS
    assert r["error"] == "EMFILE"


def test_missing_cwd_directory_sets_error(tmp_path):
    r = run_command("true", cwd=str(tmp_path / "gone"))
    assert set(r) == KEYS
    assert r["error"]


@pytest.mark.parametrize(
    ("command", "timeout"),
    [("true", 5), ("exit 3", 5), ("echo x 1>&2", 5), ("sleep 5", 1)],
)
def test_command_outcomes_have_the_full_shape_and_no_error(command, timeout):
    """A non-zero exit or a timeout is the command's outcome; the tool worked."""
    r = run_command(command, timeout=timeout)
    assert set(r) == KEYS
    assert r["error"] is None
