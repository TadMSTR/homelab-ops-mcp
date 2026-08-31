"""Tests for the shelled-out child's environment allowlist.

Two modes: shadow (the default) forwards everything but reports what
enforcement would withhold; enforcement passes only the allowlist. The PM2 IPC
strip (HLOPS-1) must hold in both.
"""

import pytest

from homelab_ops_mcp import server
from homelab_ops_mcp.server import (
    _ALLOWLIST_ENV_VAR,
    _BASE_CHILD_ENV_ALLOWLIST,
    _ENFORCE_ENV_VAR,
    _PM2_IPC_ENV_VARS,
    _child_env,
    _child_env_allowlist,
    _child_env_enforced,
    run_command,
)


@pytest.fixture()
def shadow(monkeypatch):
    monkeypatch.delenv(_ENFORCE_ENV_VAR, raising=False)
    monkeypatch.delenv(_ALLOWLIST_ENV_VAR, raising=False)


@pytest.fixture()
def enforcing(monkeypatch):
    monkeypatch.setenv(_ENFORCE_ENV_VAR, "true")
    monkeypatch.delenv(_ALLOWLIST_ENV_VAR, raising=False)


# --- the enforce flag ---------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_enforce_flag_truthy(monkeypatch, value):
    monkeypatch.setenv(_ENFORCE_ENV_VAR, value)
    assert _child_env_enforced() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_enforce_flag_falsy(monkeypatch, value):
    monkeypatch.setenv(_ENFORCE_ENV_VAR, value)
    assert _child_env_enforced() is False


def test_enforce_defaults_off(monkeypatch):
    """Shadow mode is the default — deploying the code changes nothing."""
    monkeypatch.delenv(_ENFORCE_ENV_VAR, raising=False)
    assert _child_env_enforced() is False


# --- the allowlist ------------------------------------------------------------
def test_allowlist_base_only_by_default(shadow):
    assert _child_env_allowlist() == _BASE_CHILD_ENV_ALLOWLIST


def test_allowlist_extends_with_exact_names(monkeypatch):
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, "FOO_ONE, FOO_TWO ,,")
    allowed = _child_env_allowlist()
    assert "FOO_ONE" in allowed
    assert "FOO_TWO" in allowed
    assert allowed >= _BASE_CHILD_ENV_ALLOWLIST


@pytest.mark.parametrize("pattern", ["GRAFANA_*", "FOO?", "LC_[A-Z]"])
def test_allowlist_refuses_globs(monkeypatch, recorder, pattern):
    """A prefix would auto-promote every future key sharing it (SC-06)."""
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, pattern)
    allowed = _child_env_allowlist()
    assert allowed == _BASE_CHILD_ENV_ALLOWLIST
    assert pattern not in allowed
    # refused loudly, not silently ignored
    assert recorder.of("child_env.glob_rejected") == [{"entry": pattern}]


def test_allowlist_glob_does_not_match_anything(monkeypatch, enforcing):
    """The rejected pattern must not be honoured as a pattern either."""
    monkeypatch.setenv("GRAFANA_DS_MAIN_PASSWORD", "s3cret")
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, "GRAFANA_*")
    env, _ = _child_env()
    assert "GRAFANA_DS_MAIN_PASSWORD" not in env


def test_base_allowlist_carries_no_secret_shaped_names():
    """Pin the base set: nothing credential-shaped belongs in it."""
    for name in _BASE_CHILD_ENV_ALLOWLIST:
        low = name.lower()
        assert not any(t in low for t in ("token", "secret", "passw", "key", "cred"))


# --- _child_env: shadow mode --------------------------------------------------
def test_shadow_forwards_everything(monkeypatch, shadow):
    monkeypatch.setenv("FORGE_TEST_TOKEN", "leaky")
    env, withheld = _child_env()
    assert env["FORGE_TEST_TOKEN"] == "leaky"
    assert withheld > 0


def test_shadow_still_counts_what_enforcement_would_withhold(monkeypatch, shadow):
    _, before = _child_env()
    monkeypatch.setenv("FORGE_TEST_EXTRA_ONE", "x")
    monkeypatch.setenv("FORGE_TEST_EXTRA_TWO", "y")
    _, after = _child_env()
    assert after == before + 2


def test_shadow_and_enforce_report_the_same_count(monkeypatch):
    monkeypatch.delenv(_ALLOWLIST_ENV_VAR, raising=False)
    monkeypatch.setenv(_ENFORCE_ENV_VAR, "false")
    _, shadow_count = _child_env()
    monkeypatch.setenv(_ENFORCE_ENV_VAR, "true")
    _, enforce_count = _child_env()
    assert shadow_count == enforce_count


# --- _child_env: enforcement --------------------------------------------------
def test_enforce_withholds_unlisted(monkeypatch, enforcing):
    monkeypatch.setenv("FORGE_TEST_TOKEN", "leaky")
    env, withheld = _child_env()
    assert "FORGE_TEST_TOKEN" not in env
    assert withheld > 0
    assert set(env) <= _BASE_CHILD_ENV_ALLOWLIST


def test_enforce_keeps_the_essentials(monkeypatch, enforcing):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/ted")
    env, _ = _child_env()
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/ted"


def test_enforce_honours_the_extension(monkeypatch):
    monkeypatch.setenv(_ENFORCE_ENV_VAR, "true")
    monkeypatch.setenv("FORGE_TEST_NEEDED", "wanted")
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, "FORGE_TEST_NEEDED")
    env, _ = _child_env()
    assert env["FORGE_TEST_NEEDED"] == "wanted"


def test_withheld_count_matches_the_difference(monkeypatch, enforcing):
    env, withheld = _child_env()
    parent = server._clean_env()
    assert withheld == len(parent) - len(env)


# --- HLOPS-1 regression, both modes ------------------------------------------
@pytest.mark.parametrize("enforce", ["false", "true"])
def test_pm2_ipc_vars_excluded_in_both_modes(monkeypatch, enforce):
    monkeypatch.setenv(_ENFORCE_ENV_VAR, enforce)
    monkeypatch.delenv(_ALLOWLIST_ENV_VAR, raising=False)
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    env, _ = _child_env()
    for var in _PM2_IPC_ENV_VARS:
        assert var not in env


@pytest.mark.parametrize("enforce", ["false", "true"])
def test_pm2_ipc_vars_cannot_be_readded_via_the_extension(monkeypatch, enforce):
    """The extension var must not be a way back to the HLOPS-1 crash."""
    monkeypatch.setenv(_ENFORCE_ENV_VAR, enforce)
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, ",".join(_PM2_IPC_ENV_VARS))
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    env, _ = _child_env()
    for var in _PM2_IPC_ENV_VARS:
        assert var not in env


def test_base_allowlist_names_no_pm2_ipc_var():
    assert not (_BASE_CHILD_ENV_ALLOWLIST & set(_PM2_IPC_ENV_VARS))


# --- end to end through run_command ------------------------------------------
def test_run_command_child_sees_ambient_var_in_shadow_mode(monkeypatch, shadow):
    """Retargeted from the pre-allowlist test: shadow mode preserves today's behaviour."""
    monkeypatch.setenv("FORGE_TEST_VAR", "present")
    r = run_command("echo v=$FORGE_TEST_VAR")
    assert r["stdout"] == "v=present\n"


def test_run_command_child_loses_ambient_var_when_enforcing(monkeypatch, enforcing):
    monkeypatch.setenv("FORGE_TEST_VAR", "present")
    r = run_command("echo v=[$FORGE_TEST_VAR]")
    assert r["exit_code"] == 0
    assert r["stdout"] == "v=[]\n"


def test_run_command_sourcing_a_file_still_works_when_enforcing(monkeypatch, enforcing, tmp_path):
    """The documented pattern survives: `source` reads from disk inside the child."""
    envfile = tmp_path / "creds.env"
    envfile.write_text("FORGE_TEST_SOURCED=from-disk\n")
    r = run_command(f"source {envfile} && echo v=$FORGE_TEST_SOURCED", cwd=str(tmp_path))
    assert r["exit_code"] == 0
    assert r["stdout"] == "v=from-disk\n"


# --- the withheld_count log line ---------------------------------------------
def test_done_logs_withheld_count(recorder, shadow):
    run_command("echo hi")
    (kw,) = recorder.of("run_command.done")
    assert kw["env_withheld_count"] > 0
    assert kw["env_enforced"] is False


def test_done_logs_enforced_true_when_enforcing(recorder, enforcing):
    run_command("echo hi")
    (kw,) = recorder.of("run_command.done")
    assert kw["env_enforced"] is True


def test_timeout_logs_withheld_count(recorder, shadow):
    run_command("sleep 5", timeout=1)
    (kw,) = recorder.of("run_command.timeout")
    assert kw["env_withheld_count"] > 0
    assert kw["env_enforced"] is False


def test_error_logs_withheld_count(monkeypatch, recorder, shadow):
    def boom(*a, **k):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(server.subprocess, "Popen", boom)
    run_command("echo hi")
    (kw,) = recorder.of("run_command.error")
    assert kw["env_withheld_count"] > 0
    assert kw["env_enforced"] is False


def test_no_env_names_or_values_are_logged(monkeypatch, recorder, shadow):
    """Only the count may be logged — never a name, never a value."""
    monkeypatch.setenv("FORGE_TEST_SENTINEL_NAME", "sentinel-value-xyz")
    run_command("echo hi")
    blob = repr(recorder.events)
    assert "FORGE_TEST_SENTINEL_NAME" not in blob
    assert "sentinel-value-xyz" not in blob


# --- which callers enforcement would actually break ---------------------------
def test_referenced_withheld_reports_a_var_the_command_reads(monkeypatch, recorder, shadow):
    monkeypatch.setenv("FORGE_TEST_AMBIENT", "v")
    run_command("echo $FORGE_TEST_AMBIENT")
    (kw,) = recorder.of("run_command.env_referenced_withheld")
    assert kw["names"] == ["FORGE_TEST_AMBIENT"]
    assert kw["enforced"] is False


def test_referenced_withheld_handles_brace_syntax(monkeypatch, recorder, shadow):
    monkeypatch.setenv("FORGE_TEST_AMBIENT", "v")
    run_command("echo ${FORGE_TEST_AMBIENT}")
    (kw,) = recorder.of("run_command.env_referenced_withheld")
    assert kw["names"] == ["FORGE_TEST_AMBIENT"]


def test_referenced_withheld_silent_for_allowlisted_vars(recorder, shadow):
    run_command("echo $HOME/$PATH")
    assert recorder.of("run_command.env_referenced_withheld") == []


def test_referenced_withheld_silent_for_unset_names(recorder, shadow):
    """Withholding a variable that is not set changes nothing, so do not report it."""
    run_command("echo $FORGE_TEST_DEFINITELY_UNSET")
    assert recorder.of("run_command.env_referenced_withheld") == []


def test_referenced_withheld_ignores_shell_positionals(recorder, shadow):
    run_command('set -- a b; echo "$1 $2 $@ $# $?"')
    assert recorder.of("run_command.env_referenced_withheld") == []


def test_referenced_withheld_dedupes_and_sorts(monkeypatch, recorder, shadow):
    monkeypatch.setenv("FORGE_TEST_BBB", "1")
    monkeypatch.setenv("FORGE_TEST_AAA", "2")
    run_command("echo $FORGE_TEST_BBB $FORGE_TEST_AAA $FORGE_TEST_BBB")
    (kw,) = recorder.of("run_command.env_referenced_withheld")
    assert kw["names"] == ["FORGE_TEST_AAA", "FORGE_TEST_BBB"]


def test_referenced_withheld_respects_the_extension_var(monkeypatch, recorder):
    monkeypatch.delenv(_ENFORCE_ENV_VAR, raising=False)
    monkeypatch.setenv("FORGE_TEST_AMBIENT", "v")
    monkeypatch.setenv(_ALLOWLIST_ENV_VAR, "FORGE_TEST_AMBIENT")
    run_command("echo $FORGE_TEST_AMBIENT")
    assert recorder.of("run_command.env_referenced_withheld") == []


def test_referenced_withheld_logs_no_values(monkeypatch, recorder, shadow):
    monkeypatch.setenv("FORGE_TEST_AMBIENT", "sentinel-value-xyz")
    run_command("echo $FORGE_TEST_AMBIENT")
    assert "sentinel-value-xyz" not in repr(recorder.events)


def test_referenced_withheld_predicts_the_break(monkeypatch, recorder, enforcing):
    """The signal and the breakage agree: reported name, empty expansion."""
    monkeypatch.setenv("FORGE_TEST_AMBIENT", "present")
    r = run_command("echo v=[$FORGE_TEST_AMBIENT]")
    (kw,) = recorder.of("run_command.env_referenced_withheld")
    assert kw["names"] == ["FORGE_TEST_AMBIENT"]
    assert r["stdout"] == "v=[]\n"


def test_sourcing_pattern_is_not_flagged(recorder, enforcing, tmp_path):
    """The documented `source` pattern is safe, and must not raise a false alarm."""
    envfile = tmp_path / "creds.env"
    envfile.write_text("FORGE_TEST_SOURCED=from-disk\n")
    r = run_command(f"source {envfile} && echo $FORGE_TEST_SOURCED", cwd=str(tmp_path))
    assert r["stdout"] == "from-disk\n"
    assert recorder.of("run_command.env_referenced_withheld") == []
