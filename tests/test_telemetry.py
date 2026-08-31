"""Telemetry: off by default, never fatal, and correct about what failed.

CI installs only the [dev] extra, so none of the optional backends are present
here. That is the important case to pin: the module must import and every entry
point must degrade to a no-op rather than raising.
"""

import contextlib
import threading

import pytest

from homelab_ops_mcp import telemetry
from homelab_ops_mcp.server import read_file, run_command, write_file

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


# --- off by default -----------------------------------------------------------
def test_optional_backends_are_absent_here():
    """Guards the premise of this file: these tests would prove nothing otherwise.

    Note ``opentelemetry`` itself IS importable — ``opentelemetry-api`` arrives
    transitively via fastmcp. What the [telemetry] extra adds, and what
    _init_otlp actually imports, is the SDK and the OTLP exporter. Asserting on
    the bare namespace package would test the wrong thing.
    """
    for mod in (
        "opentelemetry.sdk.trace",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "influxdb_client_3",
        "nats",
    ):
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
def test_otlp_endpoint_set_but_sdk_missing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()  # must not raise
    assert telemetry._tracer is None


def test_influx_url_set_but_client_missing(monkeypatch):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    assert telemetry._influx_client is None


def test_nats_url_set_but_client_missing(monkeypatch):
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    telemetry.init()
    # Publishing is scheduled on the sink loop and swallowed there.
    with telemetry.record_tool_call("read_file"):
        pass
    telemetry.shutdown()


def test_a_missing_backend_does_not_break_the_tool(monkeypatch, tmp_path):
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
