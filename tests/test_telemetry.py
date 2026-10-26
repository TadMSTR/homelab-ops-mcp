"""Telemetry: off by default, never fatal, and correct about what failed.

The missing-dependency cases SIMULATE the absence of the [telemetry] extra
rather than relying on it (#911). They used to pass only because CI installs
[dev] alone, so they proved nothing there and failed in any venv that had the
extra. ``no_telemetry_extra`` puts ``None`` in ``sys.modules`` for every module
the fallbacks guard, which makes each import raise ImportError whatever is
installed. CI runs this file in both a [dev] and a [dev,telemetry] venv.
"""

import contextlib
import sys
import threading

import pytest

from homelab_ops_mcp import telemetry
from homelab_ops_mcp.server import read_file, run_command, write_file
from tests.conftest import LogRecorder

ALL_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "SYSTEM_OPS_INFLUXDB3_URL",
    "SYSTEM_OPS_INFLUXDB3_TOKEN",
    "SYSTEM_OPS_INFLUXDB3_DATABASE",
    "SYSTEM_OPS_NATS_URL",
    "SYSTEM_OPS_NATS_SUBJECT",
)


@pytest.fixture(autouse=True)
def clean_telemetry(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


# Every module a fallback in telemetry.py guards. ``opentelemetry`` itself is
# absent from the list on purpose: ``opentelemetry-api`` arrives transitively via
# fastmcp, so it is importable in every venv. What the [telemetry] extra adds is
# the SDK and the exporters. Each module is listed by its full name, because an
# entry already in ``sys.modules`` is returned without its parent being consulted:
# blocking only ``opentelemetry.sdk`` would not block a cached
# ``opentelemetry.sdk.trace``.
_EXTRA_MODULES = (
    "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.sdk.metrics",
    "opentelemetry.sdk.metrics.export",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
    "influxdb_client_3",
    "nats",
)


@pytest.fixture()
def no_telemetry_extra(monkeypatch):
    """Make every [telemetry] import fail, whether or not the extra is installed."""
    for mod in _EXTRA_MODULES:
        monkeypatch.setitem(sys.modules, mod, None)


@pytest.fixture()
def tlog(monkeypatch):
    r = LogRecorder()
    monkeypatch.setattr(telemetry, "log", r)
    return r


# --- off by default -----------------------------------------------------------
def test_simulated_absence_blocks_every_guarded_import(no_telemetry_extra):
    """Guards the premise of the missing-dependency tests below.

    Formerly ``test_optional_backends_are_absent_here``, which asserted that the
    environment lacked the extra. That is a fact about the venv, not the code.
    """
    for mod in _EXTRA_MODULES:
        with pytest.raises(ImportError):
            __import__(mod)


def test_init_with_no_env_is_a_noop():
    telemetry.init()
    assert telemetry.enabled() is False


def test_init_is_idempotent():
    telemetry.init()
    telemetry.init()
    assert telemetry.enabled() is False


def test_no_background_thread_when_no_sink_configured():
    """The default deployment must not pay for a thread it never uses."""
    before = {t.name for t in threading.enumerate()}
    telemetry.init()
    with telemetry.record_tool_call("read_file"):
        pass
    assert "homelab-ops-telemetry" not in ({t.name for t in threading.enumerate()} - before)


def test_record_tool_call_is_a_plain_timer_when_disabled():
    with telemetry.record_tool_call("read_file") as outcome:
        assert outcome == {"error": None}


def test_service_name_defaults_to_the_package_name():
    assert telemetry._service_name() == "homelab-ops-mcp"


def test_service_name_overridden_by_env(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "system-ops")
    assert telemetry._service_name() == "system-ops"


# --- missing optional dependency is survivable --------------------------------
# Each test asserts the import-failure warning as well as the end state, so it
# can only pass by taking the ``except ImportError`` branch it is named for.
def test_otlp_endpoint_set_but_sdk_missing(monkeypatch, no_telemetry_extra, tlog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()  # must not raise
    assert telemetry._tracer is None
    assert telemetry._calls_counter is None
    assert len(tlog.of("otlp_import_failed")) == 1


def test_influx_url_set_but_client_missing(monkeypatch, no_telemetry_extra, tlog):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    assert telemetry._influx_client is None
    assert len(tlog.of("influxdb3_import_failed")) == 1


def test_nats_url_set_but_client_missing(monkeypatch, no_telemetry_extra, tlog):
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    telemetry.init()
    # Publishing is scheduled on the sink loop and swallowed there; shutdown
    # drains the loop, so the warning has been logged by the time it returns.
    with telemetry.record_tool_call("read_file"):
        pass
    telemetry.shutdown()
    assert telemetry._nats_conn is None
    assert len(tlog.of("nats_import_failed")) == 1


def test_a_missing_backend_does_not_break_the_tool(monkeypatch, tmp_path, no_telemetry_extra):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    telemetry.init()
    target = tmp_path / "f.txt"
    assert write_file(str(target), "x")["bytes_written"] == 1
    assert read_file(str(target))["content"] == "x"


def test_emit_survives_a_backend_that_raises(monkeypatch):
    class Exploding:
        def add(self, *a, **k):
            raise RuntimeError("counter is broken")

        def record(self, *a, **k):
            raise RuntimeError("histogram is broken")

    monkeypatch.setattr(telemetry, "_calls_counter", Exploding())
    monkeypatch.setattr(telemetry, "_latency_hist", Exploding())
    with telemetry.record_tool_call("read_file"):
        pass  # must not raise


# --- outcome recording --------------------------------------------------------
def test_exception_is_recorded_and_reraised(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "_emit", lambda t, d, e: seen.append((t, e)))
    with pytest.raises(ValueError), telemetry.record_tool_call("edit_file"):
        raise ValueError("boom")
    assert seen == [("edit_file", "ValueError")]


def test_duration_is_recorded(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "_emit", lambda t, d, e: seen.append(d))
    with telemetry.record_tool_call("read_file"):
        pass
    assert len(seen) == 1
    assert seen[0] >= 0


# --- what the decorator counts as an error ------------------------------------
@contextlib.contextmanager
def _capture(store, tool):
    outcome = {"error": None}
    try:
        yield outcome
    finally:
        store.append((tool, outcome["error"]))


@pytest.fixture()
def calls(monkeypatch):
    store = []
    monkeypatch.setattr(telemetry, "record_tool_call", lambda tool: _capture(store, tool))
    return store


def test_a_returned_error_counts_as_an_error(calls):
    """These tools return {"error": ...} rather than raising."""
    assert "error" in write_file("~/nope.md", "x")
    assert calls == [("write_file", "tool_error")]


def test_a_successful_call_counts_as_ok(calls, tmp_path):
    write_file(str(tmp_path / "f.txt"), "x")
    assert calls == [("write_file", None)]


def test_nonzero_exit_code_is_not_a_tool_error(calls):
    """The command failed; the tool worked."""
    assert run_command("exit 3")["exit_code"] == 3
    assert calls == [("run_command", None)]


def test_a_refused_cwd_counts_as_an_error(calls):
    """run_command's own failures used to return no ``error`` key and count as ok."""
    run_command("true", cwd="relative")
    assert calls == [("run_command", "tool_error")]


def test_every_tool_is_instrumented(calls, tmp_path):
    target = tmp_path / "f.txt"
    write_file(str(target), "x")
    read_file(str(target))
    run_command("true")
    from homelab_ops_mcp.server import edit_file, list_processes, read_directory

    edit_file(str(target), "x", "y")
    read_directory(str(tmp_path))
    list_processes(limit=1)
    assert {name for name, _ in calls} == {
        "write_file",
        "read_file",
        "run_command",
        "edit_file",
        "read_directory",
        "list_processes",
    }


def test_instrumentation_preserves_the_tool_signature():
    """FastMCP builds the schema from the signature; a bare wrapper would erase it."""
    import inspect

    from homelab_ops_mcp import server

    sig = inspect.signature(server.read_file)
    assert list(sig.parameters) == ["path", "start_line", "end_line"]
    assert server.read_file.__doc__ is not None
    assert server.read_file.__name__ == "read_file"


# --- shutdown -----------------------------------------------------------------
def test_shutdown_stops_the_sink_loop(monkeypatch):
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    telemetry.init()
    telemetry._ensure_loop()
    assert telemetry._loop is not None
    telemetry.shutdown()
    assert telemetry._loop is None


def test_shutdown_is_safe_when_nothing_started():
    telemetry.shutdown()
    telemetry.shutdown()
