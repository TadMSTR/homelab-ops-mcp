"""The enabled telemetry paths, exercised against injected fake backends.

CI installs only [dev], so the real opentelemetry SDK, influxdb3-python and
nats-py are absent — see test_telemetry.py, which pins the disabled behaviour.
Leaving it there would leave every line of actual wiring untested, so these
tests put fake modules in sys.modules and let the real code import them. The
wiring runs; only the network does not.
"""

import asyncio
import sys
import types

import pytest

from homelab_ops_mcp import telemetry


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "SYSTEM_OPS_INFLUXDB3_URL",
        "SYSTEM_OPS_INFLUXDB3_TOKEN",
        "SYSTEM_OPS_INFLUXDB3_DATABASE",
        "SYSTEM_OPS_NATS_URL",
        "SYSTEM_OPS_NATS_SUBJECT",
    ):
        monkeypatch.delenv(var, raising=False)
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# --- OTLP ---------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.counters = {}
        self.histograms = {}
        self.spans = []
        self.resources = []
        self.exporters = []


@pytest.fixture()
def otel(monkeypatch):
    """Install a fake opentelemetry tree and return a recorder of what was wired."""
    rec = _Recorder()

    class Span:
        def __init__(self, name):
            self.name = name
            self.exceptions = []

        def record_exception(self, exc):
            self.exceptions.append(exc)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Tracer:
        def start_as_current_span(self, name):
            span = Span(name)
            rec.spans.append(span)
            return span

    class Counter:
        def __init__(self, name):
            self.name, self.calls = name, []

        def add(self, value, attrs):
            self.calls.append((value, attrs))

    class Histogram:
        def __init__(self, name):
            self.name, self.calls = name, []

        def record(self, value, attrs):
            self.calls.append((value, attrs))

    class Meter:
        def create_counter(self, name, unit=None):
            c = Counter(name)
            rec.counters[name] = c
            return c

        def create_histogram(self, name, unit=None):
            h = Histogram(name)
            rec.histograms[name] = h
            return h

    trace_mod = _module(
        "opentelemetry.trace",
        set_tracer_provider=lambda tp: None,
        get_tracer=lambda name: Tracer(),
    )
    metrics_mod = _module(
        "opentelemetry.metrics",
        set_meter_provider=lambda mp: None,
        get_meter=lambda name: Meter(),
    )
    root = _module("opentelemetry", trace=trace_mod, metrics=metrics_mod)

    class TracerProvider:
        def __init__(self, resource=None):
            self.resource = resource

        def add_span_processor(self, proc):
            rec.exporters.append(proc)

    def _resource_create(attrs):
        rec.resources.append(attrs)
        return attrs

    mods = {
        "opentelemetry": root,
        "opentelemetry.trace": trace_mod,
        "opentelemetry.metrics": metrics_mod,
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter": _module(
            "m", OTLPMetricExporter=lambda endpoint=None: ("metric-exporter", endpoint)
        ),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": _module(
            "t", OTLPSpanExporter=lambda endpoint=None: ("span-exporter", endpoint)
        ),
        "opentelemetry.sdk.metrics": _module(
            "sm", MeterProvider=lambda resource=None, metric_readers=None: None
        ),
        "opentelemetry.sdk.metrics.export": _module(
            "sme", PeriodicExportingMetricReader=lambda exp: ("reader", exp)
        ),
        "opentelemetry.sdk.resources": _module(
            "sr", Resource=_module("R", create=_resource_create)
        ),
        "opentelemetry.sdk.trace": _module("st", TracerProvider=TracerProvider),
        "opentelemetry.sdk.trace.export": _module(
            "ste", BatchSpanProcessor=lambda exp: ("batch", exp)
        ),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return rec


def test_otlp_wires_tracer_and_instruments(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()
    assert telemetry._tracer is not None
    assert set(otel.counters) == {
        "homelab_ops_mcp.tool.calls",
        "homelab_ops_mcp.tool.errors",
    }
    assert set(otel.histograms) == {"homelab_ops_mcp.tool.latency"}
    assert telemetry.enabled() is True


def test_otlp_uses_the_configured_endpoint(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    telemetry.init()
    assert otel.exporters == [("batch", ("span-exporter", "http://collector:4317"))]


def test_otlp_service_name_default(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()
    assert otel.resources == [{"service.name": "homelab-ops-mcp"}]


def test_otlp_service_name_override(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "system-ops")
    telemetry.init()
    assert otel.resources == [{"service.name": "system-ops"}]


def test_a_span_is_opened_per_call(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()
    with telemetry.record_tool_call("read_file"):
        pass
    assert [s.name for s in otel.spans] == ["tool.read_file"]


def test_metrics_are_recorded_with_status(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()
    with telemetry.record_tool_call("read_file") as outcome:
        outcome["error"] = "tool_error"
    with telemetry.record_tool_call("read_file"):
        pass

    calls = otel.counters["homelab_ops_mcp.tool.calls"].calls
    assert [attrs["status"] for _, attrs in calls] == ["error", "ok"]
    errors = otel.counters["homelab_ops_mcp.tool.errors"].calls
    assert errors == [(1, {"tool": "read_file", "error": "tool_error"})]
    assert len(otel.histograms["homelab_ops_mcp.tool.latency"].calls) == 2


def test_exception_is_recorded_on_the_span(monkeypatch, otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()
    with pytest.raises(ValueError), telemetry.record_tool_call("edit_file"):
        raise ValueError("boom")
    assert [type(e).__name__ for e in otel.spans[0].exceptions] == ["ValueError"]


def test_otlp_init_failure_is_not_fatal(monkeypatch, otel):
    def explode(_):
        raise RuntimeError("provider is broken")

    monkeypatch.setitem(
        sys.modules, "opentelemetry.sdk.trace", _module("st", TracerProvider=explode)
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    telemetry.init()  # must not raise
    assert telemetry._tracer is None


# --- InfluxDB 3 ---------------------------------------------------------------
class _Point:
    def __init__(self, measurement):
        self.measurement, self.tags, self.fields = measurement, {}, {}

    def tag(self, k, v):
        self.tags[k] = v
        return self

    def field(self, k, v):
        self.fields[k] = v
        return self


@pytest.fixture()
def influx(monkeypatch):
    written = []

    class Client:
        def __init__(self, host=None, token=None, database=None):
            self.host, self.token, self.database = host, token, database
            self.closed = False

        def write(self, point):
            written.append(point)

        def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "influxdb_client_3",
        _module("i", InfluxDBClient3=Client, Point=_Point),
    )
    return written


def test_influx_client_built_from_env(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_TOKEN", "tok")
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_DATABASE", "db")
    telemetry.init()
    assert telemetry._influx_client.host == "http://127.0.0.1:8181"
    assert telemetry._influx_client.token == "tok"
    assert telemetry._influx_client.database == "db"


def test_influx_database_defaults(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    assert telemetry._influx_client.database == "homelab_ops_mcp"


def test_influx_point_shape(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    telemetry._influx_write("read_file", 0.25, None)
    (point,) = influx
    assert point.measurement == "tool_call"
    assert point.tags == {"tool": "read_file", "status": "ok"}
    assert point.fields == {"latency_s": 0.25, "count": 1}


def test_influx_point_tags_the_error(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    telemetry._influx_write("edit_file", 0.1, "tool_error")
    (point,) = influx
    assert point.tags["status"] == "error"
    assert point.tags["error"] == "tool_error"


def test_influx_write_failure_is_swallowed(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()

    def boom(_):
        raise RuntimeError("influx is down")

    monkeypatch.setattr(telemetry._influx_client, "write", boom)
    telemetry._influx_write("read_file", 0.1, None)  # must not raise


def test_influx_init_failure_is_swallowed(monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("bad host")

    monkeypatch.setitem(
        sys.modules, "influxdb_client_3", _module("i", InfluxDBClient3=explode, Point=_Point)
    )
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    assert telemetry._influx_client is None


def test_influx_write_reaches_the_sink_through_a_tool_call(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    with telemetry.record_tool_call("read_file"):
        pass
    telemetry.shutdown()  # drains the sink loop
    assert [p.tags["tool"] for p in influx] == ["read_file"]


def test_shutdown_closes_the_influx_client(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    client = telemetry._influx_client
    telemetry.shutdown()
    assert client.closed is True
    assert telemetry._influx_client is None


# --- NATS ---------------------------------------------------------------------
@pytest.fixture()
def nats_stub(monkeypatch):
    published = []
    connects = []

    class Conn:
        def __init__(self):
            self.is_connected = True
            self.drained = False

        async def publish(self, subject, payload):
            published.append((subject, payload))

        async def drain(self):
            self.drained = True
            self.is_connected = False

    async def connect(url, **kwargs):
        connects.append((url, kwargs))
        return Conn()

    monkeypatch.setitem(sys.modules, "nats", _module("nats", connect=connect))
    return published, connects


def _run(coro):
    return asyncio.run(coro)


def test_nats_publishes_the_payload(monkeypatch, nats_stub):
    published, _ = nats_stub
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    _run(telemetry._nats_publish("run_command", 0.5, None))
    (subject, payload) = published[0]
    assert subject == "system.ops.mcp.metrics"
    import json

    assert json.loads(payload) == {
        "tool": "run_command",
        "latency_s": 0.5,
        "status": "ok",
        "error": None,
    }


def test_nats_subject_override(monkeypatch, nats_stub):
    published, _ = nats_stub
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("SYSTEM_OPS_NATS_SUBJECT", "custom.subject")
    _run(telemetry._nats_publish("run_command", 0.5, None))
    assert published[0][0] == "custom.subject"


def test_nats_connect_is_bounded_and_quiet(monkeypatch, nats_stub):
    """nats-py's defaults retry a dead endpoint for ~2 minutes, logging per attempt."""
    _, connects = nats_stub
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    _run(telemetry._nats_publish("run_command", 0.5, None))
    (_, kwargs) = connects[0]
    assert kwargs["connect_timeout"] == 2
    assert kwargs["max_reconnect_attempts"] == 3


def test_nats_records_the_error_status(monkeypatch, nats_stub):
    published, _ = nats_stub
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    _run(telemetry._nats_publish("edit_file", 0.1, "tool_error"))
    import json

    body = json.loads(published[0][1])
    assert body["status"] == "error"
    assert body["error"] == "tool_error"


def test_nats_publish_failure_is_swallowed(monkeypatch):
    async def connect(url, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setitem(sys.modules, "nats", _module("nats", connect=connect))
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    _run(telemetry._nats_publish("read_file", 0.1, None))  # must not raise


def test_nats_noop_without_a_url(monkeypatch, nats_stub):
    published, connects = nats_stub
    _run(telemetry._nats_publish("read_file", 0.1, None))
    assert published == [] and connects == []


# --- the background sink loop -------------------------------------------------
def test_sink_loop_starts_only_when_a_sink_is_configured(monkeypatch, influx):
    assert telemetry._loop is None
    with telemetry.record_tool_call("read_file"):
        pass
    assert telemetry._loop is None, "no sink configured — no loop should exist"

    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    with telemetry.record_tool_call("read_file"):
        pass
    assert telemetry._loop is not None


def test_sink_loop_is_reused_across_calls(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    with telemetry.record_tool_call("read_file"):
        pass
    first = telemetry._loop
    with telemetry.record_tool_call("read_file"):
        pass
    assert telemetry._loop is first


def test_sink_thread_is_a_daemon(monkeypatch, influx):
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    telemetry._ensure_loop()
    assert telemetry._loop_thread.daemon is True
    assert telemetry._loop_thread.name == "homelab-ops-telemetry"


def test_nats_publish_reaches_the_sink_through_a_tool_call(monkeypatch, nats_stub):
    published, _ = nats_stub
    monkeypatch.setenv("SYSTEM_OPS_NATS_URL", "nats://127.0.0.1:4222")
    telemetry.init()
    with telemetry.record_tool_call("run_command"):
        pass
    telemetry.shutdown()
    assert [subject for subject, _ in published] == ["system.ops.mcp.metrics"]


def test_shutdown_drains_before_closing_the_client(monkeypatch, influx):
    """Closing first would let every queued write find a None client and vanish.

    The queue is drained inside shutdown, so this passes only if the drain runs
    before the client is torn down.
    """
    monkeypatch.setenv("SYSTEM_OPS_INFLUXDB3_URL", "http://127.0.0.1:8181")
    telemetry.init()
    for tool in ("read_file", "write_file", "run_command"):
        with telemetry.record_tool_call(tool):
            pass
    telemetry.shutdown()
    assert sorted(p.tags["tool"] for p in influx) == [
        "read_file",
        "run_command",
        "write_file",
    ]
