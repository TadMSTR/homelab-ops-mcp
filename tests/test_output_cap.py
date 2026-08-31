"""Tests for the per-stream output cap on run_command."""

import os
import time

import psutil
import pytest

from homelab_ops_mcp.server import (
    _DEFAULT_OUTPUT_LIMIT,
    _OUTPUT_LIMIT_ENV_VAR,
    _output_limit,
    run_command,
)


@pytest.fixture()
def small_cap(monkeypatch):
    """A cap small enough to breach quickly, but larger than a chunk boundary."""
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, "1000")
    return 1000


# --- _output_limit ------------------------------------------------------------
def test_limit_defaults_to_one_mib(monkeypatch):
    monkeypatch.delenv(_OUTPUT_LIMIT_ENV_VAR, raising=False)
    assert _output_limit() == _DEFAULT_OUTPUT_LIMIT == 1024 * 1024


def test_limit_read_from_env(monkeypatch):
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, "4096")
    assert _output_limit() == 4096


@pytest.mark.parametrize("bad", ["nonsense", "1.5", "0", "-1", "  "])
def test_limit_falls_back_on_bad_values(monkeypatch, bad):
    monkeypatch.setenv(_OUTPUT_LIMIT_ENV_VAR, bad)
    assert _output_limit() == _DEFAULT_OUTPUT_LIMIT


# --- under the cap: unchanged behaviour ---------------------------------------
def test_short_output_is_passed_through_untouched(small_cap):
    r = run_command("echo hello")
    assert r["stdout"] == "hello\n"
    assert r["truncated"] is False
    assert "[truncated" not in r["stdout"]


def test_output_just_under_the_cap_is_not_flagged(small_cap):
    r = run_command(f"printf 'x%.0s' $(seq 1 {small_cap - 1})")
    assert len(r["stdout"]) == small_cap - 1
    assert r["truncated"] is False


def test_truncated_field_present_on_every_success(small_cap):
    """Absence of the field is not the same as false — an agent must be able to read it."""
    assert run_command("true")["truncated"] is False


# --- over the cap -------------------------------------------------------------
def test_stdout_over_cap_is_truncated_and_flagged(small_cap):
    r = run_command("yes | head -c 5000000")
    assert r["truncated"] is True
    assert "[truncated" in r["stdout"]
    assert str(small_cap) in r["stdout"]
    # the captured payload itself stops at the cap; the marker is appended after
    payload = r["stdout"].split("\n[truncated")[0]
    assert len(payload.encode()) == small_cap


def test_stderr_capped_independently_of_stdout(small_cap):
    r = run_command("yes | head -c 5000000 1>&2")
    assert r["truncated"] is True
    assert "[truncated" in r["stderr"]
    assert "[truncated" not in r["stdout"]


def test_stdout_capped_independently_of_stderr(small_cap):
    r = run_command("yes | head -c 5000000")
    assert "[truncated" in r["stdout"]
    assert "[truncated" not in r["stderr"]


def test_cap_bounds_what_the_server_holds(small_cap):
    """The point of the cap: a huge command does not land whole in memory."""
    r = run_command("yes | head -c 20000000")
    assert len(r["stdout"].encode()) < small_cap + 200


def test_truncation_is_logged(recorder, small_cap):
    run_command("yes | head -c 5000000")
    (kw,) = recorder.of("run_command.truncated")
    assert kw["limit_bytes"] == small_cap


def test_no_truncation_event_when_under_the_cap(recorder, small_cap):
    run_command("echo hi")
    assert recorder.of("run_command.truncated") == []


# --- the process is reaped ----------------------------------------------------
def _descendant_pids():
    me = psutil.Process(os.getpid())
    return {c.pid for c in me.children(recursive=True)}


def test_breaching_process_is_reaped(small_cap):
    """No orphan is left behind — the whole group goes, not just bash."""
    before = _descendant_pids()
    r = run_command("yes | head -c 5000000")
    assert r["truncated"] is True
    time.sleep(0.3)
    leaked = _descendant_pids() - before
    assert leaked == set(), f"orphaned pids: {leaked}"


def test_unbounded_writer_is_killed_not_waited_on(small_cap):
    """A command that would never finish on its own still returns at the cap."""
    started = time.monotonic()
    r = run_command("yes", timeout=25)
    assert r["truncated"] is True
    assert time.monotonic() - started < 20, "did not stop at the cap"


def test_no_zombie_left_behind(small_cap):
    run_command("yes | head -c 5000000")
    me = psutil.Process(os.getpid())
    zombies = [c for c in me.children(recursive=True) if c.status() == psutil.STATUS_ZOMBIE]
    assert zombies == []


# --- timeout still works, and now reaps the whole group ----------------------
def test_timeout_still_reported(small_cap):
    r = run_command("sleep 5", timeout=1)
    assert r["exit_code"] == -1
    assert "timed out" in r["stderr"]


def test_timeout_reaps_grandchildren(small_cap):
    """bash's own children used to survive the timeout kill."""
    before = _descendant_pids()
    run_command("sleep 30 & sleep 30", timeout=1)
    time.sleep(0.3)
    leaked = _descendant_pids() - before
    assert leaked == set(), f"orphaned pids: {leaked}"


def test_timeout_returns_partial_output(small_cap):
    r = run_command("echo partial; sleep 5", timeout=1)
    assert r["exit_code"] == -1
    assert r["stdout"] == "partial\n"


# --- interaction with the default cap ----------------------------------------
def test_default_cap_allows_a_normal_sized_read(monkeypatch, tmp_path):
    monkeypatch.delenv(_OUTPUT_LIMIT_ENV_VAR, raising=False)
    target = tmp_path / "big.txt"
    target.write_text("line\n" * 20000)  # 100 KB, well under 1 MiB
    r = run_command(f"cat {target}", cwd=str(tmp_path))
    assert r["truncated"] is False
    assert len(r["stdout"]) == 100000
